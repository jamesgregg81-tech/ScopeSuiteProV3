import csv
import math
import os
import time
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/fluke_mpl")

import numpy as np

from .calibration import get_timebase_correction
from .config import CURRENT_SCALE_A_PER_V
from .frequency_tools import log_frequency_debug, select_power_frequency


UNITS = [
    None, "V", "A", "Ohm", "W", "F", "K", "s", "h", "days",
    "Hz", "deg", "degC", "degF", "%", "dBm50", "dBm600",
    "dBV", "dBA", "dBW", "VAR", "VA",
]


def checksum_ok(data, check):
    total = 0
    for b in data:
        total = (total + b) % 256
    return total == check


def get_uint(data):
    return int.from_bytes(data, byteorder="big", signed=False)


def get_int(data):
    return int.from_bytes(data, byteorder="big", signed=True)


def get_float3(data):
    mantissa = get_int(data[0:2])
    exponent = get_int(data[2:3])
    return float(mantissa * (10.0 ** exponent))


def replay_status(ser, client):
    resp = client.query_ascii(ser, "RP").strip()
    parts = [p.strip() for p in resp.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"Unexpected RP response format: {resp!r}")
    return int(parts[0]), int(parts[1])


def select_replay_screen(ser, client, index):
    client.send_cmd(ser, f"RP {index}")


def get_header(ser, client, int_size):
    raw = client.read_exact(ser, 3 + int_size, "block header")
    if raw[0:2] != b"#0":
        raise RuntimeError(f"Bad block preamble: {raw!r}")
    header = raw[2]
    size = get_uint(raw[3:3 + int_size])
    return header, size, raw


def get_block_data(ser, client, size):
    raw = client.read_exact(ser, size + 1, "block data")
    payload = raw[:-1]
    check = raw[-1]
    if not checksum_ok(payload, check):
        raise RuntimeError("Checksum failed.")
    return payload, raw


def query_waveform(ser, client, trace_no):
    client.send_cmd(ser, f"QW {trace_no}")

    _admin_header, admin_size, admin_header_raw = get_header(ser, client, 2)
    admin, admin_block_raw = get_block_data(ser, client, admin_size)

    comma = client.read_exact(ser, 1, "waveform block separator")
    if comma != b",":
        raise RuntimeError(f"Expected comma between admin and samples, got {comma!r}")

    _sample_header, sample_size, sample_header_raw = get_header(ser, client, 4)
    sample_data, sample_block_raw = get_block_data(ser, client, sample_size)

    term = client.read_exact(ser, 1, "waveform terminator")
    if term != b"\r":
        raise RuntimeError(f"Expected final CR, got {term!r}")

    parsed = parse_waveform(admin, sample_data)
    parsed["trace_no"] = str(trace_no)
    parsed["trace_source"] = {
        "10": "Channel A",
        "20": "Channel B",
    }.get(str(trace_no), f"Trace {trace_no}")
    raw_response = b"".join([
        admin_header_raw,
        admin_block_raw,
        comma,
        sample_header_raw,
        sample_block_raw,
        term,
    ])
    return parsed, raw_response


def parse_waveform(admin, sample_data):
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
    delta_x = get_float3(admin[24:27]) * get_timebase_correction()
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
    adc_min = None
    adc_max = None
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
                adc_min = val if adc_min is None else min(adc_min, val)
                adc_max = val if adc_max is None else max(adc_max, val)
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
        "adc_min": adc_min,
        "adc_max": adc_max,
        "sample_width": sample_width,
        "format_byte": fmt,
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
    window = np.hanning(n)
    spec = np.fft.rfft((y - np.mean(y)) * window)
    freq = np.fft.rfftfreq(n, d=dt)
    amp = (2.0 / np.sum(window)) * np.abs(spec)
    return freq, amp, fs


def dominant_frequency(freq, amp):
    if len(freq) <= 1:
        return float("nan"), float("nan"), -1
    idx = int(np.argmax(amp[1:]) + 1)
    return float(freq[idx]), float(amp[idx]), idx


def thd_from_fft(freq, amp, max_harmonic=15):
    f1, a1, _idx = dominant_frequency(freq, amp)
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

    return math.sqrt(sum_sq) / a1 if a1 > 0 else float("nan"), f1, a1, harmonics


def wrap_phase_deg(angle_deg):
    return ((angle_deg + 180.0) % 360.0) - 180.0


def analyze_voltage_current(voltage_samples, current_samples, dt):
    v = np.asarray(voltage_samples, dtype=float)
    i_raw = np.asarray(current_samples, dtype=float)
    finite = np.isfinite(v) & np.isfinite(i_raw)
    v = v[finite]
    i_raw = i_raw[finite]

    if len(v) == 0 or len(i_raw) == 0:
        raise ValueError("Voltage or current array is empty after removing invalid samples.")

    n = min(len(v), len(i_raw))
    v = v[:n]
    i = i_raw[:n] * CURRENT_SCALE_A_PER_V

    vrms = float(np.sqrt(np.mean(v ** 2)))
    irms = float(np.sqrt(np.mean(i ** 2)))
    real_power_w = float(np.mean(v * i))
    apparent_power_va = float(vrms * irms)
    power_factor = real_power_w / apparent_power_va if apparent_power_va > 0 else float("nan")
    if np.isfinite(power_factor):
        power_factor = max(-1.0, min(1.0, power_factor))
    reactive_power_var = float(math.sqrt(max(apparent_power_va ** 2 - real_power_w ** 2, 0.0)))

    window = np.hanning(n)
    v_fft = np.fft.rfft((v - np.mean(v)) * window)
    i_fft = np.fft.rfft((i - np.mean(i)) * window)
    freqs = np.fft.rfftfreq(n, d=dt)
    k = int(np.argmax(np.abs(v_fft[1:])) + 1) if len(freqs) > 1 else 0
    fundamental_hz = float(freqs[k]) if k else float("nan")
    phase_i_minus_v_deg = float("nan")
    if k:
        phase_i_minus_v_deg = wrap_phase_deg(math.degrees(np.angle(i_fft[k])) - math.degrees(np.angle(v_fft[k])))

    return {
        "fundamental_hz": fundamental_hz,
        "vrms_v": vrms,
        "irms_a": irms,
        "real_power_w": real_power_w,
        "apparent_power_va": apparent_power_va,
        "reactive_power_var": reactive_power_var,
        "power_factor": power_factor,
        "phase_i_minus_v_deg": phase_i_minus_v_deg,
    }


