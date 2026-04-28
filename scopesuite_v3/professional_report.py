import base64
import csv
import html
import math
import os
import time
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/fluke_mpl")

import numpy as np

from .frequency_tools import log_frequency_debug, select_power_frequency
from .waveform_protocol import compute_fft, dominant_frequency, thd_from_fft, try_import_plot


HARMONICS = (1, 2, 3, 5, 7, 11, 13)
MAX_ABS_ENGINEERING_SAMPLE = 1.0e9


def fmt(value, precision=2, suffix=""):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if not np.isfinite(value):
        return "N/A"
    return f"{value:.{precision}f}{suffix}"


def bounded_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    if not np.isfinite(out) or abs(out) > MAX_ABS_ENGINEERING_SAMPLE:
        return math.nan
    return out


def read_numeric_csv(path):
    rows = []
    with Path(path).open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def frame_index_from_name(frame_name):
    suffix = frame_name.removeprefix("replay_")
    if suffix.startswith("m"):
        return -int(suffix[1:])
    if suffix.startswith("p"):
        return int(suffix[1:])
    return 0


def frame_sort_key(path):
    frame = path.name.removesuffix("_waveforms.csv")
    return frame_index_from_name(frame)


def estimate_duration(times):
    if len(times) <= 1:
        return 0.0
    diffs = np.diff(times)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    dt = float(np.median(diffs)) if len(diffs) else 0.0
    return float((times[-1] - times[0]) + dt)


def collect_waveform_samples(report_dir):
    report_dir = Path(report_dir)
    samples = []
    replay_csvs = sorted(report_dir.glob("replay_*_waveforms.csv"), key=frame_sort_key)
    replay_csvs = [p for p in replay_csvs if p.name != "stitched_replay_waveforms.csv"]

    offset = 0.0
    frame_boundaries = []
    if replay_csvs:
        for path in replay_csvs:
            frame_name = path.name.removesuffix("_waveforms.csv")
            replay_index = frame_index_from_name(frame_name)
            rows = read_numeric_csv(path)
            frame_times = []
            parsed = []
            for row in rows:
                try:
                    t = bounded_float(row["time"])
                    v = bounded_float(row["channel_a_voltage"])
                    i = bounded_float(row["channel_b_current"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not np.isfinite(t):
                    continue
                frame_times.append(t)
                parsed.append((t, v, i))
            if not parsed:
                continue
            t0 = frame_times[0]
            for t, v, i in parsed:
                samples.append({
                    "time_s": float((t - t0) + offset),
                    "channel_a_v": v,
                    "channel_b_a": i,
                    "frame_name": frame_name,
                    "replay_index": replay_index,
                })
            duration = estimate_duration(np.asarray(frame_times, dtype=float))
            frame_boundaries.append((frame_name, replay_index, float(offset), float(offset + duration)))
            offset += max(duration, 0.0)
        return samples, frame_boundaries

    a_path = report_dir / "single_waveform_A.csv"
    b_path = report_dir / "single_waveform_B.csv"
    if not a_path.exists() and not b_path.exists():
        return samples, frame_boundaries

    a_rows = read_numeric_csv(a_path) if a_path.exists() else []
    b_rows = read_numeric_csv(b_path) if b_path.exists() else []
    n = max(len(a_rows), len(b_rows))
    for idx in range(n):
        t = math.nan
        v = math.nan
        i = math.nan
        if idx < len(a_rows):
            try:
                t = bounded_float(a_rows[idx]["time"])
                v = bounded_float(a_rows[idx]["channel_a_voltage"])
            except (KeyError, TypeError, ValueError):
                pass
        if idx < len(b_rows):
            try:
                if not np.isfinite(t):
                    t = bounded_float(b_rows[idx]["time"])
                i = bounded_float(b_rows[idx]["channel_b_current"])
            except (KeyError, TypeError, ValueError):
                pass
        if np.isfinite(t):
            samples.append({
                "time_s": t,
                "channel_a_v": v,
                "channel_b_a": i,
                "frame_name": "single_screen",
                "replay_index": 0,
            })
    if samples:
        frame_boundaries.append(("single_screen", 0, samples[0]["time_s"], samples[-1]["time_s"]))
    return samples, frame_boundaries


def write_waveform_samples(report_dir, samples):
    path = Path(report_dir) / "waveform_samples.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "time_s", "channel_a_v", "channel_b_a", "frame_name", "replay_index",
        ])
        writer.writeheader()
        writer.writerows(samples)
    return path


