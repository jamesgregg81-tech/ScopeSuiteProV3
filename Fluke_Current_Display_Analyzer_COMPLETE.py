
import os
import csv
import math
import time
import atexit
import traceback
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import serial
from serial.tools import list_ports

# =========================
# USER SETTINGS
# =========================
PORT = "COM2"
PREFERRED_BAUDS = [9600, 1200, 2400, 4800, 19200, 38400, 57600]

# Fixed field convention per user:
# Channel A = Voltage
# Channel B = Current
CURRENT_SCALE_A_PER_V = 1.0

# Optional command to recall something before analysis, e.g. "RS 1"
RECALL_COMMAND = None

SAVE_ROOT = r"C:\Users\JimGr\Desktop\FlukeCurrentScreenAnalysis"
SITE_NAME = "Fluke ScopeMeter 19x"
EQUIPMENT_NAME = "Maintenance loads"
TECHNICIAN = "Jim G."
NOTES = ""

UNITS = [
    None, "V", "A", "Ohm", "W", "F", "K", "s", "h", "days",
    "Hz", "deg", "degC", "degF", "%", "dBm50", "dBm600",
    "dBV", "dBA", "dBW", "VAR", "VA"
]

_ACTIVE_SER = None
_RETURNED_TO_LOCAL = False


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


def open_scope(port_name: str, baudrate: int, timeout: float = 3.0) -> serial.Serial:
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
            f"Timeout waiting for ACK. Got {ack!r}. "
            "Check COM port, OC4USB driver, optical head alignment, and baud."
        )
    if ack[1] != 13:
        raise RuntimeError(f"Bad ACK terminator: {ack!r}")
    code = ack[0] - ord("0")
    print(f"<< ACK {code}")
    return code


def send_command(ser: serial.Serial, cmd: str, clear_input: bool = True) -> int:
    print(f"\n>> {cmd}")
    if clear_input:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
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


def try_get_id(ser: serial.Serial):
    print("\n>> ID")
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    ser.write(b"ID\r")
    ser.flush()
    ack = ser.read(2)
    if len(ack) != 2:
        raise RuntimeError(
            f"Timeout waiting for ACK. Got {ack!r}. "
            "Check COM port, OC4USB driver, optical head alignment, and baud."
        )
    if ack[1] != 13:
        raise RuntimeError(f"Bad ACK terminator: {ack!r}")
    code = ack[0] - ord("0")
    print(f"<< ACK {code}")
    if code == 0:
        ident = read_cr_terminated_ascii(ser)
        print(f"ID response: {ident}")
        return True, code, ident
    return False, code, None


def ensure_remote_ready(ser: serial.Serial) -> None:
    for cmd in ["GR", "GL", "GR"]:
        try:
            code = send_command(ser, cmd)
            if code == 0:
                time.sleep(0.15)
        except Exception as exc:
            print(f"{cmd} attempt did not help: {exc}")
            time.sleep(0.15)


def query_id(ser: serial.Serial) -> str:
    ok, code, ident = try_get_id(ser)
    if ok:
        return ident
    print(f"ID returned ACK={code}; trying GR/GL recovery...")
    ensure_remote_ready(ser)
    ok, code, ident = try_get_id(ser)
    if ok:
        return ident
    raise RuntimeError(f"ID failed with ACK={code}")


def list_available_ports():
    print("Available serial ports:")
    ports = list(list_ports.comports())
    for p in ports:
        print(f"  {p.device:<9} {p.description}  {p.hwid}")
    return ports


def raw_probe_id(port_name: str, baudrate: int) -> bytes:
    ser = open_scope(port_name, baudrate, timeout=1.2)
    try:
        time.sleep(0.2)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        print(f"Trying raw ID on {port_name} at {baudrate} baud...")
        ser.write(b"ID\r")
        ser.flush()
        data = ser.read(128)
        print(f"RAW response at {baudrate}: {data!r}")
        return data
    finally:
        ser.close()