def frame_name_for_index(index):
    if index < 0:
        return f"replay_m{abs(index):02d}"
    return f"replay_p{index:02d}"


def save_frame_csv(path, wf_a, wf_b):
    n = min(len(wf_a["x"]), len(wf_a["y"]), len(wf_b["y"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "channel_a_voltage", "channel_b_current"])
        for i in range(n):
            writer.writerow([float(wf_a["x"][i]), float(wf_a["y"][i]), float(wf_b["y"][i])])


def log_waveform_debug(log, frame_name, channel_name, wf):
    log(
        f"{frame_name} {channel_name}: sample count={wf['n_points']}, "
        f"ADC min/max={wf['adc_min']}/{wf['adc_max']}, "
        f"volts/div={wf['y_scale']} {wf['y_unit']}, time/div={wf['x_scale']} {wf['x_unit']}, "
        f"scaling y_resolution={wf['y_resolution']}, delta_x={wf['delta_x']} {wf['x_unit']}, "
        f"sample_width={wf['sample_width']}, samples_per_point={wf['samples_per_point']}"
    )


def log_waveform_acceptance(log, frame_name, wf_a, wf_b, csv_name):
    log(
        f"{frame_name}: Waveform Capture Test PASS - QW returned numeric samples, "
        "not screen pixels."
    )
    log(
        f"{frame_name}: Channel A/B arrays independent of screen bitmap boundaries; "
        f"A samples={wf_a['n_points']}, B samples={wf_b['n_points']}, "
        "screen bitmap is not reused as waveform data."
    )
    log(f"{frame_name}: Export CSV includes columns: time, channel_a_voltage, channel_b_current ({csv_name})")


def write_summary_csv(path, rows):
    headers = [
        "frame_name", "replay_index", "points_a", "points_b",
        "vrms_v", "irms_a", "real_power_w", "apparent_power_va",
        "reactive_power_var", "power_factor", "phase_i_minus_v_deg",
        "dominant_freq_v_hz", "dominant_freq_i_hz", "thd_v", "thd_i",
        "harm3_i", "harm5_i", "harm7_i", "waveform_csv",
        "waveform_png", "fft_png", "raw_a_bin", "raw_b_bin",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def save_global_report(path, ident, rp_count, rows):
    lines = [
        "FLUKE REPLAY WAVEFORM EXPORT",
        "============================",
        "",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scope ID: {ident}",
        f"Replay frames exported: {len(rows)}",
        f"Scope replay count reported: {rp_count}",
        "",
    ]
    if rows:
        vrms = np.array([r["vrms_v"] for r in rows], dtype=float)
        irms = np.array([r["irms_a"] for r in rows], dtype=float)
        pf = np.array([r["power_factor"] for r in rows], dtype=float)
        lines.extend([
            f"Vrms mean/min/max: {np.nanmean(vrms):.3f} / {np.nanmin(vrms):.3f} / {np.nanmax(vrms):.3f} V",
            f"Irms mean/min/max: {np.nanmean(irms):.3f} / {np.nanmin(irms):.3f} / {np.nanmax(irms):.3f} A",
            f"PF mean/min/max: {np.nanmean(pf):.4f} / {np.nanmin(pf):.4f} / {np.nanmax(pf):.4f}",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def try_import_plot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception:
        return None


def save_waveform_plot(path, frame_name, wf_a, wf_b):
    plt = try_import_plot()
    if plt is None:
        return False
    plt.figure(figsize=(10, 6))
    plt.plot(wf_a["x"], wf_a["y"], label="Channel A Voltage (V)")
    plt.plot(wf_b["x"], wf_b["y"], label="Channel B Current (A)")
    plt.xlabel(f"Time ({wf_a['x_unit']})")
    plt.ylabel("Amplitude")
    plt.title(frame_name)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return True


def save_fft_plot(path, frame_name, freq_a, amp_a, freq_b, amp_b):
    plt = try_import_plot()
    if plt is None:
        return False
    plt.figure(figsize=(10, 6))
    plt.plot(freq_a, amp_a, label="Channel A Voltage FFT")
    plt.plot(freq_b, amp_b, label="Channel B Current FFT")
    plt.xlim(left=0)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Amplitude")
    plt.title(f"{frame_name} FFT")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return True


def waveform_stats(wf, scale=1.0, log=None, label="waveform", fallback_wf=None):
    log = log or (lambda _msg: None)
    y = np.asarray(wf["y"], dtype=float) * scale
    finite = y[np.isfinite(y)]
    if len(finite) == 0:
        raise RuntimeError("No finite waveform samples.")

    stats = {
        "sample_count": int(len(finite)),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "peak_to_peak": float(np.max(finite) - np.min(finite)),
        "rms": float(np.sqrt(np.mean(finite ** 2))),
        "frequency_hz": float("nan"),
        "frequency_method": "unavailable",
        "zero_crossing_frequency": float("nan"),
        "dominant_frequency": float("nan"),
        "dominant_amplitude": float("nan"),
        "thd": float("nan"),
    }

    try:
        freq, amp, _fs = compute_fft(wf["x"], y)
        dom_freq, dom_amp, _idx = dominant_frequency(freq, amp)
        thd, _f1, _a1, _harm = thd_from_fft(freq, amp)
        stats["dominant_frequency"] = dom_freq
        stats["dominant_amplitude"] = dom_amp
        stats["thd"] = thd
    except Exception:
        pass

    fallback_values = None
    if fallback_wf is not None:
        fallback_values = np.asarray(fallback_wf["y"], dtype=float)

    freq_info = select_power_frequency(wf["x"], y, fallback_values=fallback_values)
    log_frequency_debug(log, label, freq_info, delta_x=wf.get("delta_x"))
    stats["frequency_hz"] = freq_info["final_hz"]
    stats["frequency_method"] = freq_info["method"]
    stats["zero_crossing_frequency"] = freq_info["zero_crossing_hz"]

    return stats


def save_single_waveform_csv(path, wf, value_label):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time", value_label])
        for x, y in zip(wf["x"], wf["y"]):
            writer.writerow([float(x), float(y)])


def save_single_waveform_plot(path, wf_a=None, wf_b=None):
    plt = try_import_plot()
    if plt is None:
        return False
    if wf_a is None and wf_b is None:
        return False

    plt.figure(figsize=(10, 6))
    if wf_a is not None:
        plt.plot(wf_a["x"], wf_a["y"], label=f"Channel A ({wf_a['y_unit']})")
    if wf_b is not None:
        plt.plot(wf_b["x"], wf_b["y"], label=f"Channel B ({wf_b['y_unit']})")
    x_unit = wf_a["x_unit"] if wf_a is not None else wf_b["x_unit"]
    plt.xlabel(f"Time ({x_unit})")
    plt.ylabel("Amplitude")
    plt.title("Single Screen Waveform")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return True


def save_single_fft_plot(path, wf_a=None, wf_b=None):
    plt = try_import_plot()
    if plt is None:
        return False
    if wf_a is None and wf_b is None:
        return False

    plotted = False
    plt.figure(figsize=(10, 6))
    if wf_a is not None:
        try:
            freq_a, amp_a, _fs_a = compute_fft(wf_a["x"], wf_a["y"])
            plt.plot(freq_a, amp_a, label="Channel A FFT")
            plotted = True
        except Exception:
            pass
    if wf_b is not None:
        try:
            freq_b, amp_b, _fs_b = compute_fft(wf_b["x"], wf_b["y"])
            plt.plot(freq_b, amp_b, label="Channel B FFT")
            plotted = True
        except Exception:
            pass

    if not plotted:
        plt.close()
        return False

    plt.xlim(left=0)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Amplitude")
    plt.title("Single Screen FFT")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return True


def single_channel_report_lines(label, wf, stats, rms_label):
    return [
        label,
        "-" * len(label),
        f"Volts/div or unit/div: {wf['y_scale']} {wf['y_unit']}",
        f"Time/div: {wf['x_scale']} {wf['x_unit']}",
        f"Sample count: {wf['n_points']}",
        f"ADC min/max: {wf['adc_min']} / {wf['adc_max']}",
        f"{rms_label}: {stats['rms']:.6g}",
        f"Frequency estimate: {stats['frequency_hz']:.6g} Hz ({stats['frequency_method']})",
        f"Zero-crossing frequency: {stats['zero_crossing_frequency']:.6g} Hz",
        f"Min / Max / Peak-to-peak: {stats['min']:.6g} / {stats['max']:.6g} / {stats['peak_to_peak']:.6g}",
        f"FFT dominant frequency: {stats['dominant_frequency']:.6g} Hz",
        f"FFT dominant amplitude: {stats['dominant_amplitude']:.6g}",
        f"THD: {stats['thd']:.6g}",
        "",
    ]


def save_single_waveform_report(export_dir, ident, screen_capture_path, wf_a=None, wf_b=None, visual_only=False, q_w_error=None, log=None):
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    log = log or (lambda _msg: None)

    if wf_a is not None:
        save_single_waveform_csv(export_dir / "single_waveform_A.csv", wf_a, "channel_a_voltage")
        log_waveform_debug(log, "single_screen", "Channel A", wf_a)
    if wf_b is not None:
        save_single_waveform_csv(export_dir / "single_waveform_B.csv", wf_b, "channel_b_current")
        log_waveform_debug(log, "single_screen", "Channel B", wf_b)

    plotted_waveform = save_single_waveform_plot(export_dir / "single_waveform_plot.png", wf_a, wf_b)
    plotted_fft = save_single_fft_plot(export_dir / "single_fft_plot.png", wf_a, wf_b)

    lines = [
        "SINGLE SCREEN WAVEFORM REPORT",
        "=============================",
        "",
        f"Scope ID: {ident}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Screenshot reference image: {Path(screen_capture_path).name if screen_capture_path else 'Unavailable'}",
        "",
    ]

    if visual_only:
        lines.extend([
            "Visual approximation only -- numeric waveform data unavailable.",
            f"QW failure: {q_w_error}",
            "",
        ])
    else:
        lines.extend([
            "Primary source: QW numeric waveform data from active displayed traces.",
            "",
        ])
        if wf_a is not None:
            stats_a = waveform_stats(wf_a, log=log, label="single_screen Channel A frequency", fallback_wf=wf_b)
            lines.extend(single_channel_report_lines("Channel A", wf_a, stats_a, "Vrms"))
        if wf_b is not None:
            stats_b = waveform_stats(wf_b, CURRENT_SCALE_A_PER_V, log=log, label="single_screen Channel B frequency")
            lines.extend(single_channel_report_lines("Channel B", wf_b, stats_b, "Irms"))

    if plotted_waveform:
        lines.append("Waveform plot: single_waveform_plot.png")
    if plotted_fft:
        lines.append("FFT plot: single_fft_plot.png")

    report_path = export_dir / "SINGLE_WAVEFORM_REPORT.txt"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def save_trend_plots(export_dir, rows):
    plt = try_import_plot()
    if plt is None or not rows:
        return False

    x = np.arange(len(rows))
    labels = [r["frame_name"] for r in rows]
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
    fig.savefig(export_dir / "global_trend_summary.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

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
    fig.savefig(export_dir / "global_harmonic_summary.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def replay_csv_sort_key(path):
    name = path.name
    prefix = name.removeprefix("replay_").removesuffix("_waveforms.csv")
    if prefix.startswith("m"):
        return -int(prefix[1:])
    if prefix.startswith("p"):
        return int(prefix[1:])
    return 0


def replay_index_from_frame_name(frame_name):
    suffix = frame_name.removeprefix("replay_")
    if suffix.startswith("m"):
        return -int(suffix[1:])
    if suffix.startswith("p"):
        return int(suffix[1:])
    return 0


def estimate_frame_duration(times):
    if len(times) <= 1:
        return 0.0
    diffs = np.diff(times)
    finite_diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if len(finite_diffs):
        delta_t = float(np.median(finite_diffs))
        return float((times[-1] - times[0]) + delta_t)
    return float(times[-1] - times[0])


def stitch_replay_waveform_csvs(export_dir, rows, log=None):
    export_dir = Path(export_dir)
    log = log or (lambda _msg: None)
    csv_paths = sorted(export_dir.glob("replay_*_waveforms.csv"), key=replay_csv_sort_key)
    csv_paths = [p for p in csv_paths if p.name != "stitched_replay_waveforms.csv"]

    stitched_csv = export_dir / "stitched_replay_waveforms.csv"
    stitched_png = export_dir / "stitched_replay_overview.png"
    frame_ranges = {}
    samples_per_frame = {}
    total_samples = 0
    offset = 0.0

    with stitched_csv.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow([
            "stitched_time",
            "channel_a_voltage",
            "channel_b_current",
            "frame_name",
            "replay_index",
        ])

        for path in csv_paths:
            frame_name = path.name.removesuffix("_waveforms.csv")
            replay_index = replay_index_from_frame_name(frame_name)
            times = []
            channel_a = []
            channel_b = []

            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        t = float(row["time"])
                        a = float(row["channel_a_voltage"])
                        b = float(row["channel_b_current"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    times.append(t)
                    channel_a.append(a)
                    channel_b.append(b)

            if not times:
                continue

            times_arr = np.asarray(times, dtype=float)
            rel_times = times_arr - times_arr[0]
            stitched_times = rel_times + offset
            for t, a, b in zip(stitched_times, channel_a, channel_b):
                writer.writerow([float(t), float(a), float(b), frame_name, replay_index])

            duration = estimate_frame_duration(times_arr)
            frame_ranges[frame_name] = {
                "start": float(offset),
                "end": float(offset + duration),
                "mid": float(offset + duration / 2.0),
                "samples": len(times),
            }
            samples_per_frame[frame_name] = len(times)
            total_samples += len(times)
            offset += max(duration, 0.0)

    if not csv_paths:
        log("Stitched Replay View skipped: no replay waveform CSV files found.")
        return None

    def safe_metric(row, key, default=float("nan")):
        try:
            value = float(row.get(key, default))
        except (TypeError, ValueError, AttributeError):
            return default
        return value if np.isfinite(value) else default

    row_by_frame = {r["frame_name"]: r for r in rows if "frame_name" in r}
    current_rows = [r for r in rows if np.isfinite(safe_metric(r, "irms_a"))]
    pf_rows = [r for r in rows if np.isfinite(safe_metric(r, "power_factor"))]
    max_current = max(current_rows, key=lambda r: safe_metric(r, "irms_a")) if current_rows else None
    lowest_pf = min(pf_rows, key=lambda r: safe_metric(r, "power_factor")) if pf_rows else None

    log(f"Stitched Replay View: frames stitched={len(frame_ranges)}")
    log(
        "Stitched Replay View: samples per frame="
        + ", ".join(f"{name}:{count}" for name, count in samples_per_frame.items())
    )
    log(f"Stitched Replay View: total stitched samples={total_samples}")
    log(f"Stitched Replay View: estimated total replay duration={offset:.6g} stitched-time units")
    if max_current:
        log(f"Stitched Replay View: detected max current frame={max_current['frame_name']} Irms={safe_metric(max_current, 'irms_a'):.6g}")
    if lowest_pf:
        log(f"Stitched Replay View: detected lowest PF frame={lowest_pf['frame_name']} PF={safe_metric(lowest_pf, 'power_factor'):.6g}")

    save_stitched_replay_plot(stitched_csv, stitched_png, frame_ranges, row_by_frame, log)
    return {
        "csv": stitched_csv,
        "png": stitched_png if stitched_png.exists() else None,
        "frames": len(frame_ranges),
        "samples": total_samples,
        "duration": offset,
        "max_current_frame": max_current["frame_name"] if max_current else None,
        "lowest_pf_frame": lowest_pf["frame_name"] if lowest_pf else None,
    }


def _finite_float(value, default=float("nan")):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _row_value(row, key):
    return _finite_float(row.get(key))


def _frame_midpoints_from_stitched(stitched_csv):
    frame_points = {}
    with Path(stitched_csv).open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            frame = row.get("frame_name", "")
            t = _finite_float(row.get("stitched_time"))
            if frame and np.isfinite(t):
                frame_points.setdefault(frame, []).append(t)
    return {
        frame: float((min(times) + max(times)) / 2.0)
        for frame, times in frame_points.items()
        if times
    }


def write_deep_memory_full_dataset(export_dir, stitched_csv):
    export_dir = Path(export_dir)
    out_path = export_dir / "deep_memory_full_capture.csv"
    with Path(stitched_csv).open("r", newline="", encoding="utf-8") as src, out_path.open("w", newline="", encoding="utf-8") as dst:
        reader = csv.DictReader(src)
        fieldnames = [
            "time_s",
            "channel_a_voltage_v",
            "channel_b_current_a",
            "instantaneous_power_w",
            "frame_name",
            "replay_index",
        ]
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            t = _finite_float(row.get("stitched_time"))
            v = _finite_float(row.get("channel_a_voltage"))
            i = _finite_float(row.get("channel_b_current"))
            p = v * i if np.isfinite(v) and np.isfinite(i) else float("nan")
            writer.writerow({
                "time_s": t,
                "channel_a_voltage_v": v,
                "channel_b_current_a": i,
                "instantaneous_power_w": p,
                "frame_name": row.get("frame_name", ""),
                "replay_index": row.get("replay_index", ""),
            })
    return out_path


def write_deep_memory_trends(export_dir, rows, stitched_csv):
    export_dir = Path(export_dir)
    out_path = export_dir / "deep_memory_trends.csv"
    frame_midpoints = _frame_midpoints_from_stitched(stitched_csv)
    fieldnames = [
        "time_s",
        "frame_name",
        "replay_index",
        "vrms_v",
        "irms_a",
        "frequency_hz",
        "real_power_kw",
        "apparent_power_kva",
        "reactive_power_kvar",
        "power_factor",
        "phase_i_minus_v_deg",
        "thd_v_percent",
        "thd_i_percent",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for idx, row in enumerate(rows):
            frame_name = row.get("frame_name", "")
            real_w = _row_value(row, "real_power_w")
            apparent_va = _row_value(row, "apparent_power_va")
            reactive_var = _row_value(row, "reactive_power_var")
            writer.writerow({
                "time_s": frame_midpoints.get(frame_name, float(idx)),
                "frame_name": frame_name,
                "replay_index": row.get("replay_index", ""),
                "vrms_v": _row_value(row, "vrms_v"),
                "irms_a": _row_value(row, "irms_a"),
                "frequency_hz": _row_value(row, "dominant_freq_v_hz"),
                "real_power_kw": real_w / 1000.0 if np.isfinite(real_w) else float("nan"),
                "apparent_power_kva": apparent_va / 1000.0 if np.isfinite(apparent_va) else float("nan"),
                "reactive_power_kvar": reactive_var / 1000.0 if np.isfinite(reactive_var) else float("nan"),
                "power_factor": _row_value(row, "power_factor"),
                "phase_i_minus_v_deg": _row_value(row, "phase_i_minus_v_deg"),
                "thd_v_percent": _row_value(row, "thd_v") * 100.0,
                "thd_i_percent": _row_value(row, "thd_i") * 100.0,
            })
    return out_path


def detect_worst_cases(rows):
    finite_rows = list(rows or [])

    def pick(label, key, selector, unit="", transform=lambda v: v):
        candidates = [(row, _row_value(row, key)) for row in finite_rows]
        candidates = [(row, value) for row, value in candidates if np.isfinite(value)]
        if not candidates:
            return {"label": label, "frame_name": "N/A", "value": float("nan"), "unit": unit}
        row, value = selector(candidates, key=lambda item: item[1])
        return {
            "label": label,
            "frame_name": row.get("frame_name", "N/A"),
            "value": transform(value),
            "unit": unit,
        }

    frequency_values = [_row_value(row, "dominant_freq_v_hz") for row in finite_rows]
    frequency_values = [v for v in frequency_values if np.isfinite(v)]
    nominal_frequency = float(np.nanmedian(frequency_values)) if frequency_values else float("nan")
    frequency_candidates = []
    for row in finite_rows:
        freq = _row_value(row, "dominant_freq_v_hz")
        if np.isfinite(freq) and np.isfinite(nominal_frequency):
            frequency_candidates.append((row, abs(freq - nominal_frequency)))
    if frequency_candidates:
        freq_row, freq_dev = max(frequency_candidates, key=lambda item: item[1])
        worst_frequency = {
            "label": "Worst frequency deviation",
            "frame_name": freq_row.get("frame_name", "N/A"),
            "value": freq_dev,
            "unit": "Hz from median",
        }
    else:
        worst_frequency = {"label": "Worst frequency deviation", "frame_name": "N/A", "value": float("nan"), "unit": "Hz"}

    transient_candidates = []
    for prev, cur in zip(finite_rows, finite_rows[1:]):
        prev_kw = _row_value(prev, "real_power_w")
        cur_kw = _row_value(cur, "real_power_w")
        if np.isfinite(prev_kw) and np.isfinite(cur_kw):
            transient_candidates.append((cur, abs(cur_kw - prev_kw) / 1000.0))
    if transient_candidates:
        transient_row, transient = max(transient_candidates, key=lambda item: item[1])
        worst_transient = {
            "label": "Largest load-change transient",
            "frame_name": transient_row.get("frame_name", "N/A"),
            "value": transient,
            "unit": "kW step",
        }
    else:
        worst_transient = {"label": "Largest load-change transient", "frame_name": "N/A", "value": float("nan"), "unit": "kW"}

    return [
        pick("Lowest voltage", "vrms_v", min, "V"),
        pick("Highest current", "irms_a", max, "A"),
        pick("Highest THD", "thd_i", max, "% THD-I", lambda v: v * 100.0),
        pick("Lowest power factor", "power_factor", min, ""),
        worst_frequency,
        worst_transient,
    ]


def write_worst_case_report(export_dir, rows, stitched_info=None):
    export_dir = Path(export_dir)
    worst_cases = detect_worst_cases(rows)
    csv_path = export_dir / "deep_memory_worst_cases.csv"
    txt_path = export_dir / "DEEP_MEMORY_SUMMARY.txt"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "frame_name", "value", "unit"])
        writer.writeheader()
        writer.writerows(worst_cases)

    lines = [
        "DEEP MEMORY RECONSTRUCTION SUMMARY",
        "==================================",
        "",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Frames stitched: {stitched_info.get('frames', 'N/A') if stitched_info else 'N/A'}",
        f"Total samples: {stitched_info.get('samples', 'N/A') if stitched_info else 'N/A'}",
        f"Estimated duration: {stitched_info.get('duration', float('nan')) if stitched_info else float('nan'):.6g}",
        "",
        "Worst-case detector:",
    ]
    for item in worst_cases:
        value = item["value"]
        value_text = f"{value:.6g}" if np.isfinite(_finite_float(value)) else "N/A"
        suffix = f" {item['unit']}" if item.get("unit") else ""
        lines.append(f"- {item['label']}: {value_text}{suffix} at {item['frame_name']}")
    lines.extend([
        "",
        "Outputs:",
        "- deep_memory_full_capture.csv",
        "- deep_memory_trends.csv",
        "- deep_memory_full_capture.png",
        "- deep_memory_trends.png",
        "- deep_memory_worst_cases.csv",
    ])
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return {"csv": csv_path, "txt": txt_path, "items": worst_cases}


def save_deep_memory_plots(export_dir, full_csv, trend_csv, log=None):
    log = log or (lambda _msg: None)
    plt = try_import_plot()
    if plt is None:
        log("Deep Memory plot skipped: Matplotlib is not available.")
        return {"full_plot": None, "trend_plot": None}

    full_time, voltage, current, power_kw = [], [], [], []
    with Path(full_csv).open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = _finite_float(row.get("time_s"))
            v = _finite_float(row.get("channel_a_voltage_v"))
            i = _finite_float(row.get("channel_b_current_a"))
            p = _finite_float(row.get("instantaneous_power_w"))
            if np.isfinite(t):
                full_time.append(t)
                voltage.append(v)
                current.append(i)
                power_kw.append(p / 1000.0 if np.isfinite(p) else float("nan"))

    full_plot = Path(export_dir) / "deep_memory_full_capture.png"
    if full_time:
        fig, axes = plt.subplots(3, 1, figsize=(15, 10), sharex=True)
        axes[0].plot(full_time, voltage, linewidth=0.7, color="#0b5cab")
        axes[0].set_ylabel("Voltage (V)")
        axes[1].plot(full_time, current, linewidth=0.7, color="#b04700")
        axes[1].set_ylabel("Current (A)")
        axes[2].plot(full_time, power_kw, linewidth=0.7, color="#087f5b")
        axes[2].set_ylabel("Power (kW)")
        axes[2].set_xlabel("Continuous stitched time")
        for ax in axes:
            ax.grid(True, alpha=0.3)
        fig.suptitle("Deep Memory Reconstructed Full Capture")
        fig.tight_layout()
        fig.savefig(full_plot, dpi=150, bbox_inches="tight")
        plt.close(fig)

    trend_time = []
    trend_values = {key: [] for key in ("vrms_v", "irms_a", "frequency_hz", "real_power_kw", "power_factor", "thd_i_percent")}
    with Path(trend_csv).open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = _finite_float(row.get("time_s"))
            if not np.isfinite(t):
                continue
            trend_time.append(t)
            for key in trend_values:
                trend_values[key].append(_finite_float(row.get(key)))

    trend_plot = Path(export_dir) / "deep_memory_trends.png"
    if trend_time:
        fig, axes = plt.subplots(5, 1, figsize=(15, 12), sharex=True)
        axes[0].plot(trend_time, trend_values["vrms_v"], marker="o"); axes[0].set_ylabel("Vrms (V)")
        axes[1].plot(trend_time, trend_values["irms_a"], marker="o"); axes[1].set_ylabel("Irms (A)")
        axes[2].plot(trend_time, trend_values["frequency_hz"], marker="o"); axes[2].set_ylabel("Hz")
        axes[3].plot(trend_time, trend_values["real_power_kw"], marker="o"); axes[3].set_ylabel("kW")
        axes[3].plot(trend_time, trend_values["power_factor"], marker="s", label="PF")
        axes[3].legend(loc="best")
        axes[4].plot(trend_time, trend_values["thd_i_percent"], marker="o"); axes[4].set_ylabel("THD-I %")
        axes[4].set_xlabel("Continuous stitched time")
        for ax in axes:
            ax.grid(True, alpha=0.3)
        fig.suptitle("Deep Memory Trend Analysis")
        fig.tight_layout()
        fig.savefig(trend_plot, dpi=150, bbox_inches="tight")
        plt.close(fig)

    return {
        "full_plot": full_plot if full_plot.exists() else None,
        "trend_plot": trend_plot if trend_plot.exists() else None,
    }


def analyze_deep_memory_capture(export_dir, rows=None, log=None):
    export_dir = Path(export_dir)
    log = log or (lambda _msg: None)
    if rows is None:
        summary_path = export_dir / "replay_summary.csv"
        rows = []
        if summary_path.exists():
            with summary_path.open("r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

    stitched_csv = export_dir / "stitched_replay_waveforms.csv"
    stitched_info = None
    if not stitched_csv.exists():
        stitched_info = stitch_replay_waveform_csvs(export_dir, rows or [], log=log)
        if not stitched_info:
            raise RuntimeError("No replay frame CSV files available for deep-memory reconstruction.")
        stitched_csv = Path(stitched_info["csv"])
    else:
        stitched_info = stitch_replay_waveform_csvs(export_dir, rows or [], log=log)

    full_csv = write_deep_memory_full_dataset(export_dir, stitched_csv)
    trends_csv = write_deep_memory_trends(export_dir, rows or [], stitched_csv)
    worst = write_worst_case_report(export_dir, rows or [], stitched_info or {})
    plots = save_deep_memory_plots(export_dir, full_csv, trends_csv, log=log)

    log(f"Deep Memory Reconstruction: combined dataset={full_csv}")
    log(f"Deep Memory Reconstruction: trend dataset={trends_csv}")
    if plots.get("full_plot"):
        log(f"Deep Memory Reconstruction: full plot={plots['full_plot']}")
    if plots.get("trend_plot"):
        log(f"Deep Memory Reconstruction: trend plot={plots['trend_plot']}")
    log(f"Deep Memory Reconstruction: summary={worst['txt']}")
    return {
        "full_csv": full_csv,
        "trends_csv": trends_csv,
        "worst_csv": worst["csv"],
        "summary_txt": worst["txt"],
        "full_plot": plots.get("full_plot"),
        "trend_plot": plots.get("trend_plot"),
        "stitched_csv": stitched_csv,
    }


def save_stitched_replay_plot(stitched_csv, stitched_png, frame_ranges, row_by_frame, log=None):
    log = log or (lambda _msg: None)
    plt = try_import_plot()
    if plt is None:
        log("Stitched Replay View plot skipped: Matplotlib is not available.")
        return False

    stitched_time = []
    channel_a = []
    channel_b = []
    with stitched_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                stitched_time.append(float(row["stitched_time"]))
                channel_a.append(float(row["channel_a_voltage"]))
                channel_b.append(float(row["channel_b_current"]))
            except (KeyError, TypeError, ValueError):
                continue

    if not stitched_time:
        log("Stitched Replay View plot skipped: stitched CSV has no numeric samples.")
        return False

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    axes[0].plot(stitched_time, channel_a, linewidth=0.8, color="#0b6f85", label="Channel A Voltage")
    axes[1].plot(stitched_time, channel_b, linewidth=0.8, color="#8a4b08", label="Channel B Current")

    for ax in axes:
        for frame_name, info in frame_ranges.items():
            ax.axvline(info["start"], color="#999999", alpha=0.25, linewidth=0.7)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right")

    anomalies = []
    if row_by_frame:
        def plot_metric(row, key, default=float("nan")):
            try:
                value = float(row.get(key, default))
            except (TypeError, ValueError, AttributeError):
                return default
            return value if np.isfinite(value) else default

        vrms_rows = [r for r in row_by_frame.values() if np.isfinite(plot_metric(r, "vrms_v"))]
        irms_rows = [r for r in row_by_frame.values() if np.isfinite(plot_metric(r, "irms_a"))]
        kw_rows = [r for r in row_by_frame.values() if np.isfinite(plot_metric(r, "real_power_w"))]
        pf_rows = [r for r in row_by_frame.values() if np.isfinite(plot_metric(r, "power_factor"))]
        anomalies = [
            *([("min Vrms", min(vrms_rows, key=lambda r: plot_metric(r, "vrms_v")), "#2f6fed")] if vrms_rows else []),
            *([("max Vrms", max(vrms_rows, key=lambda r: plot_metric(r, "vrms_v")), "#2f6fed")] if vrms_rows else []),
            *([("max Irms", max(irms_rows, key=lambda r: plot_metric(r, "irms_a")), "#d04a02")] if irms_rows else []),
            *([("max kW", max(kw_rows, key=lambda r: plot_metric(r, "real_power_w")), "#6a3fb5")] if kw_rows else []),
            *([("lowest PF", min(pf_rows, key=lambda r: plot_metric(r, "power_factor")), "#b00020")] if pf_rows else []),
        ]

    for label, row, color in anomalies:
        frame = row["frame_name"]
        info = frame_ranges.get(frame)
        if not info:
            continue
        target_ax = axes[1] if label in ("max Irms", "lowest PF") else axes[0]
        target_ax.axvline(info["mid"], color=color, linestyle="--", linewidth=1.1, alpha=0.8)
        target_ax.text(
            info["mid"],
            0.95,
            f"{label}\n{frame}",
            color=color,
            rotation=90,
            transform=target_ax.get_xaxis_transform(),
            va="top",
            ha="right",
            fontsize=8,
        )

    axes[0].set_ylabel("Channel A Voltage")
    axes[1].set_ylabel("Channel B Current")
    axes[1].set_xlabel("Frame-relative stitched replay time")
    fig.suptitle("Stitched Replay View")
    fig.tight_layout()
    fig.savefig(stitched_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def save_waterfall_replay_heatmap(export_dir, log=None):
    export_dir = Path(export_dir)
    log = log or (lambda _msg: None)
    plt = try_import_plot()
    if plt is None:
        log("Waterfall Replay View skipped: Matplotlib is not available.")
        return None

    csv_paths = sorted(export_dir.glob("replay_*_waveforms.csv"), key=replay_csv_sort_key)
    csv_paths = [p for p in csv_paths if p.name != "stitched_replay_waveforms.csv"]
    if not csv_paths:
        log("Waterfall Replay View skipped: no replay waveform CSV files found.")
        return None

    frame_names = []
    frame_times = []
    current_magnitudes = []
    max_duration = 0.0
    max_samples = 0

    for path in csv_paths:
        times = []
        currents = []
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    times.append(float(row["time"]))
                    currents.append(abs(float(row["channel_b_current"])))
                except (KeyError, TypeError, ValueError):
                    continue

        if not times:
            continue

        times_arr = np.asarray(times, dtype=float)
        rel_times = times_arr - times_arr[0]
        frame_names.append(path.name.removesuffix("_waveforms.csv"))
        frame_times.append(rel_times)
        current_magnitudes.append(np.asarray(currents, dtype=float))
        max_duration = max(max_duration, estimate_frame_duration(times_arr))
        max_samples = max(max_samples, len(times))

    if not current_magnitudes or max_samples == 0:
        log("Waterfall Replay View skipped: waveform CSVs had no numeric current samples.")
        return None

    grid = np.full((len(current_magnitudes), max_samples), np.nan, dtype=float)
    if max_duration > 0 and max_samples > 1:
        common_time = np.linspace(0.0, max_duration, max_samples)
        for row_idx, (times, currents) in enumerate(zip(frame_times, current_magnitudes)):
            finite = np.isfinite(times) & np.isfinite(currents)
            if np.count_nonzero(finite) >= 2:
                grid[row_idx, :] = np.interp(common_time, times[finite], currents[finite])
            elif np.count_nonzero(finite) == 1:
                grid[row_idx, :] = currents[finite][0]
    else:
        common_time = np.arange(max_samples, dtype=float)
        for row_idx, currents in enumerate(current_magnitudes):
            grid[row_idx, :len(currents)] = currents

    heatmap_path = export_dir / "waterfall_replay_heatmap.png"
    fig, ax = plt.subplots(figsize=(14, 8))
    masked = np.ma.masked_invalid(grid)
    extent = [float(common_time[0]), float(common_time[-1]) if len(common_time) else 0.0, len(frame_names) - 0.5, -0.5]
    image = ax.imshow(masked, aspect="auto", interpolation="nearest", extent=extent, cmap="inferno")
    fig.colorbar(image, ax=ax, label="Channel B current magnitude")
    ax.set_xlabel("Time within replay frame")
    ax.set_ylabel("Replay frame number")
    ax.set_title("Waterfall / Heatmap Replay View")

    if len(frame_names) <= 30:
        ax.set_yticks(np.arange(len(frame_names)))
        ax.set_yticklabels(frame_names)
    else:
        tick_positions = np.linspace(0, len(frame_names) - 1, min(12, len(frame_names))).astype(int)
        ax.set_yticks(tick_positions)
        ax.set_yticklabels([frame_names[i] for i in tick_positions])

    fig.tight_layout()
    fig.savefig(heatmap_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    log(
        f"Waterfall Replay View: frames={len(frame_names)}, columns={max_samples}, "
        f"time window={float(common_time[-1]) if len(common_time) else 0.0:.6g}, output={heatmap_path.name}"
    )
    return heatmap_path


def export_replay_waveforms(ser, client, export_dir, ident, progress=None, log=None):
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    log = log or (lambda _msg: None)

    nr_screens, screen_index = replay_status(ser, client)
    if nr_screens <= 0:
        select_replay_screen(ser, client, 0)
        nr_screens, screen_index = replay_status(ser, client)
    if nr_screens <= 0:
        raise RuntimeError("No replay screens found. Put the ScopeMeter in Replay first.")

    indices = list(range(-(nr_screens - 1), 1))
    rows = []
    log(f"Replay status: screens={nr_screens}, current index={screen_index}")

    for pos, idx in enumerate(indices, start=1):
        frame_name = frame_name_for_index(idx)
        log(f"Exporting {frame_name} ({pos}/{len(indices)})")
        if progress:
            progress(pos - 1, len(indices), f"Replay {pos}/{len(indices)}")

        select_replay_screen(ser, client, idx)
        wf_a, raw_a = query_waveform(ser, client, "10")
        wf_b, raw_b = query_waveform(ser, client, "20")

        raw_a_name = f"{frame_name}_A_raw.bin"
        raw_b_name = f"{frame_name}_B_raw.bin"
        csv_name = f"{frame_name}_waveforms.csv"
        waveform_png = f"{frame_name}_waveforms.png"
        fft_png = f"{frame_name}_fft.png"
        (export_dir / raw_a_name).write_bytes(raw_a)
        (export_dir / raw_b_name).write_bytes(raw_b)
        save_frame_csv(export_dir / csv_name, wf_a, wf_b)
        log_waveform_debug(log, frame_name, "Channel A", wf_a)
        log_waveform_debug(log, frame_name, "Channel B", wf_b)
        log_waveform_acceptance(log, frame_name, wf_a, wf_b, csv_name)

        freq_a, amp_a, _fs_a = compute_fft(wf_a["x"], wf_a["y"])
        freq_b, amp_b, _fs_b = compute_fft(wf_b["x"], wf_b["y"])
        freq_info_a = select_power_frequency(wf_a["x"], wf_a["y"], fallback_values=wf_b["y"])
        freq_info_b = select_power_frequency(wf_b["x"], wf_b["y"], fallback_values=wf_a["y"])
        log_frequency_debug(log, f"{frame_name} Channel A frequency", freq_info_a, delta_x=wf_a.get("delta_x"))
        log_frequency_debug(log, f"{frame_name} Channel B frequency", freq_info_b, delta_x=wf_b.get("delta_x"))
        dom_a = freq_info_a["final_hz"]
        dom_b = freq_info_b["final_hz"]
        thd_a, _f1a, _a1a, _harm_a = thd_from_fft(freq_a, amp_a)
        thd_b, _f1b, _a1b, harm_b = thd_from_fft(freq_b, amp_b)
        power = analyze_voltage_current(wf_a["y"], wf_b["y"], wf_a["delta_x"])
        plotted_waveform = save_waveform_plot(export_dir / waveform_png, frame_name, wf_a, wf_b)
        plotted_fft = save_fft_plot(export_dir / fft_png, frame_name, freq_a, amp_a, freq_b, amp_b)

        rows.append({
            "frame_name": frame_name,
            "replay_index": idx,
            "points_a": wf_a["n_points"],
            "points_b": wf_b["n_points"],
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
            "waveform_csv": csv_name,
            "waveform_png": waveform_png if plotted_waveform else "",
            "fft_png": fft_png if plotted_fft else "",
            "raw_a_bin": raw_a_name,
            "raw_b_bin": raw_b_name,
        })

    write_summary_csv(export_dir / "replay_summary.csv", rows)
    save_global_report(export_dir / "FINAL_GLOBAL_REPORT.txt", ident, nr_screens, rows)
    save_trend_plots(export_dir, rows)
    stitch_replay_waveform_csvs(export_dir, rows, log=log)
    save_waterfall_replay_heatmap(export_dir, log=log)
    analyze_deep_memory_capture(export_dir, rows, log=log)
    if progress:
        progress(len(indices), len(indices), "Replay complete")
    return rows, nr_screens