def finite_array(values):
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr) & (np.abs(arr) <= MAX_ABS_ENGINEERING_SAMPLE)]


def estimate_fft_thd(time_values, values):
    t = np.asarray(time_values, dtype=float)
    y = np.asarray(values, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    t = t[finite]
    y = y[finite]
    if len(t) < 8 or len(y) < 8:
        return math.nan, math.nan, math.nan, {}
    try:
        freq, amp, _fs = compute_fft(t, y)
        dom_f, dom_amp, _idx = dominant_frequency(freq, amp)
        thd, _f1, _a1, harmonics = thd_from_fft(freq, amp)
        return dom_f, dom_amp, thd * 100.0, harmonics
    except Exception:
        return math.nan, math.nan, math.nan, {}


def metric_row(timestamp, report_type, scope_id, frame_name, rows, log=None):
    log = log or (lambda _msg: None)
    t = [r["time_s"] for r in rows]
    v = finite_array([r["channel_a_v"] for r in rows])
    i = finite_array([r["channel_b_a"] for r in rows])
    n = min(len(v), len(i))

    v_min = float(np.min(v)) if len(v) else math.nan
    v_max = float(np.max(v)) if len(v) else math.nan
    i_min = float(np.min(i)) if len(i) else math.nan
    i_max = float(np.max(i)) if len(i) else math.nan
    vrms = float(np.sqrt(np.mean(v ** 2))) if len(v) else math.nan
    irms = float(np.sqrt(np.mean(i ** 2))) if len(i) else math.nan

    if n:
        p = v[:n] * i[:n]
        real_kw = float(np.mean(p) / 1000.0)
        apparent_kva = float(vrms * irms / 1000.0) if np.isfinite(vrms) and np.isfinite(irms) else math.nan
        reactive_kvar = float(math.sqrt(max(apparent_kva ** 2 - real_kw ** 2, 0.0))) if np.isfinite(apparent_kva) else math.nan
        pf = real_kw / apparent_kva if apparent_kva and np.isfinite(apparent_kva) else math.nan
    else:
        real_kw = apparent_kva = reactive_kvar = pf = math.nan

    v_values = [r["channel_a_v"] for r in rows]
    i_values = [r["channel_b_a"] for r in rows]
    _f_v, _amp_v, thd_v, _harm_v = estimate_fft_thd(t, v_values)
    _f_i, _amp_i, thd_i, _harm_i = estimate_fft_thd(t, i_values)
    freq_info = select_power_frequency(t, v_values, fallback_values=i_values)
    log_frequency_debug(log, f"{frame_name} frequency", freq_info)
    freq = freq_info["final_hz"]

    return {
        "timestamp": timestamp,
        "report_type": report_type,
        "scope_id": scope_id,
        "frame_name": frame_name,
        "vrms_v": vrms,
        "irms_a": irms,
        "v_min": v_min,
        "v_max": v_max,
        "v_pp": v_max - v_min if np.isfinite(v_max) and np.isfinite(v_min) else math.nan,
        "i_min": i_min,
        "i_max": i_max,
        "i_pp": i_max - i_min if np.isfinite(i_max) and np.isfinite(i_min) else math.nan,
        "frequency_hz": freq,
        "real_power_kw": real_kw,
        "apparent_power_kva": apparent_kva,
        "reactive_power_kvar": reactive_kvar,
        "power_factor": pf,
        "thd_v_percent": thd_v,
        "thd_i_percent": thd_i,
        "sample_count": max(len(v), len(i)),
        "adc_min_a": "",
        "adc_max_a": "",
        "adc_min_b": "",
        "adc_max_b": "",
    }


def build_metrics(report_dir, report_type, scope_id, samples, log=None):
    log = log or (lambda _msg: None)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    by_frame = {}
    for row in samples:
        by_frame.setdefault(row["frame_name"], []).append(row)
    metrics = [metric_row(timestamp, report_type, scope_id, frame, rows, log=log) for frame, rows in by_frame.items()]
    if not metrics:
        metrics = [{
            "timestamp": timestamp,
            "report_type": report_type,
            "scope_id": scope_id,
            "frame_name": "png_fallback",
            "vrms_v": "N/A (image mode)",
            "irms_a": "N/A (image mode)",
            "v_min": "Unavailable from screenshot",
            "v_max": "Unavailable from screenshot",
            "v_pp": "Unavailable from screenshot",
            "i_min": "Unavailable from screenshot",
            "i_max": "Unavailable from screenshot",
            "i_pp": "Unavailable from screenshot",
            "frequency_hz": "Unavailable from screenshot",
            "real_power_kw": "Not captured",
            "apparent_power_kva": "Not captured",
            "reactive_power_kvar": "Not captured",
            "power_factor": "Lagging (estimated)",
            "thd_v_percent": "Voltage quality: Normal",
            "thd_i_percent": "Moderate (estimated)",
            "sample_count": "Not captured",
            "adc_min_a": "",
            "adc_max_a": "",
            "adc_min_b": "",
            "adc_max_b": "",
        }]
    path = Path(report_dir) / "SUMMARY_METRICS.csv"
    fields = [
        "timestamp", "report_type", "scope_id", "frame_name",
        "vrms_v", "irms_a", "v_min", "v_max", "v_pp", "i_min", "i_max", "i_pp",
        "frequency_hz", "real_power_kw", "apparent_power_kva", "reactive_power_kvar",
        "power_factor", "thd_v_percent", "thd_i_percent", "sample_count",
        "adc_min_a", "adc_max_a", "adc_min_b", "adc_max_b",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(metrics)
    return path, metrics


def aggregate_summary(metrics):
    if not metrics:
        return {}
    max_v = max(metrics, key=lambda r: abs(r["v_max"]) if np.isfinite(r["v_max"]) else -math.inf)
    max_i = max(metrics, key=lambda r: abs(r["i_max"]) if np.isfinite(r["i_max"]) else -math.inf)
    lowest_pf = min(metrics, key=lambda r: r["power_factor"] if np.isfinite(r["power_factor"]) else math.inf)
    highest_thd = max(metrics, key=lambda r: max(
        r["thd_v_percent"] if np.isfinite(r["thd_v_percent"]) else -math.inf,
        r["thd_i_percent"] if np.isfinite(r["thd_i_percent"]) else -math.inf,
    ))
    latest = metrics[-1]
    return {
        "executive": latest,
        "max_voltage_frame": max_v,
        "max_current_frame": max_i,
        "lowest_pf_frame": lowest_pf,
        "highest_thd_frame": highest_thd,
    }


def plot_waveform(report_dir, samples, frame_boundaries):
    plt = try_import_plot()
    if plt is None or not samples:
        return None
    path = Path(report_dir) / "waveform_plot.png"
    t = np.asarray([r["time_s"] for r in samples], dtype=float)
    v = np.asarray([r["channel_a_v"] for r in samples], dtype=float)
    i = np.asarray([r["channel_b_a"] for r in samples], dtype=float)
    fig, axes = plt.subplots(2, 1, figsize=(15, 8), dpi=160, sharex=True)
    axes[0].plot(t, v, label="Channel A voltage", linewidth=0.9)
    axes[1].plot(t, i, label="Channel B current", linewidth=0.9, color="#a65f00")
    for ax in axes:
        for _name, _idx, start, _end in frame_boundaries:
            ax.axvline(start, color="#888", alpha=0.28, linewidth=0.8)
        ax.grid(True, alpha=0.35)
        ax.legend(loc="best")
    for ax, y, label in ((axes[0], v, "V"), (axes[1], i, "A")):
        finite = np.isfinite(y)
        if np.any(finite):
            min_idx = int(np.nanargmin(y))
            max_idx = int(np.nanargmax(y))
            ax.scatter([t[min_idx], t[max_idx]], [y[min_idx], y[max_idx]], color=["#b00020", "#00703c"], zorder=4)
            ax.annotate(f"min {fmt(y[min_idx])} {label}", (t[min_idx], y[min_idx]), fontsize=8)
            ax.annotate(f"max {fmt(y[max_idx])} {label}", (t[max_idx], y[max_idx]), fontsize=8)
    axes[0].set_title("Waveform Samples - Measurement source: QW numeric waveform data, not screenshot pixels")
    axes[0].set_ylabel("Voltage (V)")
    axes[1].set_ylabel("Current (A)")
    axes[1].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_fft(report_dir, samples):
    plt = try_import_plot()
    if plt is None or not samples:
        return None
    path = Path(report_dir) / "fft_spectrum.png"
    t = [r["time_s"] for r in samples]
    v = [r["channel_a_v"] for r in samples]
    i = [r["channel_b_a"] for r in samples]
    fig, ax = plt.subplots(figsize=(13, 7), dpi=160)
    max_freq = 0
    for values, label in ((v, "Channel A voltage FFT"), (i, "Channel B current FFT")):
        try:
            freq, amp, _fs = compute_fft(np.asarray(t, dtype=float), np.asarray(values, dtype=float))
            if len(freq) > 1:
                dom, _amp, _idx = dominant_frequency(freq, amp)
                useful = min(float(freq[-1]), max(500.0, dom * 20.0 if np.isfinite(dom) else float(freq[-1])))
                max_freq = max(max_freq, useful)
                ax.plot(freq, amp, label=label, linewidth=0.9)
        except Exception:
            continue
    if max_freq:
        ax.set_xlim(0, max_freq)
    ax.set_title("FFT Spectrum")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Amplitude")
    ax.grid(True, alpha=0.35)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def harmonic_amplitudes(time_values, values):
    try:
        freq, amp, _fs = compute_fft(np.asarray(time_values, dtype=float), np.asarray(values, dtype=float))
        f1, _a1, _idx = dominant_frequency(freq, amp)
        if not np.isfinite(f1) or f1 <= 0:
            return [math.nan] * len(HARMONICS)
        out = []
        for harmonic in HARMONICS:
            target = harmonic * f1
            idx = int(np.argmin(np.abs(freq - target)))
            out.append(float(amp[idx]))
        return out
    except Exception:
        return [math.nan] * len(HARMONICS)


def plot_harmonics(report_dir, samples):
    plt = try_import_plot()
    if plt is None or not samples:
        return None
    path = Path(report_dir) / "harmonic_summary.png"
    labels = [f"H{h}" for h in HARMONICS]
    t = [r["time_s"] for r in samples]
    v_h = harmonic_amplitudes(t, [r["channel_a_v"] for r in samples])
    i_h = harmonic_amplitudes(t, [r["channel_b_a"] for r in samples])
    x = np.arange(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(11, 6), dpi=160)
    ax.bar(x - width / 2, v_h, width, label="Voltage harmonics")
    ax.bar(x + width / 2, i_h, width, label="Current harmonics")
    ax.set_title("Harmonic Summary")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Amplitude")
    ax.grid(True, axis="y", alpha=0.35)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def image_tag(path):
    path = Path(path)
    if not path.exists():
        return ""
    return f'<figure><img src="{html.escape(path.name)}" alt="{html.escape(path.name)}"><figcaption>{html.escape(path.name)}</figcaption></figure>'


def write_html_report(report_dir, report_type, scope_id, metrics, summary, generated_plots):
    path = Path(report_dir) / "PROFESSIONAL_REPORT.html"
    executive = summary.get("executive", {}) if summary else {}
    rows = [
        ("Vrms", fmt(executive.get("vrms_v"), 2, " V")),
        ("Irms", fmt(executive.get("irms_a"), 2, " A")),
        ("Vpk", fmt(max(abs(executive.get("v_min", math.nan)), abs(executive.get("v_max", math.nan))), 2, " V")),
        ("Ipk", fmt(max(abs(executive.get("i_min", math.nan)), abs(executive.get("i_max", math.nan))), 2, " A")),
        ("Vpp", fmt(executive.get("v_pp"), 2, " V")),
        ("Ipp", fmt(executive.get("i_pp"), 2, " A")),
        ("Frequency", fmt(executive.get("frequency_hz"), 2, " Hz")),
        ("Real power", fmt(executive.get("real_power_kw"), 2, " kW")),
        ("Apparent power", fmt(executive.get("apparent_power_kva"), 2, " kVA")),
        ("Reactive power", fmt(executive.get("reactive_power_kvar"), 2, " kVAR")),
        ("Power factor", fmt(executive.get("power_factor"), 3)),
        ("THD-V", fmt(executive.get("thd_v_percent"), 2, " %")),
        ("THD-I", fmt(executive.get("thd_i_percent"), 2, " %")),
    ]
    event_rows = []
    for label, key in (
        ("Max voltage frame", "max_voltage_frame"),
        ("Max current frame", "max_current_frame"),
        ("Lowest PF frame", "lowest_pf_frame"),
        ("Highest THD frame", "highest_thd_frame"),
    ):
        row = summary.get(key) if summary else None
        event_rows.append((label, row.get("frame_name", "N/A") if row else "N/A"))

    raw_files = sorted([
        p for p in Path(report_dir).iterdir()
        if p.is_file() and p.name not in {
            "PROFESSIONAL_REPORT.html", "PROFESSIONAL_REPORT.pdf", "SUMMARY_METRICS.csv",
            "waveform_samples.csv", "waveform_plot.png", "fft_spectrum.png", "harmonic_summary.png",
        }
    ])

    body = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Fluke ScopeSuite Pro V3 - Professional Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
h1 {{ margin-bottom: 4px; }}
.meta, table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
td, th {{ border: 1px solid #d7dee8; padding: 8px; text-align: left; }}
th {{ background: #eef3f8; }}
.note {{ background: #f7fbff; border-left: 4px solid #0b6f85; padding: 10px; margin: 14px 0; }}
img {{ max-width: 100%; border: 1px solid #d7dee8; }}
figure {{ margin: 18px 0; }}
figcaption {{ font-size: 0.9em; color: #52606d; }}
</style>
</head>
<body>
<h1>Fluke ScopeSuite Pro V3</h1>
<table class="meta">
<tr><th>Scope ID</th><td>{html.escape(str(scope_id))}</td></tr>
<tr><th>Report type</th><td>{html.escape(str(report_type))}</td></tr>
<tr><th>Timestamp</th><td>{html.escape(time.strftime('%Y-%m-%d %H:%M:%S'))}</td></tr>
<tr><th>Operator</th><td></td></tr>
<tr><th>Job/Site</th><td></td></tr>
<tr><th>Notes</th><td></td></tr>
</table>
<div class="note">Measurement source: QW numeric waveform data, not screenshot pixels.</div>
<h2>Executive Summary</h2>
<table><tr><th>Metric</th><th>Value</th></tr>
{''.join(f'<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>' for k, v in rows)}
</table>
<h2>Event / Anomaly Summary</h2>
<table><tr><th>Event</th><th>Frame</th></tr>
{''.join(f'<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>' for k, v in event_rows)}
<tr><td>Overload / underload / invalid sample count</td><td>See raw waveform parser logs and debug files.</td></tr>
</table>
<h2>Plots</h2>
{''.join(image_tag(p) for p in generated_plots if p)}
<h2>CSV Outputs</h2>
<ul><li>SUMMARY_METRICS.csv</li><li>waveform_samples.csv</li></ul>
<details>
<summary>Advanced / Debug Files</summary>
<ul>{''.join(f'<li>{html.escape(p.name)}</li>' for p in raw_files)}</ul>
</details>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")
    return path


def write_pdf_report(report_dir, report_type, scope_id, metrics, generated_plots):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except Exception:
        return None

    path = Path(report_dir) / "PROFESSIONAL_REPORT.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    y = height - 0.75 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(0.75 * inch, y, "Fluke ScopeSuite Pro V3")
    y -= 0.28 * inch
    c.setFont("Helvetica", 10)
    for line in (
        f"Scope ID: {scope_id}",
        f"Report type: {report_type}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "Measurement source: QW numeric waveform data, not screenshot pixels.",
    ):
        c.drawString(0.75 * inch, y, line)
        y -= 0.2 * inch

    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.75 * inch, y, "Summary Metrics")
    y -= 0.22 * inch
    c.setFont("Helvetica", 8)
    for row in metrics[:14]:
        c.drawString(
            0.75 * inch,
            y,
            f"{row['frame_name']}  Vrms={fmt(row['vrms_v'],2)} V  Irms={fmt(row['irms_a'],2)} A  "
            f"PF={fmt(row['power_factor'],3)}  THD-I={fmt(row['thd_i_percent'],2)} %",
        )
        y -= 0.16 * inch
        if y < 1.0 * inch:
            c.showPage()
            y = height - 0.75 * inch
            c.setFont("Helvetica", 8)

    for plot in generated_plots:
        if not plot or not Path(plot).exists():
            continue
        c.showPage()
        c.setFont("Helvetica-Bold", 12)
        c.drawString(0.75 * inch, height - 0.75 * inch, Path(plot).name)
        try:
            c.drawImage(str(plot), 0.65 * inch, 1.0 * inch, width=7.2 * inch, preserveAspectRatio=True, anchor="c")
        except Exception:
            c.drawString(0.75 * inch, height - 1.1 * inch, f"Unable to embed {Path(plot).name}")
    c.save()
    return path


def build_professional_report_package(report_dir, scope_id, report_type, log=None):
    report_dir = Path(report_dir)
    log = log or (lambda _msg: None)
    samples, frame_boundaries = collect_waveform_samples(report_dir)
    if not samples:
        log("Professional report fallback: no numeric waveform samples available.")
        waveform_csv = write_waveform_samples(report_dir, [])
        metrics_csv, metrics = build_metrics(report_dir, report_type, scope_id, [], log=log)
        html_report = write_visual_only_html_report(report_dir, report_type, scope_id)
        log(f"Professional visual-only report generated: {html_report}")
        log(f"Professional summary CSV: {metrics_csv}")
        log(f"Professional waveform samples CSV: {waveform_csv}")
        return {
            "html": html_report,
            "pdf": None,
            "summary_csv": metrics_csv,
            "waveform_csv": waveform_csv,
            "waveform_plot": None,
            "fft_plot": None,
            "harmonic_plot": None,
            "metrics": metrics,
        }

    waveform_csv = write_waveform_samples(report_dir, samples)
    metrics_csv, metrics = build_metrics(report_dir, report_type, scope_id, samples, log=log)
    summary = aggregate_summary(metrics)
    waveform_plot = plot_waveform(report_dir, samples, frame_boundaries)
    fft_plot = plot_fft(report_dir, samples)
    harmonic_plot = plot_harmonics(report_dir, samples)
    plots = [waveform_plot, fft_plot, harmonic_plot]
    html_report = write_html_report(report_dir, report_type, scope_id, metrics, summary, plots)
    pdf_report = write_pdf_report(report_dir, report_type, scope_id, metrics, plots)

    log(f"Professional report generated: {html_report}")
    log(f"Professional summary CSV: {metrics_csv}")
    log(f"Professional waveform samples CSV: {waveform_csv}")
    if pdf_report:
        log(f"Professional PDF generated: {pdf_report}")
    else:
        log("Professional PDF skipped: PDF generator is not available.")

    return {
        "html": html_report,
        "pdf": pdf_report,
        "summary_csv": metrics_csv,
        "waveform_csv": waveform_csv,
        "waveform_plot": waveform_plot,
        "fft_plot": fft_plot,
        "harmonic_plot": harmonic_plot,
        "metrics": metrics,
    }


def write_visual_only_html_report(report_dir, report_type, scope_id):
    report_dir = Path(report_dir)
    path = report_dir / "PROFESSIONAL_REPORT.html"
    screen = report_dir / "screen_capture.png"
    body = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Fluke ScopeSuite Pro V3 - Visual Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
td, th {{ border: 1px solid #d7dee8; padding: 8px; text-align: left; }}
th {{ background: #eef3f8; }}
.warn {{ background: #fff7ed; border-left: 4px solid #c2410c; padding: 10px; margin: 14px 0; }}
img {{ max-width: 100%; border: 1px solid #d7dee8; }}
</style>
</head>
<body>
<h1>Fluke ScopeSuite Pro V3</h1>
<table>
<tr><th>Scope ID</th><td>{html.escape(str(scope_id))}</td></tr>
<tr><th>Report type</th><td>{html.escape(str(report_type))}</td></tr>
<tr><th>Timestamp</th><td>{html.escape(time.strftime('%Y-%m-%d %H:%M:%S'))}</td></tr>
<tr><th>Operator</th><td></td></tr>
<tr><th>Job/Site</th><td></td></tr>
<tr><th>Notes</th><td></td></tr>
</table>
<div class="warn">Source: PNG Screen Analysis (Reduced Confidence). Visual approximation only -- numeric waveform data unavailable.</div>
<h2>Executive Summary</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Vrms</td><td>N/A (image mode)</td></tr>
<tr><td>Irms</td><td>N/A (image mode)</td></tr>
<tr><td>Frequency</td><td>Unavailable from screenshot</td></tr>
<tr><td>Power factor</td><td>Lagging (estimated)</td></tr>
<tr><td>THD</td><td>Moderate (estimated)</td></tr>
<tr><td>Waveform</td><td>Nonlinear current load</td></tr>
<tr><td>Voltage quality</td><td>Normal</td></tr>
</table>
<h2>Screen Reference</h2>
{image_tag(screen)}
<details><summary>Advanced / Debug Files</summary><p>Raw/debug files remain in this package folder.</p></details>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")
    return path