def detect_scope(preferred_port: str):
    print("\nAuto-detecting COM port / baud...")
    ports = list_available_ports()
    port_names = [p.device for p in ports]

    ordered_ports = []
    if preferred_port and preferred_port in port_names:
        ordered_ports.append(preferred_port)
    for p in port_names:
        if p not in ordered_ports:
            ordered_ports.append(p)

    for port_name in ordered_ports:
        for baud in PREFERRED_BAUDS:
            try:
                raw = raw_probe_id(port_name, baud)
            except Exception as exc:
                print(f"Probe failed on {port_name} at {baud}: {exc}")
                continue

            if b"FLUKE" in raw and raw.startswith(b"0\r"):
                print(f"Detected ScopeMeter on {port_name} at {baud} baud.")
                return port_name, baud

            if raw in (b"0\r", b"1\r", b"2\r", b"3\r", b"4\r", b"5\r", b"6\r", b"7\r", b"8\r", b"9\r"):
                print(
                    f"Detected probable ScopeMeter on {port_name} at {baud} baud "
                    f"(reply {raw!r}). Will attempt recovery."
                )
                return port_name, baud

    raise RuntimeError(
        "Could not detect the ScopeMeter on any tested COM port / baud rate.\n"
        "Check the OC4USB/adapter, driver, COM port, and optical head alignment."
    )


def connect_scope(preferred_port: str):
    if preferred_port:
        for baud in PREFERRED_BAUDS:
            ser = None
            try:
                ser = open_scope(preferred_port, baud)
                time.sleep(0.25)
                ident = query_id(ser)
                print(f"Connected on {preferred_port} at {baud}: {ident}")
                return ser, ident, preferred_port, baud
            except Exception as exc:
                print(f"Initial connection on {preferred_port} at {baud} failed: {exc}")
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

    port_name, baud = detect_scope(preferred_port)
    ser = open_scope(port_name, baud)
    time.sleep(0.25)
    ident = query_id(ser)
    print(f"Connected after auto-detect on {port_name} at {baud}: {ident}")
    return ser, ident, port_name, baud


def maybe_recall(ser: serial.Serial):
    if not RECALL_COMMAND:
        return
    code = send_command(ser, RECALL_COMMAND)
    if code != 0:
        raise RuntimeError(f"{RECALL_COMMAND} failed with ACK={code}")
    time.sleep(0.5)


def get_header(ser: serial.Serial, int_size: int):
    raw = ser.read(3 + int_size)
    if len(raw) != 3 + int_size:
        raise RuntimeError("Timeout while reading block header.")
    if raw[0:2] != b"#0":
        raise RuntimeError(f"Bad block preamble: {raw!r}")
    header = raw[2]
    size = get_uint(raw[3:3 + int_size])
    return header, size


def read_exact(ser, total_size, overall_timeout=20.0, label="data"):
    buf = bytearray()
    start = time.time()
    print(f"Reading {total_size} bytes of {label}...")
    while len(buf) < total_size:
        chunk = ser.read(total_size - len(buf))
        if chunk:
            buf.extend(chunk)
            print(f"  received {len(buf)}/{total_size} bytes")
        else:
            if time.time() - start > overall_timeout:
                raise RuntimeError(
                    f"Timeout while reading {label}. Got {len(buf)}/{total_size} bytes."
                )
    return bytes(buf)


def get_block_data(ser, size):
    raw = read_exact(ser, size + 1, overall_timeout=20.0, label="block data + checksum")
    payload = raw[:-1]
    check = raw[-1]
    if not checksum_ok(payload, check):
        raise RuntimeError(
            f"Checksum failed. Calculated payload checksum does not match received byte {check!r}."
        )
    return payload


