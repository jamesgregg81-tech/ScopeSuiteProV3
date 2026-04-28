
import os
import csv
import math
import time
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import serial
from serial.tools import list_ports

# =========================
# USER SETTINGS
# =========================
PORT = "COM2"

# Fluke 19x/19xC powers up at 1200 baud. The script connects there first,
# then asks the instrument to switch to WORK_BAUDRATE and reopens the port.
INITIAL_BAUDRATE = 1200
WORK_BAUDRATE = 9600
FALLBACK_BAUDRATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600]

# Channel B is already scaled to amps by the ScopeMeter.
CURRENT_SCALE_A_PER_V = 1.0

# Best workflow:
# 1) On the ScopeMeter, manually recall the saved Record+Setup memory.
# 2) Enter Replay and confirm the replay set is visible.
# 3) Run this script.
MANUAL_REPLAY_MODE = True

# If MANUAL_REPLAY_MODE is False, the script will try RS 1001 first.
RECORD_MEMORY = 1001

# Optional correction if replay exports show ~66.67 Hz on a real 60 Hz system.
# Leave at 1.0 unless you know you need it.
TIMEBASE_CORRECTION = 1.0

SAVE_ROOT = r"C:\Users\JimGr\Desktop\FlukeReplayFinalReports"
SITE_NAME = "Fluke ScopeMeter 19x"
EQUIPMENT_NAME = "Maintenance loads"
TECHNICIAN = "Jim G."
NOTES = ""

SERIAL_TIMEOUT = 10
SERIAL_REOPEN_DELAY_S = 0.5
SERIAL_POST_OPEN_DELAY_S = 0.5

UNITS = [
    None, "V", "A", "Ohm", "W", "F", "K", "s", "h", "days",
    "Hz", "deg", "degC", "degF", "%", "dBm50", "dBm600",
    "dBV", "dBA", "dBW", "VAR", "VA"
]


def checksum_ok(data: bytes, check: int) -> bool:
    total = 0
    for b in data:
        total = (total + b) % 256
    return total == check


def get_uint(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=False)


def get_int(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=True)


def get_float3(data: bytes) -> float:
    mantissa = get_int(data[0:2])
    exponent = get_int(data[2:3])
    return float(mantissa * (10.0 ** exponent))




def list_available_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found by pySerial.")
        return []
    print("Available serial ports:")
    for p in ports:
        desc = p.description or ""
        hwid = p.hwid or ""
        print(f"  {p.device:8s}  {desc}  {hwid}")
    return ports


def read_ascii_until_cr(ser: serial.Serial, timeout_s: float = 2.0) -> bytes:
    deadline = time.time() + timeout_s
    out = bytearray()
    while time.time() < deadline:
        b = ser.read(1)
        if b:
            out.extend(b)
            if b == b"\r":
                break
    return bytes(out)


def try_id_raw_once(port_name: str, baudrate: int, pause_s: float = 0.7):
    ser = None
    try:
        ser = open_scope(port_name, baudrate, timeout=2.0)
        time.sleep(pause_s)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        print(f"Trying raw ID on {port_name} at {baudrate} baud...")
        ser.write(b"\r")
        ser.flush()
        time.sleep(0.2)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(b"ID\r")
        ser.flush()
        raw = ser.read(64)
        if not raw:
            more = read_ascii_until_cr(ser, timeout_s=1.0)
            raw += more
        print(f"RAW response at {baudrate}: {raw!r}")
        return raw
    finally:
        if ser is not None:
            ser.close()


