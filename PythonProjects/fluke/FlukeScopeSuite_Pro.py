import serial
import time
import threading
import subprocess
import csv
import io
import math
from pathlib import Path
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from serial.tools import list_ports
import numpy as np
from scipy.fft import fft, fftfreq, rfft, rfftfreq
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from datetime import datetime


APP_NAME = "Fluke ScopeSuite Pro"
BAUD = 1200

# Units lookup from Fluke protocol
UNITS = [
    None, "V", "A", "Ohm", "W", "F", "K", "s", "h", "days",
    "Hz", "deg", "degC", "degF", "%", "dBm50", "dBm600",
    "dBV", "dBA", "dBW", "VAR", "VA"
]

# Channel configuration
CHANNEL_A_ROLE = "voltage"
CHANNEL_B_ROLE = "current"
CHANNEL_A_LABEL = "Channel A Voltage"
CHANNEL_B_LABEL = "Channel B Current"
CHANNEL_A_DISPLAY_UNIT = "V"
CHANNEL_B_DISPLAY_UNIT = "A"
CURRENT_SCALE_A_PER_V = 1.0
TIMEBASE_CORRECTION = 1.0


def get_uint(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=False)


def get_int(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=True)


def get_float3(data: bytes) -> float:
    """Fluke 3-byte float: 2-byte mantissa + 1-byte exponent"""
    mantissa = get_int(data[0:2])
    exponent = get_int(data[2:3])
    return float(mantissa * (10.0 ** exponent))


def checksum_ok(data: bytes, check: int) -> bool:
    total = 0
    for b in data:
        total = (total + b) % 256
    return total == check


def parse_advanced_waveform(admin: bytes, sample_data: bytes) -> dict:
    """
    Advanced waveform parser (ported from Windows script).
    Handles format codes, min/max/avg modes, overload/underload markers.
    """
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
        "role": None,
        "display_label": None,
        "display_unit": None,
    }


def compute_fft(x, y):
    """Compute FFT with windowing"""
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

    spec = rfft(y_win)
    freq = rfftfreq(n, d=dt)
    amp = (2.0 / np.sum(window)) * np.abs(spec)

    return freq, amp, fs


def dominant_frequency(freq, amp):
    """Find dominant frequency"""
    if len(freq) <= 1:
        return float("nan"), float("nan"), -1
    idx = np.argmax(amp[1:]) + 1
    return float(freq[idx]), float(amp[idx]), int(idx)


def thd_from_fft(freq, amp, max_harmonic=15):
    """Compute THD and individual harmonics"""
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
        if abs(freq[idx] - target) < f1 * 0.1:
            an = float(amp[idx])
            harmonics[n] = an
            sum_sq += an * an

    thd = (np.sqrt(sum_sq) / a1) * 100 if a1 > 0 else float("nan")
    return thd, f1, a1, harmonics


def wrap_phase_deg(angle_deg: float) -> float:
    return ((angle_deg + 180.0) % 360.0) - 180.0


def analyze_voltage_current(voltage_samples, current_samples, dt, current_scale_a_per_v):
    """Advanced power analysis: real/reactive/apparent power, PF, phase"""
    v = np.asarray(voltage_samples, dtype=float)
    i_raw = np.asarray(current_samples, dtype=float)

    finite = np.isfinite(v) & np.isfinite(i_raw)
    v = v[finite]
    i_raw = i_raw[finite]

    if len(v) == 0 or len(i_raw) == 0:
        raise ValueError("V or I array empty after removing NaN.")

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

    # Phase angle via FFT
    v_for_fft = v - np.mean(v)
    i_for_fft = i - np.mean(i)
    window = np.hanning(n)

    V = fft(v_for_fft * window)
    I = fft(i_for_fft * window)
    freqs = fftfreq(n, d=dt)

    if len(freqs) < 2:
        raise ValueError("Not enough samples for phase analysis.")

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
        phase_note = "Nearly in phase"

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