def query_waveform(ser: serial.Serial, trace_no: str):
    print(f"\nRequesting waveform with QW {trace_no} ...")
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
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
    delta_x = get_float3(admin[24:27])
    y_at_0 = get_float3(admin[27:30])

    fmt = sample_data[0]
    signed_vals = (fmt & 0b10000000) != 0
    group_bits = fmt & 0b01110000
    sample_size = fmt & 0b00000111

    if sample_size not in (1, 2, 4):
        raise RuntimeError(f"Unexpected numeric sample width: {sample_size}")

    get_num = get_int if signed_vals else get_uint

    samples_per_point = 1
    if group_bits == 0b01000000:
        samples_per_point = 2
    elif group_bits in (0b01100000, 0b01110000):
        samples_per_point = 3

    p = 1
    overload = get_num(sample_data[p:p + sample_size]); p += sample_size
    underload = get_num(sample_data[p:p + sample_size]); p += sample_size
    invalid = get_num(sample_data[p:p + sample_size]); p += sample_size
    n_points = get_uint(sample_data[p:p + 2]); p += 2

    raw = np.empty((n_points, samples_per_point), dtype=float)

    for i in range(n_points):
        for j in range(samples_per_point):
            val = get_num(sample_data[p:p + sample_size])
            p += sample_size
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
    n = len(y)

    y_ac = y - np.mean(y)
    window = np.hanning(n)
    y_win = y_ac * window

    spec = np.fft.rfft(y_win)
    freq = np.fft.rfftfreq(n, d=dt)
    amp = (2.0 / np.sum(window)) * np.abs(spec)

    return freq, amp, 1.0 / dt


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

    vrms = float(np.sqrt(np.mean(v ** 2)))
    irms = float(np.sqrt(np.mean(i ** 2)))
    real_power_w = float(np.mean(v * i))
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
        "vrms_v": vrms,
        "irms_a": irms,
        "real_power_w": real_power_w,
        "apparent_power_va": apparent_power_va,
        "reactive_power_var": reactive_power_var,
        "power_factor": power_factor,
        "phase_i_minus_v_deg": phase_i_minus_v_deg,
        "phase_note": phase_note,
    }