def autodetect_scope_port_and_baud(preferred_port: str | None = None):
    ports = list_available_ports()
    port_names = [p.device for p in ports]
    candidates = []
    if preferred_port:
        candidates.append(preferred_port)
    for p in port_names:
        if p not in candidates:
            candidates.append(p)

    if not candidates:
        raise RuntimeError("No COM ports found. Check OC4USB driver installation in Windows Device Manager.")

    attempts = []
    for port_name in candidates:
        for baud in FALLBACK_BAUDRATES:
            try:
                raw = try_id_raw_once(port_name, baud)
                attempts.append((port_name, baud, raw))
                if raw.startswith(b"0\rFLUKE") or b"FLUKE" in raw:
                    print(f"Detected ScopeMeter on {port_name} at {baud} baud.")
                    return port_name, baud, attempts
            except Exception as e:
                print(f"Probe failed on {port_name} at {baud}: {e}")
                attempts.append((port_name, baud, repr(e).encode("ascii", errors="replace")))
    raise RuntimeError(
        "Could not find a responding ScopeMeter on any scanned COM port/baud. "
        "This points to COM port selection, OC4USB driver, optical head seating/alignment, "
        "or the instrument not seeing the IR adapter."
    )


def open_scope(port_name: str, baudrate: int, timeout: float = SERIAL_TIMEOUT) -> serial.Serial:
    print(f"Opening {port_name} at {baudrate},N,8,1 ...")
    return serial.Serial(
        port=port_name,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        write_timeout=timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def read_ack(ser: serial.Serial) -> int:
    ack = ser.read(2)
    if len(ack) != 2:
        raise RuntimeError(
            "Timeout waiting for ACK. "
            f"Got {ack!r}. Check COM port, OC4USB driver, optical head alignment, "
            f"and initial baud ({INITIAL_BAUDRATE})."
        )
    if ack[1] != 13:
        raise RuntimeError(f"Bad ACK terminator: {ack!r}")
    code = ack[0] - ord("0")
    print(f"<< ACK {code}")
    return code


def send_command(ser: serial.Serial, cmd: str, clear_input: bool = True) -> int:
    print(f"\n>> {cmd}")
    if clear_input:
        ser.reset_input_buffer()
    ser.write(cmd.encode("ascii") + b"\r")
    ser.flush()
    return read_ack(ser)


def read_cr_terminated_ascii(ser: serial.Serial) -> str:
    out = bytearray()
    while True:
        b = ser.read(1)
        if len(b) != 1:
            raise RuntimeError("Timeout while reading ASCII response.")
        if b[0] == 13:
            return out.decode("ascii", errors="replace")
        out.append(b[0])


def query_id(ser: serial.Serial) -> str:
    code = send_command(ser, "ID")
    if code != 0:
        raise RuntimeError(f"ID failed with ACK={code}")
    ident = read_cr_terminated_ascii(ser)
    print(f"ID response: {ident}")
    return ident



def connect_scope(port_name: str) -> tuple[serial.Serial, str]:
    """
    Connect to a Fluke 19x/19xC. Start with the requested port if possible,
    but auto-scan COM ports and common baud rates if the first ID query fails.
    Returns (serial_port, identification_string).
    """
    selected_port = port_name
    selected_baud = INITIAL_BAUDRATE
    ident = None
    ser = None

    try:
        ser = open_scope(selected_port, selected_baud)
        time.sleep(SERIAL_POST_OPEN_DELAY_S)
        ident = query_id(ser)
        print(f"Connected at {selected_baud}: {ident}")
    except Exception as first_error:
        print(f"Initial connection on {selected_port} at {selected_baud} failed: {first_error}")
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            ser = None

        print("\nAuto-detecting COM port / baud...")
        selected_port, selected_baud, _attempts = autodetect_scope_port_and_baud(selected_port)
        ser = open_scope(selected_port, selected_baud)
        time.sleep(SERIAL_POST_OPEN_DELAY_S)
        ident = query_id(ser)
        print(f"Connected after auto-detect on {selected_port} at {selected_baud}: {ident}")

    if WORK_BAUDRATE != selected_baud:
        code = send_command(ser, f"PC {WORK_BAUDRATE}")
        if code != 0:
            raise RuntimeError(f"PC {WORK_BAUDRATE} failed with ACK={code}")

        print(f"ScopeMeter accepted PC {WORK_BAUDRATE}. Reopening port...")
        ser.close()
        time.sleep(SERIAL_POST_BAUD_SWITCH_DELAY_S)

        ser = open_scope(selected_port, WORK_BAUDRATE)
        time.sleep(SERIAL_POST_OPEN_DELAY_S)
        ident = query_id(ser)
        print(f"Connected at {WORK_BAUDRATE}: {ident}")

    return ser, ident

def replay_status(ser: serial.Serial):
    code = send_command(ser, "RP")
    if code != 0:
        raise RuntimeError(f"RP failed with ACK={code}")
    resp = read_cr_terminated_ascii(ser).strip()
    print(f"RP response: {resp}")
    parts = [p.strip() for p in resp.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"Unexpected RP response format: {resp!r}")
    nr_screens = int(parts[0])
    screen_index = int(parts[1])
    return nr_screens, screen_index


def select_replay_screen(ser: serial.Serial, index: int):
    code = send_command(ser, f"RP {index}")
    if code != 0:
        raise RuntimeError(f"RP {index} failed with ACK={code}")


def get_header(ser: serial.Serial, int_size: int):
    raw = ser.read(3 + int_size)
    if len(raw) != 3 + int_size:
        raise RuntimeError("Timeout while reading block header.")
    if raw[0:2] != b"#0":
        raise RuntimeError(f"Bad block preamble: {raw!r}")
    header = raw[2]
    size = get_uint(raw[3:3 + int_size])
    return header, size


def get_block_data(ser: serial.Serial, size: int) -> bytes:
    raw = ser.read(size + 1)
    if len(raw) != size + 1:
        raise RuntimeError("Timeout while reading block data.")
    payload = raw[:-1]
    check = raw[-1]
    if not checksum_ok(payload, check):
        raise RuntimeError("Checksum failed.")
    return payload


def query_waveform(ser: serial.Serial, trace_no: str):
    print(f"\nRequesting waveform with QW {trace_no} ...")
    ser.reset_input_buffer()
    ser.write(f"QW {trace_no}\r".encode("ascii"))
    ser.flush()

    code = read_ack(ser)
    if code != 0:
        raise RuntimeError(f"QW {trace_no} failed with ACK={code}")

    admin_header, admin_size = get_header(ser, 2)
    print(f"Admin header={admin_header}, size={admin_size}")
    admin = get_block_data(ser, admin_size)

    comma = ser.read(1)
    if comma != b",":
        raise RuntimeError(f"Expected comma between admin and samples, got {comma!r}")

    sample_header, sample_size = get_header(ser, 4)
    print(f"Sample header={sample_header}, size={sample_size}")
    sample_data = get_block_data(ser, sample_size)

    term = ser.read(1)
    if term != b"\r":
        raise RuntimeError(f"Expected final CR, got {term!r}")

    return parse_waveform(admin, sample_data)


def parse_waveform(admin: bytes, sample_data: bytes):
    if len(admin) < 47:
        raise RuntimeError(f"Admin block too short: {len(admin)} bytes")

    y_unit_code = admin[1]
    x_unit_code = admin[2]
    y_unit = UNITS[y_unit_code] if y_unit_code < len(UNITS) else f"unit{y_unit_code}"
    x_unit = UNITS[x_unit_code] if x_unit_code < len(UNITS) else f"unit{x_unit_code}"

    y_divisions = get_uint(admin[3:5])
    x_divisions = get_uint(admin[5:7])
    y_scale = get_float3(admin[7:10])
    x_scale = get_float3(admin[10:13])

    y_zero = get_float3(admin[15:18])
    x_zero = get_float3(admin[18:21])
    y_resolution = get_float3(admin[21:24])
    delta_x = get_float3(admin[24:27]) * TIMEBASE_CORRECTION
    y_at_0 = get_float3(admin[27:30])

    fmt = sample_data[0]
    signed_vals = (fmt & 0b10000000) != 0
    group_bits = fmt & 0b01110000
    sample_width = fmt & 0b00000111

    if sample_width not in (1, 2, 4):
        raise RuntimeError(f"Unexpected numeric sample width: {sample_width}")

    get_num = get_int if signed_vals else get_uint

    samples_per_point = 1
    if group_bits == 0b01000000:
        samples_per_point = 2
    elif group_bits in (0b01100000, 0b01110000):
        samples_per_point = 3

    p = 1
    overload = get_num(sample_data[p:p + sample_width]); p += sample_width
    underload = get_num(sample_data[p:p + sample_width]); p += sample_width
    invalid = get_num(sample_data[p:p + sample_width]); p += sample_width
    n_points = get_uint(sample_data[p:p + 2]); p += 2

    raw = np.empty((n_points, samples_per_point), dtype=float)

    for i in range(n_points):
        for j in range(samples_per_point):
            val = get_num(sample_data[p:p + sample_width])
            p += sample_width
            if val == overload:
                raw[i, j] = np.inf
            elif val == underload:
                raw[i, j] = -np.inf
            elif val == invalid:
                raw[i, j] = np.nan
            else:
                raw[i, j] = y_zero + val * y_resolution

    if p != len(sample_data):
        raise RuntimeError(f"Sample parsing ended at {p}, expected {len(sample_data)}")

    y = raw[:, 0] if samples_per_point == 1 else np.nanmean(raw, axis=1)
    x = x_zero + np.arange(len(y)) * delta_x

    return {
        "x": x,
        "y": y,
        "x_unit": x_unit,
        "y_unit": y_unit,
        "delta_x": delta_x,
        "y_scale": y_scale,
        "x_scale": x_scale,
        "y_zero": y_zero,
        "x_zero": x_zero,
        "y_resolution": y_resolution,
        "y_at_0": y_at_0,
        "x_divisions": x_divisions,
        "y_divisions": y_divisions,
        "samples_per_point": samples_per_point,
        "n_points": len(y),
    }


def compute_fft(x, y):
    finite = np.isfinite(y)
    x = x[finite]
    y = y[finite]

    if len(y) < 8:
        raise RuntimeError("Not enough valid samples for FFT.")

    dt = float(np.median(np.diff(x)))
    fs = 1.0 / dt
    n = len(y)

    y_ac = y - np.mean(y)
    window = np.hanning(n)
    y_win = y_ac * window

    spec = np.fft.rfft(y_win)
    freq = np.fft.rfftfreq(n, d=dt)
    amp = (2.0 / np.sum(window)) * np.abs(spec)

    return freq, amp, fs


def dominant_frequency(freq, amp):
    if len(freq) <= 1:
        return float("nan"), float("nan"), -1
    idx = np.argmax(amp[1:]) + 1
    return float(freq[idx]), float(amp[idx]), int(idx)


def thd_from_fft(freq, amp, max_harmonic=15):
    f1, a1, _ = dominant_frequency(freq, amp)
    if not np.isfinite(f1) or a1 <= 0:
        return float("nan"), f1, a1, {}

    harmonics = {}
    sum_sq = 0.0
    for n in range(2, max_harmonic + 1):
        target = n * f1
        if target > freq[-1]:
            break
        idx = int(np.argmin(np.abs(freq - target)))
        an = float(amp[idx])
        harmonics[n] = an
        sum_sq += an * an

    thd = math.sqrt(sum_sq) / a1 if a1 > 0 else float("nan")
    return thd, f1, a1, harmonics


def wrap_phase_deg(angle_deg: float) -> float:
    return ((angle_deg + 180.0) % 360.0) - 180.0


def analyze_voltage_current(voltage_samples, current_samples, dt, current_scale_a_per_v):
    v = np.asarray(voltage_samples, dtype=float)
    i_raw = np.asarray(current_samples, dtype=float)

    finite = np.isfinite(v) & np.isfinite(i_raw)
    v = v[finite]
    i_raw = i_raw[finite]

    if len(v) == 0 or len(i_raw) == 0:
        raise ValueError("Voltage or current array is empty after removing invalid samples.")

    n = min(len(v), len(i_raw))
    v = v[:n]
    i = i_raw[:n] * current_scale_a_per_v

    v_mean = float(np.mean(v))
    i_mean = float(np.mean(i))
    vrms = float(np.sqrt(np.mean(v ** 2)))
    irms = float(np.sqrt(np.mean(i ** 2)))

    p_inst = v * i
    real_power_w = float(np.mean(p_inst))
    apparent_power_va = float(vrms * irms)

    if apparent_power_va > 0:
        power_factor = real_power_w / apparent_power_va
        power_factor = max(-1.0, min(1.0, power_factor))
    else:
        power_factor = float("nan")

    reactive_power_var = float(math.sqrt(max(apparent_power_va ** 2 - real_power_w ** 2, 0.0)))

    v_for_fft = v - np.mean(v)
    i_for_fft = i - np.mean(i)
    window = np.hanning(n)

    V = np.fft.rfft(v_for_fft * window)
    I = np.fft.rfft(i_for_fft * window)
    freqs = np.fft.rfftfreq(n, d=dt)

    if len(freqs) < 2:
        raise ValueError("Not enough samples for FFT phase analysis.")

    k = int(np.argmax(np.abs(V[1:])) + 1)
    fundamental_hz = float(freqs[k])

    phase_v_deg = math.degrees(np.angle(V[k]))
    phase_i_deg = math.degrees(np.angle(I[k]))
    phase_i_minus_v_deg = wrap_phase_deg(phase_i_deg - phase_v_deg)

    if phase_i_minus_v_deg < -1.0:
        phase_note = "Current lags voltage"
    elif phase_i_minus_v_deg > 1.0:
        phase_note = "Current leads voltage"
    else:
        phase_note = "Voltage and current nearly in phase"

    return {
        "fundamental_hz": fundamental_hz,
        "v_mean_v": v_mean,
        "i_mean_a": i_mean,
        "vrms_v": vrms,
        "irms_a": irms,
        "real_power_w": real_power_w,
        "apparent_power_va": apparent_power_va,
        "reactive_power_var": reactive_power_var,
        "power_factor": power_factor,
        "phase_i_minus_v_deg": phase_i_minus_v_deg,
        "phase_note": phase_note,
    }


def save_waveform_plot(path, frame_name, wf_a, wf_b):
    plt.figure(figsize=(10, 6))
    plt.plot(wf_a["x"], wf_a["y"], label=f"Channel A ({wf_a['y_unit']})")
    plt.plot(wf_b["x"], wf_b["y"], label=f"Channel B ({wf_b['y_unit']})")
    plt.xlabel(f"Time ({wf_a['x_unit']})")
    plt.ylabel("Amplitude")
    plt.title(frame_name)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def save_fft_plot(path, frame_name, freq_a, amp_a, freq_b, amp_b, unit_a, unit_b):
    plt.figure(figsize=(10, 6))
    plt.plot(freq_a, amp_a, label=f"Channel A FFT ({unit_a})")
    plt.plot(freq_b, amp_b, label=f"Channel B FFT ({unit_b})")
    plt.xlim(left=0)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Amplitude")
    plt.title(f"{frame_name} FFT")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def frame_summary_text(frame_name, power, wf_a, dom_a, ampdom_a, wf_b, dom_b, ampdom_b, thd_a, thd_b):
    lines = [
        f"Frame: {frame_name}",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Channel A",
        f"Unit: {wf_a['y_unit']}",
        f"RMS: {power['vrms_v']:.6f}",
        f"Dominant frequency: {dom_a:.6f} Hz",
        f"Dominant amplitude: {ampdom_a:.6f} {wf_a['y_unit']}",
        f"THD estimate: {thd_a:.6f}",
        "",
        "Channel B",
        f"Unit: {wf_b['y_unit']}",
        f"RMS: {power['irms_a']:.6f}",
        f"Dominant frequency: {dom_b:.6f} Hz",
        f"Dominant amplitude: {ampdom_b:.6f} {wf_b['y_unit']}",
        f"THD estimate: {thd_b:.6f}",
        "",
        "Power Analysis",
        f"Vrms: {power['vrms_v']:.6f} V",
        f"Irms: {power['irms_a']:.6f} A",
        f"Real Power: {power['real_power_w']:.6f} W",
        f"Apparent Power: {power['apparent_power_va']:.6f} VA",
        f"Reactive Power: {power['reactive_power_var']:.6f} VAR",
        f"Power Factor: {power['power_factor']:.6f}",
        f"Phase (I - V): {power['phase_i_minus_v_deg']:.6f} deg",
        f"Interpretation: {power['phase_note']}",
    ]
    return "\n".join(lines)


def write_csv(csv_path, rows):
    headers = [
        "frame_name", "replay_index",
        "vrms_v", "irms_a",
        "real_power_w", "apparent_power_va", "reactive_power_var",
        "power_factor", "phase_i_minus_v_deg",
        "dominant_freq_v_hz", "dominant_freq_i_hz",
        "thd_v", "thd_i",
        "harm3_i", "harm5_i", "harm7_i",
        "waveform_png", "fft_png", "summary_txt",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def worst_case_summary(rows):
    def best_row(key, fn=max):
        valid = [r for r in rows if isinstance(r.get(key), (int, float)) and np.isfinite(r.get(key))]
        return fn(valid, key=lambda r: r[key]) if valid else None

    def min_row(key):
        return best_row(key, fn=min)

    low_pf = min_row("power_factor")
    high_i = best_row("irms_a")
    high_kw = best_row("real_power_w")
    high_q = best_row("reactive_power_var")
    high_thd_i = best_row("thd_i")

    lines = ["Worst-Case Detector", "===================", ""]
    if low_pf:
        lines += [f"Lowest PF frame: {low_pf['frame_name']}  PF={low_pf['power_factor']:.3f}", ""]
    if high_i:
        lines += [f"Highest current frame: {high_i['frame_name']}  Irms={high_i['irms_a']:.3f} A", ""]
    if high_kw:
        lines += [f"Highest real power frame: {high_kw['frame_name']}  kW={high_kw['real_power_w']/1000.0:.3f}", ""]
    if high_q:
        lines += [f"Highest reactive power frame: {high_q['frame_name']}  kVAR={high_q['reactive_power_var']/1000.0:.3f}", ""]
    if high_thd_i:
        lines += [f"Highest estimated current THD frame: {high_thd_i['frame_name']}  THD(I)={high_thd_i['thd_i']:.3f}", ""]
    return "\n".join(lines)


def global_summary_text(ident, rp_count, rows):
    vrms_vals = np.array([r["vrms_v"] for r in rows], dtype=float)
    irms_vals = np.array([r["irms_a"] for r in rows], dtype=float)
    kw_vals = np.array([r["real_power_w"] for r in rows], dtype=float) / 1000.0
    pf_vals = np.array([r["power_factor"] for r in rows], dtype=float)
    kvar_vals = np.array([r["reactive_power_var"] for r in rows], dtype=float) / 1000.0
    thd_i_vals = np.array([r["thd_i"] for r in rows], dtype=float)

    lines = [
        "FLUKE FINAL FIELD REPLAY REPORT",
        "================================",
        "",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scope ID: {ident}",
        f"Site: {SITE_NAME}",
        f"Equipment / Load: {EQUIPMENT_NAME}",
        f"Technician: {TECHNICIAN}",
        f"Replay frames exported: {len(rows)}",
        f"Scope replay count reported: {rp_count}",
        f"Notes: {NOTES}",
        "",
        "Global Summary",
        "--------------",
        f"Vrms mean/min/max: {np.mean(vrms_vals):.2f} / {np.min(vrms_vals):.2f} / {np.max(vrms_vals):.2f} V",
        f"Irms mean/min/max: {np.mean(irms_vals):.2f} / {np.min(irms_vals):.2f} / {np.max(irms_vals):.2f} A",
        f"kW mean/min/max: {np.mean(kw_vals):.2f} / {np.min(kw_vals):.2f} / {np.max(kw_vals):.2f} kW",
        f"kVAR mean/min/max: {np.mean(kvar_vals):.2f} / {np.min(kvar_vals):.2f} / {np.max(kvar_vals):.2f} kVAR",
        f"PF mean/min/max: {np.mean(pf_vals):.3f} / {np.min(pf_vals):.3f} / {np.max(pf_vals):.3f}",
        f"Estimated current THD mean/max: {np.nanmean(thd_i_vals):.3f} / {np.nanmax(thd_i_vals):.3f}",
        "",
        worst_case_summary(rows),
    ]
    return "\n".join(lines)


def save_trend_plot(path, rows):
    labels = [r["frame_name"] for r in rows]
    x = np.arange(len(rows))
    vrms = np.array([r["vrms_v"] for r in rows], dtype=float)
    irms = np.array([r["irms_a"] for r in rows], dtype=float)
    kw = np.array([r["real_power_w"] for r in rows], dtype=float) / 1000.0
    pf = np.array([r["power_factor"] for r in rows], dtype=float)

    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True)
    axes[0].plot(x, vrms, marker="o"); axes[0].set_ylabel("Vrms (V)"); axes[0].grid(True)
    axes[1].plot(x, irms, marker="o"); axes[1].set_ylabel("Irms (A)"); axes[1].grid(True)
    axes[2].plot(x, kw, marker="o"); axes[2].set_ylabel("kW"); axes[2].grid(True)
    axes[3].plot(x, pf, marker="o"); axes[3].set_ylabel("PF"); axes[3].grid(True)
    axes[3].set_xticks(x); axes[3].set_xticklabels(labels, rotation=45, ha="right")
    axes[3].set_xlabel("Replay Frame")
    fig.suptitle("Replay Trend Summary")
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def save_harmonic_trend_plot(path, rows):
    labels = [r["frame_name"] for r in rows]
    x = np.arange(len(rows))
    h3 = np.array([r["harm3_i"] for r in rows], dtype=float)
    h5 = np.array([r["harm5_i"] for r in rows], dtype=float)
    h7 = np.array([r["harm7_i"] for r in rows], dtype=float)
    thd_i = np.array([r["thd_i"] for r in rows], dtype=float)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(x, h3, marker="o", label="H3")
    axes[0].plot(x, h5, marker="o", label="H5")
    axes[0].plot(x, h7, marker="o", label="H7")
    axes[0].grid(True); axes[0].legend(); axes[0].set_ylabel("Harmonic Current (A)")
    axes[1].plot(x, thd_i, marker="o")
    axes[1].grid(True); axes[1].set_ylabel("THD(I)")
    axes[1].set_xticks(x); axes[1].set_xticklabels(labels, rotation=45, ha="right")
    axes[1].set_xlabel("Replay Frame")
    fig.suptitle("Replay Harmonic Trend Summary")
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    print("Fluke Final Replay Field Tool starting...")
    os.makedirs(SAVE_ROOT, exist_ok=True)
    export_dir = os.path.join(SAVE_ROOT, "replay_final_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(export_dir, exist_ok=True)

    ser = None
    rows = []

    try:
        ser, ident = connect_scope(PORT)

        if not MANUAL_REPLAY_MODE:
            print(f"\nRecalling long record memory {RECORD_MEMORY} ...")
            code = send_command(ser, f"RS {RECORD_MEMORY}")
            if code != 0:
                raise RuntimeError(f"RS {RECORD_MEMORY} failed with ACK={code}")

        nr_screens, screen_index = replay_status(ser)
        if nr_screens <= 0:
            print("\nTrying RP 0 to force replay mode ...")
            select_replay_screen(ser, 0)
            nr_screens, screen_index = replay_status(ser)

        if nr_screens <= 0:
            raise RuntimeError("No replay screens found.")

        print(f"Replay status: screens={nr_screens}, current index={screen_index}")
        replay_indices = list(range(-(nr_screens - 1), 1))
        print(f"Exporting replay indices: {replay_indices}")

        for idx in replay_indices:
            frame_name = f"replay_{'m' + str(abs(idx)).zfill(2) if idx < 0 else 'p' + str(idx).zfill(2)}"
            print(f"\n=== Exporting {frame_name} (RP {idx}) ===")
            select_replay_screen(ser, idx)

            wf_a = query_waveform(ser, "10")
            wf_b = query_waveform(ser, "20")
            freq_a, amp_a, _ = compute_fft(wf_a["x"], wf_a["y"])
            freq_b, amp_b, _ = compute_fft(wf_b["x"], wf_b["y"])
            dom_a, ampdom_a, _ = dominant_frequency(freq_a, amp_a)
            dom_b, ampdom_b, _ = dominant_frequency(freq_b, amp_b)
            thd_a, _, _, harm_a = thd_from_fft(freq_a, amp_a)
            thd_b, _, _, harm_b = thd_from_fft(freq_b, amp_b)
            power = analyze_voltage_current(wf_a["y"], wf_b["y"], wf_a["delta_x"], CURRENT_SCALE_A_PER_V)

            waveform_png = os.path.join(export_dir, f"{frame_name}_waveforms.png")
            fft_png = os.path.join(export_dir, f"{frame_name}_fft.png")
            summary_txt = os.path.join(export_dir, f"{frame_name}_summary.txt")

            save_waveform_plot(waveform_png, frame_name, wf_a, wf_b)
            save_fft_plot(fft_png, frame_name, freq_a, amp_a, freq_b, amp_b, wf_a["y_unit"], wf_b["y_unit"])
            with open(summary_txt, "w", encoding="utf-8") as f:
                f.write(frame_summary_text(frame_name, power, wf_a, dom_a, ampdom_a, wf_b, dom_b, ampdom_b, thd_a, thd_b))

            rows.append({
                "frame_name": frame_name,
                "replay_index": idx,
                "vrms_v": power["vrms_v"],
                "irms_a": power["irms_a"],
                "real_power_w": power["real_power_w"],
                "apparent_power_va": power["apparent_power_va"],
                "reactive_power_var": power["reactive_power_var"],
                "power_factor": power["power_factor"],
                "phase_i_minus_v_deg": power["phase_i_minus_v_deg"],
                "dominant_freq_v_hz": dom_a,
                "dominant_freq_i_hz": dom_b,
                "thd_v": thd_a,
                "thd_i": thd_b,
                "harm3_i": harm_b.get(3, float("nan")),
                "harm5_i": harm_b.get(5, float("nan")),
                "harm7_i": harm_b.get(7, float("nan")),
                "waveform_png": os.path.basename(waveform_png),
                "fft_png": os.path.basename(fft_png),
                "summary_txt": os.path.basename(summary_txt),
            })

        csv_path = os.path.join(export_dir, "replay_summary.csv")
        write_csv(csv_path, rows)
        trend_png = os.path.join(export_dir, "global_trend_summary.png")
        harmonic_trend_png = os.path.join(export_dir, "global_harmonic_summary.png")
        global_summary_txt = os.path.join(export_dir, "FINAL_GLOBAL_REPORT.txt")
        save_trend_plot(trend_png, rows)
        save_harmonic_trend_plot(harmonic_trend_png, rows)
        with open(global_summary_txt, "w", encoding="utf-8") as f:
            f.write(global_summary_text(ident, nr_screens, rows))

        print(f"\nSaved export folder: {export_dir}")
        print(f"Saved replay summary CSV: {csv_path}")
        print(f"Saved trend plot: {trend_png}")
        print(f"Saved harmonic trend plot: {harmonic_trend_png}")
        print(f"Saved final global report: {global_summary_txt}")

    finally:
        if ser is not None:
            try:
                ser.close()
            finally:
                print("\nSerial port closed.")


if __name__ == "__main__":
    main()