class FlukeScopeSuitePro:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1000x900")

        self.outdir = Path("/Users/jamesgregg/PythonProjects/fluke/captures")
        self.outdir.mkdir(exist_ok=True, parents=True)

        self.last_png = None
        self.last_pdf = None
        self.last_csv = None
        self.image_ref = None
        self.analysis_results = {}
        self.replay_session = None

        self.build_ui()
        self.refresh_ports()

    def build_ui(self):
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, font=("Arial", 20, "bold")).pack(side="left")

        port_frame = ttk.Frame(self.root, padding=10)
        port_frame.pack(fill="x")

        ttk.Label(port_frame, text="Serial Port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, width=40)
        self.port_combo.pack(side="left", padx=8)

        ttk.Button(port_frame, text="Refresh Ports", command=self.refresh_ports).pack(side="left", padx=4)
        ttk.Button(port_frame, text="Choose Save Folder", command=self.choose_folder).pack(side="left", padx=4)
        ttk.Button(port_frame, text="Open Folder", command=self.open_folder).pack(side="left", padx=4)

        action_frame = ttk.Frame(self.root, padding=10)
        action_frame.pack(fill="x")

        ttk.Button(action_frame, text="Test Connection", command=self.test_id_thread).pack(side="left", padx=5)
        ttk.Button(action_frame, text="Capture Single", command=self.capture_thread).pack(side="left", padx=5)
        ttk.Button(action_frame, text="Capture Replay", command=self.capture_replay_thread).pack(side="left", padx=5)
        ttk.Button(action_frame, text="Open Last Report", command=self.open_last_report).pack(side="left", padx=5)

        ttk.Separator(self.root).pack(fill="x", padx=10, pady=5)

        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(body, width=300)
        right.pack(side="right", fill="y", padx=(10, 0))

        self.image_label = tk.Label(left, bg="white", relief="sunken")
        self.image_label.pack(fill="both", expand=True)

        ttk.Label(right, text="Session Info", font=("Arial", 13, "bold")).pack(anchor="w")
        self.info_var = tk.StringVar(value="Ready")
        ttk.Label(right, textvariable=self.info_var, wraplength=280).pack(anchor="w", pady=8)

        ttk.Label(right, text="Analysis Results", font=("Arial", 12, "bold")).pack(anchor="w", pady=(12, 4))
        self.results_var = tk.StringVar(value="None yet")
        ttk.Label(right, textvariable=self.results_var, wraplength=280, justify="left", font=("Arial", 9)).pack(anchor="w")

        ttk.Label(right, text="Features", font=("Arial", 11, "bold")).pack(anchor="w", pady=(12, 4))
        features = (
            "✓ Screen capture\n"
            "✓ Single-waveform FFT/THD\n"
            "✓ Dual-channel analysis\n"
            "✓ Power metrics (P/Q/S/PF)\n"
            "✓ Replay loop (multi-frame)\n"
            "✓ Harmonic tracking\n"
            "✓ Trend plots\n"
            "✓ PDF reports"
        )
        ttk.Label(right, text=features, justify="left", font=("Arial", 8)).pack(anchor="w")

        log_frame = ttk.Frame(self.root, padding=10)
        log_frame.pack(fill="both", expand=True)

        ttk.Label(log_frame, text="Log").pack(anchor="w")
        self.log = tk.Text(log_frame, height=6)
        self.log.pack(fill="both", expand=True)

    def log_msg(self, msg):
        self.root.after(0, lambda: self._log_msg(msg))

    def _log_msg(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{timestamp}] {msg}\n")
        self.log.see("end")
        self.info_var.set(msg)

    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports() if p.device.startswith("/dev/cu.")]
        self.port_combo["values"] = ports
        preferred = [p for p in ports if "usbserial" in p.lower() or "usbmodem" in p.lower()]
        if preferred:
            self.port_var.set(preferred[0])
        elif ports:
            self.port_var.set(ports[0])
        self.log_msg("Ports refreshed")

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.outdir))
        if folder:
            self.outdir = Path(folder)
            self.outdir.mkdir(exist_ok=True)
            self.log_msg(f"Save folder: {self.outdir}")

    def open_folder(self):
        subprocess.run(["open", str(self.outdir)])

    def open_last_report(self):
        if self.last_pdf and self.last_pdf.exists():
            subprocess.run(["open", str(self.last_pdf)])
        else:
            messagebox.showinfo("No Report", "No PDF report yet.")

    def open_serial(self):
        port = self.port_var.get().strip()
        if not port:
            raise RuntimeError("No serial port selected.")
        return serial.Serial(
            port=port, baudrate=BAUD, bytesize=8, parity="N", stopbits=1,
            timeout=2, write_timeout=2, xonxoff=False, rtscts=False, dsrdtr=False,
        )

    def read_until_cr(self, ser):
        data = bytearray()
        while True:
            b = ser.read(1)
            if not b:
                raise TimeoutError("Timeout waiting for CR.")
            if b == b"\r":
                return bytes(data)
            data.extend(b)

    def send_cmd(self, ser, cmd):
        self.log_msg(f"TX: {cmd}")
        ser.reset_input_buffer()
        ser.write(cmd.encode("ascii") + b"\r")
        ser.flush()
        ack = self.read_until_cr(ser)
        if ack != b"0":
            raise RuntimeError(f"Command rejected: ACK={ack!r}")

    def get_qp_data(self, ser):
        digits = bytearray()
        while True:
            b = ser.read(1)
            if not b:
                raise TimeoutError("Timeout reading QP byte count.")
            if b == b",":
                break
            if 48 <= b[0] <= 57:
                digits.extend(b)
        if not digits:
            raise RuntimeError("No QP byte count.")
        total = int(digits.decode("ascii"))
        self.log_msg(f"Receiving {total} screen bytes")
        data = bytearray()
        while len(data) < total:
            chunk = ser.read(min(512, total - len(data)))
            if chunk:
                data.extend(chunk)
            else:
                time.sleep(0.05)
        return bytes(data)

    def get_header(self, ser, int_size: int):
        raw = ser.read(3 + int_size)
        if len(raw) != 3 + int_size:
            raise RuntimeError("Timeout reading block header.")
        if raw[0:2] != b"#0":
            raise RuntimeError(f"Bad preamble: {raw!r}")
        header = raw[2]
        size = get_uint(raw[3:3 + int_size])
        return header, size

    def get_block_data(self, ser, size: int) -> bytes:
        raw = ser.read(size + 1)
        if len(raw) != size + 1:
            raise RuntimeError("Timeout reading block data.")
        payload = raw[:-1]
        check = raw[-1]
        if not checksum_ok(payload, check):
            raise RuntimeError("Checksum failed.")
        return payload

    def query_measurements(self, ser):
        """Query active measurements (QM) - most accurate, calculated by scope"""
        try:
            self.log_msg("Querying measurements (QM)...")
            ser.reset_input_buffer()
            ser.write(b"QM\r")
            ser.flush()

            ack = self.read_until_cr(ser)
            if ack != b"0":
                self.log_msg(f"QM rejected: {ack!r}")
                return {}

            # Read all measurement lines until CR
            measurements = {}
            while True:
                line = self.read_until_cr(ser)
                if not line:
                    break
                line_str = line.decode("ascii", errors="replace").strip()
                if not line_str:
                    break
                # Parse: <no>,<valid>,<source>,<unit>,<type>,<pres>,<resol>
                parts = line_str.split(",")
                if len(parts) >= 7:
                    try:
                        meas_no = int(parts[0])
                        valid = int(parts[1])
                        unit_code = int(parts[3])
                        unit = UNITS[unit_code] if unit_code < len(UNITS) else "?"
                        # Try to parse value
                        try:
                            value = float(parts[6]) if parts[6] else 0
                            measurements[f"meas_{meas_no}"] = {
                                "value": value,
                                "unit": unit,
                                "valid": valid
                            }
                        except:
                            pass
                    except:
                        pass

            if measurements:
                self.log_msg(f"✓ Got {len(measurements)} measurements")
            return measurements
        except Exception as e:
            self.log_msg(f"QM query failed: {e}")
            return {}

    def query_setup(self, ser):
        """Query setup configuration (QS)"""
        try:
            self.log_msg("Querying setup (QS)...")
            ser.reset_input_buffer()
            ser.write(b"QS\r")
            ser.flush()

            ack = self.read_until_cr(ser)
            if ack != b"0":
                return None

            # Setup is binary block
            setup_header, setup_size = self.get_header(ser, 2)
            setup_data = self.get_block_data(ser, setup_size)
            self.log_msg(f"✓ Got setup: {setup_size} bytes")
            return setup_data
        except Exception as e:
            self.log_msg(f"QS query failed: {e}")
            return None

    def get_all_waveforms_safe(self, ser, traces=[10, 20]):
        """Try to get all waveforms with timeout and retry"""
        waveforms = {}
        for trace_no in traces:
            # Try up to 2 times
            for attempt in range(2):
                try:
                    self.log_msg(f"QW {trace_no} (attempt {attempt+1})...")
                    wf = self.get_qw_data_advanced(ser, str(trace_no))
                    if wf:
                        waveforms[trace_no] = wf
                        break
                    time.sleep(0.5)
                except Exception as e:
                    if attempt == 1:
                        self.log_msg(f"QW {trace_no}: giving up after 2 attempts")
                    else:
                        self.log_msg(f"QW {trace_no}: retry...")
                    time.sleep(0.5)

        return waveforms

    def get_qw_data_advanced(self, ser, trace_no: str):
        """Query waveform with advanced binary protocol"""
        try:
            self.log_msg(f"Querying waveform QW {trace_no}...")
            ser.reset_input_buffer()
            ser.write(f"QW {trace_no}\r".encode("ascii"))
            ser.flush()

            # Read ACK with timeout
            deadline = time.time() + 5.0  # 5 second timeout for ACK
            ack = bytearray()
            while time.time() < deadline:
                b = ser.read(1)
                if b:
                    ack.extend(b)
                    if b == b"\r":
                        break

            if not ack or ack != b"0\r":
                raise RuntimeError(f"QW {trace_no} rejected or timeout. ACK={ack!r}")

            # Read admin header
            admin_header, admin_size = self.get_header(ser, 2)
            admin = self.get_block_data(ser, admin_size)

            comma = ser.read(1)
            if comma != b",":
                raise RuntimeError(f"Expected comma, got {comma!r}")

            # Read sample header
            sample_header, sample_size = self.get_header(ser, 4)
            sample_data = self.get_block_data(ser, sample_size)

            # Read terminator
            term = ser.read(1)
            if term != b"\r":
                raise RuntimeError(f"Expected CR, got {term!r}")

            waveform = parse_advanced_waveform(admin, sample_data)
            self.log_msg(f"✓ Parsed {waveform['n_points']} samples from {trace_no}")
            return waveform
        except Exception as e:
            self.log_msg(f"QW {trace_no} failed (graceful fallback): {e}")
            return None

    def query_replay_status(self, ser):
        """Query replay status (RP command)"""
        try:
            self.send_cmd(ser, "RP")
            resp = self.read_until_cr(ser).decode("ascii").strip()
            parts = [p.strip() for p in resp.split(",")]
            if len(parts) != 2:
                raise RuntimeError(f"Bad RP response: {resp}")
            nr_screens = int(parts[0])
            screen_index = int(parts[1])
            return nr_screens, screen_index
        except Exception as e:
            self.log_msg(f"Replay query failed: {e}")
            return 0, 0

    def select_replay_frame(self, ser, index: int):
        """Select replay frame (RP idx command)"""
        self.send_cmd(ser, f"RP {index}")

    def decode_printer_stream_to_png(self, raw_bytes, outfile):
        bands = []
        i = 0
        while i < len(raw_bytes) - 5:
            if raw_bytes[i] == 27 and raw_bytes[i + 1] == ord("*"):
                mode = raw_bytes[i + 2]
                n1 = raw_bytes[i + 3]
                n2 = raw_bytes[i + 4]
                columns = n1 + 256 * n2

                if mode in (0, 1, 4):
                    bytes_per_column, band_height = 1, 8
                elif mode in (32, 33):
                    bytes_per_column, band_height = 3, 24
                else:
                    i += 1
                    continue

                start, end = i + 5, i + 5 + columns * bytes_per_column
                if end > len(raw_bytes):
                    break

                block = raw_bytes[start:end]
                band = Image.new("1", (columns, band_height), 1)

                for x in range(columns):
                    for byte_index in range(bytes_per_column):
                        value = block[x * bytes_per_column + byte_index]
                        for bit in range(8):
                            if value & (1 << (7 - bit)):
                                y = byte_index * 8 + bit
                                band.putpixel((x, y), 0)

                bands.append(band)
                i = end
            else:
                i += 1

        if not bands:
            raise RuntimeError("No ESC/P bands found.")

        width = max(b.width for b in bands)
        height = sum(b.height for b in bands)
        final = Image.new("1", (width, height), 1)
        y = 0
        for band in bands:
            final.paste(band, (0, y))
            y += band.height

        final.save(outfile)
        self.log_msg(f"PNG saved: {width}x{height}")

    def test_id_thread(self):
        threading.Thread(target=self.test_connection, daemon=True).start()

    def test_connection(self):
        try:
            with self.open_serial() as ser:
                self.send_cmd(ser, "GR")
                self.send_cmd(ser, "ID")
                ident = self.read_until_cr(ser)
                self.log_msg("Instrument: " + ident.decode(errors="replace"))
        except Exception as e:
            self.log_msg(f"ERROR: {e}")
            messagebox.showerror("Connection Error", str(e))

    def capture_thread(self):
        threading.Thread(target=self.capture_single, daemon=True).start()

    def capture_single(self):
        """Single frame capture with advanced dual-channel analysis"""
        try:
            self.log_msg("Starting single-frame capture...")
            ts = time.strftime("%Y%m%d_%H%M%S")
            session_dir = self.outdir / f"single_{ts}"
            session_dir.mkdir(exist_ok=True)

            with self.open_serial() as ser:
                self.send_cmd(ser, "GR")
                self.send_cmd(ser, "ID")
                ident = self.read_until_cr(ser).decode(errors="replace")
                self.log_msg(f"Instrument: {ident}")

                # Try to get both channels
                wf_a = self.get_qw_data_advanced(ser, "10")
                wf_b = self.get_qw_data_advanced(ser, "20")

                # Also capture screen
                self.send_cmd(ser, "QP")
                raw_qp = self.get_qp_data(ser)

            # Save outputs
            if wf_a:
                self._save_single_frame_analysis(session_dir, wf_a, wf_b, raw_qp, ident, "frame_00")

            self.log_msg("Single capture complete")
        except Exception as e:
            self.log_msg(f"ERROR: {e}")
            messagebox.showerror("Capture Error", str(e))

    def _save_single_frame_analysis(self, session_dir, wf_a, wf_b, raw_qp, ident, frame_name):
        """Analyze and save single frame"""
        try:
            # Set channel roles
            wf_a["role"] = CHANNEL_A_ROLE
            wf_a["display_label"] = CHANNEL_A_LABEL
            wf_a["display_unit"] = CHANNEL_A_DISPLAY_UNIT

            # FFT and THD for channel A
            freq_a, mag_a, fs_a = compute_fft(wf_a["x"], wf_a["y"])
            thd_a, f1_a, amp_a, harm_a = thd_from_fft(freq_a, mag_a)
            self.log_msg(f"Ch A FFT: f1={f1_a:.1f} Hz, THD={thd_a:.1f}%")

            # Dual-channel analysis if available
            power_info = None
            if wf_b:
                wf_b["role"] = CHANNEL_B_ROLE
                wf_b["display_label"] = CHANNEL_B_LABEL
                wf_b["display_unit"] = CHANNEL_B_DISPLAY_UNIT

                freq_b, mag_b, fs_b = compute_fft(wf_b["x"], wf_b["y"])
                thd_b, f1_b, amp_b, harm_b = thd_from_fft(freq_b, mag_b)

                # Power analysis
                try:
                    power_info = analyze_voltage_current(wf_a["y"], wf_b["y"], wf_a["delta_x"], CURRENT_SCALE_A_PER_V)
                    self.log_msg(f"Power: P={power_info['real_power_w']:.1f}W, PF={power_info['power_factor']:.3f}")
                except Exception as pe:
                    self.log_msg(f"Power analysis failed: {pe}")

            # Save plots
            self._save_frame_plots(session_dir, frame_name, wf_a, wf_b, freq_a, mag_a, freq_b=None, mag_b=None)

            # Save PNG from screen capture
            if raw_qp:
                png_file = session_dir / f"{frame_name}_screen.png"
                self.decode_printer_stream_to_png(raw_qp, png_file)
                self.show_image(png_file)

            # Update results display
            self.analysis_results = {
                "f1_hz": f"{f1_a:.1f}",
                "thd": f"{thd_a:.1f}%",
                "frames": 1,
            }
            if power_info:
                self.analysis_results.update({
                    "real_power_w": f"{power_info['real_power_w']:.1f}",
                    "power_factor": f"{power_info['power_factor']:.3f}",
                })
            self._update_results_display()

        except Exception as e:
            self.log_msg(f"Frame analysis failed: {e}")

    def _save_frame_plots(self, session_dir, frame_name, wf_a, wf_b, freq_a, mag_a, freq_b=None, mag_b=None):
        """Save waveform and FFT plots"""
        try:
            # Waveform plot
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(wf_a["x"], wf_a["y"], label=f"{wf_a['display_label']} ({wf_a['display_unit']})", linewidth=2)
            if wf_b:
                ax.plot(wf_b["x"], wf_b["y"], label=f"{wf_b['display_label']} ({wf_b['display_unit']})", linewidth=2)
            ax.set_xlabel(f"Time ({wf_a['x_unit']})")
            ax.set_ylabel("Amplitude")
            ax.grid(True, alpha=0.3)
            ax.legend()
            ax.set_title(frame_name)
            plt.tight_layout()
            plt.savefig(session_dir / f"{frame_name}_waveforms.png", dpi=150)
            plt.close(fig)

            # FFT plot
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(freq_a[:min(1000, len(freq_a))], mag_a[:min(1000, len(mag_a))], label=f"{CHANNEL_A_LABEL} FFT", linewidth=2)
            if freq_b is not None and mag_b is not None:
                ax.plot(freq_b[:min(1000, len(freq_b))], mag_b[:min(1000, len(mag_b))], label=f"{CHANNEL_B_LABEL} FFT", linewidth=2)
            ax.set_xlabel("Frequency (Hz)")
            ax.set_ylabel("Magnitude")
            ax.set_xlim(left=0)
            ax.grid(True, alpha=0.3)
            ax.legend()
            ax.set_title(f"{frame_name} FFT")
            plt.tight_layout()
            plt.savefig(session_dir / f"{frame_name}_fft.png", dpi=150)
            plt.close(fig)
        except Exception as e:
            self.log_msg(f"Plot save failed: {e}")

    def capture_replay_thread(self):
        threading.Thread(target=self.capture_replay_session, daemon=True).start()

    def capture_replay_session(self):
        """Capture and analyze all replay frames (multi-frame session)"""
        try:
            self.log_msg("Starting replay session capture...")
            ts = time.strftime("%Y%m%d_%H%M%S")
            session_dir = self.outdir / f"replay_{ts}"
            session_dir.mkdir(exist_ok=True)

            frames_data = []
            with self.open_serial() as ser:
                self.send_cmd(ser, "GR")
                self.send_cmd(ser, "ID")
                ident = self.read_until_cr(ser).decode(errors="replace")
                self.log_msg(f"Instrument: {ident}")

                # Query replay status
                nr_frames, current_idx = self.query_replay_status(ser)
                if nr_frames <= 0:
                    self.log_msg("No replay frames available")
                    return

                self.log_msg(f"Replay has {nr_frames} frames")

                # Iterate through replay frames
                replay_indices = list(range(-(nr_frames - 1), 1))
                for frame_idx, idx in enumerate(replay_indices):
                    frame_name = f"frame_{str(frame_idx).zfill(2)}"
                    self.log_msg(f"Processing {frame_name} (RP {idx})...")

                    try:
                        self.select_replay_frame(ser, idx)
                        time.sleep(1.0)

                        try:
                            ser.reset_input_buffer()
                            ser.reset_output_buffer()
                        except:
                            pass

                        # Priority order for most accurate automatic capture:
                        # 1. Measurements (QM) - most accurate, scope-calculated
                        measurements = self.query_measurements(ser)

                        # 2. Setup (QS) - configuration/calibration info
                        setup = self.query_setup(ser)

                        # 3. Waveforms (QW) - raw data for analysis (might timeout)
                        waveforms = self.get_all_waveforms_safe(ser, traces=[10, 20])
                        wf_a = waveforms.get(10)
                        wf_b = waveforms.get(20)

                        # 4. Screen capture (QP) - most reliable fallback
                        try:
                            self.send_cmd(ser, "QP")
                            raw_qp = self.get_qp_data(ser)
                            self.log_msg(f"✓ Screen captured")
                        except Exception as qp_err:
                            self.log_msg(f"⚠ Screen capture failed: {qp_err}")
                            raw_qp = None

                        # Analyze with all available data
                        frame_result = self._analyze_replay_frame(session_dir, wf_a, wf_b, raw_qp, frame_name, measurements)
                        frames_data.append(frame_result)
                        self.log_msg(f"✓ {frame_name} complete (M:{len(measurements)} W:{sum(1 for x in [wf_a,wf_b] if x)} QP:{bool(raw_qp)})")

                    except Exception as e:
                        self.log_msg(f"Frame {frame_name} error: {e}")
                        # Continue to next frame instead of stopping

            # Generate global reports
            if frames_data:
                self._generate_replay_reports(session_dir, frames_data, ident, nr_frames)
            self.log_msg("Replay session complete")

        except Exception as e:
            self.log_msg(f"ERROR: {e}")
            messagebox.showerror("Replay Error", str(e))

    def _analyze_replay_frame(self, session_dir, wf_a, wf_b, raw_qp, frame_name, measurements=None):
        """Analyze single replay frame with all available data sources"""
        frame_result = {"frame_name": frame_name}
        if measurements is None:
            measurements = {}

        try:
            # Use scope measurements if available (most accurate)
            if measurements:
                self.log_msg(f"Using scope measurements: {measurements}")
                # Extract key values from scope measurements
                for key, meas_data in measurements.items():
                    if isinstance(meas_data, dict):
                        frame_result[key] = meas_data.get("value", np.nan)

            # Analyze waveforms if available (for FFT/THD)
            if wf_a:
                wf_a["role"] = CHANNEL_A_ROLE
                wf_a["display_label"] = CHANNEL_A_LABEL
                wf_a["display_unit"] = CHANNEL_A_DISPLAY_UNIT

                freq_a, mag_a, fs_a = compute_fft(wf_a["x"], wf_a["y"])
                thd_a, f1_a, amp_a, harm_a = thd_from_fft(freq_a, mag_a)
                frame_result.update({
                    "f1_hz": f1_a,
                    "thd_v": thd_a,
                    "vrms_v": float(np.sqrt(np.mean(wf_a["y"] ** 2))),
                })
                self.log_msg(f"Ch A: f1={f1_a:.1f}Hz, THD={thd_a:.1f}%")

            if wf_b:
                wf_b["role"] = CHANNEL_B_ROLE
                wf_b["display_label"] = CHANNEL_B_LABEL
                wf_b["display_unit"] = CHANNEL_B_DISPLAY_UNIT

                freq_b, mag_b, fs_b = compute_fft(wf_b["x"], wf_b["y"])
                thd_b, f1_b, amp_b, harm_b = thd_from_fft(freq_b, mag_b)
                frame_result["thd_i"] = thd_b
                frame_result["irms_a"] = float(np.sqrt(np.mean(wf_b["y"] ** 2)))
                self.log_msg(f"Ch B: THD={thd_b:.1f}%")

                # Power analysis if both channels available
                try:
                    power = analyze_voltage_current(wf_a["y"], wf_b["y"], wf_a["delta_x"], CURRENT_SCALE_A_PER_V)
                    frame_result.update({
                        "real_power_w": power["real_power_w"],
                        "power_factor": power["power_factor"],
                        "phase_deg": power["phase_i_minus_v_deg"],
                    })
                    self.log_msg(f"Power: P={power['real_power_w']:.1f}W, PF={power['power_factor']:.3f}")
                except Exception as pe:
                    self.log_msg(f"Power analysis skipped: {pe}")

                # Save plots
                self._save_frame_plots(session_dir, frame_name, wf_a, wf_b, freq_a, mag_a, freq_b, mag_b)

            # Save screen if available
            if raw_qp:
                png_file = session_dir / f"{frame_name}_screen.png"
                self.decode_printer_stream_to_png(raw_qp, png_file)

        except Exception as e:
            self.log_msg(f"Frame analysis error: {e}")

        return frame_result

    def _generate_replay_reports(self, session_dir, frames_data, ident, total_frames):
        """Generate global summary and CSV for replay session"""
        try:
            # CSV export
            csv_file = session_dir / "replay_summary.csv"
            headers = ["frame_name", "f1_hz", "thd_v", "thd_i", "vrms_v", "irms_a", "real_power_w", "power_factor", "phase_deg"]
            with open(csv_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                for row in frames_data:
                    writer.writerow({k: row.get(k, "") for k in headers})
            self.log_msg(f"CSV saved: {csv_file.name}")

            # Trend plot if enough data
            if len(frames_data) > 1:
                self._save_trend_plots(session_dir, frames_data)

            # Global summary text
            summary_txt = session_dir / "REPLAY_SUMMARY.txt"
            with open(summary_txt, 'w') as f:
                f.write(f"Replay Session Summary\n")
                f.write(f"Instrument: {ident}\n")
                f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total frames: {total_frames}\n")
                f.write(f"Frames analyzed: {len(frames_data)}\n\n")
                for frame in frames_data:
                    f.write(f"{frame['frame_name']}: ")
                    f.write(f"f1={frame.get('f1_hz', 'N/A')} Hz, ")
                    f.write(f"THD={frame.get('thd_i', 'N/A')}\n")
            self.log_msg(f"Summary saved: {summary_txt.name}")

            self.last_pdf = session_dir / "REPLAY_SUMMARY.txt"
        except Exception as e:
            self.log_msg(f"Report generation error: {e}")

    def _save_trend_plots(self, session_dir, frames_data):
        """Save multi-frame trend plots"""
        try:
            labels = [f['frame_name'] for f in frames_data]
            x_pos = np.arange(len(labels))

            # Extract trend data
            thd_i_vals = np.array([f.get('thd_i', np.nan) for f in frames_data])
            pf_vals = np.array([f.get('power_factor', np.nan) for f in frames_data])
            power_vals = np.array([f.get('real_power_w', np.nan) for f in frames_data])

            fig, axes = plt.subplots(3, 1, figsize=(12, 9))

            axes[0].plot(x_pos, thd_i_vals, marker='o', linewidth=2)
            axes[0].set_ylabel("THD (%)")
            axes[0].grid(True, alpha=0.3)
            axes[0].set_title("Current THD Trend")

            axes[1].plot(x_pos, pf_vals, marker='o', linewidth=2, color='orange')
            axes[1].set_ylabel("Power Factor")
            axes[1].set_ylim([0, 1.05])
            axes[1].grid(True, alpha=0.3)
            axes[1].set_title("Power Factor Trend")

            axes[2].plot(x_pos, power_vals, marker='o', linewidth=2, color='green')
            axes[2].set_ylabel("Real Power (W)")
            axes[2].grid(True, alpha=0.3)
            axes[2].set_title("Real Power Trend")
            axes[2].set_xticks(x_pos)
            axes[2].set_xticklabels(labels, rotation=45, ha='right')

            fig.tight_layout()
            fig.savefig(session_dir / "trend_analysis.png", dpi=150)
            plt.close(fig)
            self.log_msg("Trend plots saved")
        except Exception as e:
            self.log_msg(f"Trend plot error: {e}")

    def _update_results_display(self):
        """Update UI results panel"""
        results_text = "Latest Analysis:\n"
        for key, val in self.analysis_results.items():
            results_text += f"• {key}: {val}\n"
        self.root.after(0, lambda: self.results_var.set(results_text))

    def show_image(self, path):
        try:
            img = Image.open(path).convert("RGB")
            max_w, max_h = 660, 430
            scale = min(max_w / img.width, max_h / img.height, 1.0)
            display_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(display_size)
            self.image_ref = ImageTk.PhotoImage(img)
            self.root.after(0, lambda: self.image_label.config(image=self.image_ref))
        except Exception as e:
            self.log_msg(f"Image display error: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = FlukeScopeSuitePro(root)
    root.mainloop()