def save_waveform_plot(path, title, wf_a, wf_b):
    plt.figure(figsize=(10, 6))
    plt.plot(wf_a["x"], wf_a["y"], label=f"Channel A Voltage ({wf_a['y_unit']})")
    plt.plot(wf_b["x"], wf_b["y"], label=f"Channel B Current ({wf_b['y_unit']})")
    plt.xlabel(f"Time ({wf_a['x_unit']})")
    plt.ylabel("Amplitude")
    plt.title(title)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def save_fft_plot(path, title, freq_a, amp_a, freq_b, amp_b, unit_a, unit_b):
    plt.figure(figsize=(10, 6))
    plt.plot(freq_a, amp_a, label=f"Channel A Voltage FFT ({unit_a})")
    plt.plot(freq_b, amp_b, label=f"Channel B Current FFT ({unit_b})")
    plt.xlim(left=0)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Amplitude")
    plt.title(f"{title} FFT")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def save_single_waveform_plot(path, title, wf):
    plt.figure(figsize=(10, 6))
    plt.plot(wf["x"], wf["y"], label=f"Current ({wf['y_unit']})")
    plt.xlabel(f"Time ({wf['x_unit']})")
    plt.ylabel("Amplitude")
    plt.title(title)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def save_single_fft_plot(path, title, freq, amp, unit):
    plt.figure(figsize=(10, 6))
    plt.plot(freq, amp, label=f"FFT ({unit})")
    plt.xlim(left=0)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Amplitude")
    plt.title(title)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def summary_text(ident, port_name, baudrate, power, wf_a, dom_a, ampdom_a, wf_b, dom_b, ampdom_b, thd_a, thd_b):
    lines = [
        "FLUKE CURRENT DISPLAY ANALYSIS",
        "==============================",
        "",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scope ID: {ident}",
        f"COM Port: {port_name}",
        f"Baud Rate: {baudrate}",
        f"Site: {SITE_NAME}",
        f"Equipment / Load: {EQUIPMENT_NAME}",
        f"Technician: {TECHNICIAN}",
        f"Notes: {NOTES}",
        "",
        "Channel Assignment",
        "------------------",
        f"Channel A = Voltage ({wf_a['y_unit']})",
        f"Channel B = Current ({wf_b['y_unit']})",
        "",
        "Channel A Voltage",
        f"RMS: {power['vrms_v']:.6f} V",
        f"Dominant frequency: {dom_a:.6f} Hz",
        f"Dominant amplitude: {ampdom_a:.6f} {wf_a['y_unit']}",
        f"THD estimate: {thd_a:.6f}",
        "",
        "Channel B Current",
        f"RMS: {power['irms_a']:.6f} A",
        f"Dominant frequency: {dom_b:.6f} Hz",
        f"Dominant amplitude: {ampdom_b:.6f} {wf_b['y_unit']}",
        f"THD estimate: {thd_b:.6f}",
        "",
        "Power Analysis",
        "--------------",
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


def neutral_summary_text(ident, port_name, baudrate, wf_b, dom_b, ampdom_b, thd_b):
    y = np.asarray(wf_b["y"], dtype=float)
    y = y[np.isfinite(y)]
    irms = float(np.sqrt(np.mean(y ** 2))) if len(y) else float("nan")
    lines = [
        "FLUKE CURRENT DISPLAY ANALYSIS",
        "==============================",
        "",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scope ID: {ident}",
        f"COM Port: {port_name}",
        f"Baud Rate: {baudrate}",
        f"Site: {SITE_NAME}",
        f"Equipment / Load: {EQUIPMENT_NAME}",
        f"Technician: {TECHNICIAN}",
        f"Notes: {NOTES}",
        "",
        "Channel Assignment",
        "------------------",
        "Channel A = Not captured",
        f"Channel B = Neutral Current ({wf_b['y_unit']})",
        "",
        "Neutral Current",
        "---------------",
        f"RMS: {irms:.6f} A",
        f"Dominant frequency: {dom_b:.6f} Hz",
        f"Dominant amplitude: {ampdom_b:.6f} {wf_b['y_unit']}",
        f"THD estimate: {thd_b:.6f}",
    ]
    return "\n".join(lines), irms


def write_csv(csv_path, row):
    headers = [
        "mode", "vrms_v", "irms_a", "real_power_w", "apparent_power_va", "reactive_power_var",
        "power_factor", "phase_i_minus_v_deg", "dominant_freq_v_hz", "dominant_freq_i_hz",
        "thd_v", "thd_i", "waveform_png", "fft_png", "summary_txt",
        "scope_id", "port_name", "baudrate",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerow(row)


def return_scope_to_local(ser):
    global _RETURNED_TO_LOCAL
    if ser is None or _RETURNED_TO_LOCAL:
        return
    try:
        print("\n>> GL  (return ScopeMeter to local control)")
        send_command(ser, "GL")
        _RETURNED_TO_LOCAL = True
    except Exception as e:
        print(f"Warning: could not return ScopeMeter to local mode: {e}")


def cleanup_scope():
    global _ACTIVE_SER
    if _ACTIVE_SER is not None:
        try:
            return_scope_to_local(_ACTIVE_SER)
        finally:
            try:
                _ACTIVE_SER.close()
            except Exception:
                pass
            _ACTIVE_SER = None


atexit.register(cleanup_scope)


def main():
    global _ACTIVE_SER, _RETURNED_TO_LOCAL

    print("Fluke Current Display Analyzer starting...")
    os.makedirs(SAVE_ROOT, exist_ok=True)
    export_dir = os.path.join(
        SAVE_ROOT,
        "display_analysis_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    os.makedirs(export_dir, exist_ok=True)

    ser = None
    _RETURNED_TO_LOCAL = False

    try:
        ser, ident, port_name, baudrate = connect_scope(PORT)
        _ACTIVE_SER = ser
        maybe_recall(ser)

        wf_a = None
        try:
            wf_a = query_waveform(ser, "10")
        except RuntimeError as e:
            print(f"Channel A not available: {e}")

        wf_b = query_waveform(ser, "20")

        if wf_a is not None:
            print(f"Detected units: A={wf_a['y_unit']}  B={wf_b['y_unit']}")
        else:
            print(f"Detected units: A=NOT AVAILABLE  B={wf_b['y_unit']}")

        freq_b, amp_b, _ = compute_fft(wf_b["x"], wf_b["y"])
        dom_b, ampdom_b, _ = dominant_frequency(freq_b, amp_b)
        thd_b, _, _, _ = thd_from_fft(freq_b, amp_b)

        waveform_png = os.path.join(export_dir, "current_display_waveforms.png")
        fft_png = os.path.join(export_dir, "current_display_fft.png")
        summary_txt = os.path.join(export_dir, "CURRENT_DISPLAY_REPORT.txt")
        csv_path = os.path.join(export_dir, "current_display_summary.csv")

        if wf_a is not None:
            freq_a, amp_a, _ = compute_fft(wf_a["x"], wf_a["y"])
            dom_a, ampdom_a, _ = dominant_frequency(freq_a, amp_a)
            thd_a, _, _, _ = thd_from_fft(freq_a, amp_a)

            power = analyze_voltage_current(
                wf_a["y"],
                wf_b["y"],
                wf_a["delta_x"],
                CURRENT_SCALE_A_PER_V,
            )

            save_waveform_plot(waveform_png, "Current Display", wf_a, wf_b)
            save_fft_plot(
                fft_png, "Current Display", freq_a, amp_a, freq_b, amp_b, wf_a["y_unit"], wf_b["y_unit"]
            )

            with open(summary_txt, "w", encoding="utf-8") as f:
                f.write(summary_text(
                    ident, port_name, baudrate, power,
                    wf_a, dom_a, ampdom_a,
                    wf_b, dom_b, ampdom_b,
                    thd_a, thd_b,
                ))

            row = {
                "mode": "voltage_current",
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
                "waveform_png": os.path.basename(waveform_png),
                "fft_png": os.path.basename(fft_png),
                "summary_txt": os.path.basename(summary_txt),
                "scope_id": ident,
                "port_name": port_name,
                "baudrate": baudrate,
            }
        else:
            save_single_waveform_plot(waveform_png, "Neutral Current", wf_b)
            save_single_fft_plot(fft_png, "Neutral FFT", freq_b, amp_b, wf_b["y_unit"])

            text, irms = neutral_summary_text(ident, port_name, baudrate, wf_b, dom_b, ampdom_b, thd_b)
            with open(summary_txt, "w", encoding="utf-8") as f:
                f.write(text)

            row = {
                "mode": "current_only",
                "vrms_v": "",
                "irms_a": irms,
                "real_power_w": "",
                "apparent_power_va": "",
                "reactive_power_var": "",
                "power_factor": "",
                "phase_i_minus_v_deg": "",
                "dominant_freq_v_hz": "",
                "dominant_freq_i_hz": dom_b,
                "thd_v": "",
                "thd_i": thd_b,
                "waveform_png": os.path.basename(waveform_png),
                "fft_png": os.path.basename(fft_png),
                "summary_txt": os.path.basename(summary_txt),
                "scope_id": ident,
                "port_name": port_name,
                "baudrate": baudrate,
            }

        write_csv(csv_path, row)

        print(f"\nSaved export folder: {export_dir}")
        print(f"Saved waveform plot: {waveform_png}")
        print(f"Saved FFT plot: {fft_png}")
        print(f"Saved text report: {summary_txt}")
        print(f"Saved CSV summary: {csv_path}")

    except Exception:
        print("\nERROR:")
        traceback.print_exc()
        raise

    finally:
        if ser is not None:
            return_scope_to_local(ser)
            try:
                ser.close()
            except Exception:
                pass
            if _ACTIVE_SER is ser:
                _ACTIVE_SER = None
        print("\nSerial port closed.")


if __name__ == "__main__":
    main()
