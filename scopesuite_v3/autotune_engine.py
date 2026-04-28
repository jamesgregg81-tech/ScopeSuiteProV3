import csv
import html
import json
import math
import time
from pathlib import Path

try:
    import numpy as np
except Exception:
    np = None

from .waveform_protocol import analyze_voltage_current, compute_fft, query_waveform, thd_from_fft


SAFETY_WARNING = (
    "AutoTune provides recommendations only. Generator controller adjustments "
    "must be performed by qualified personnel."
)


def capture_waveform_pair(ser, client, export_dir, prefix, log=None):
    log = log or (lambda _msg: None)
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    log(f"AutoTune: capturing Channel A voltage for {prefix}")
    wf_a, raw_a = query_waveform(ser, client, "10")
    (export_dir / f"{prefix}_channel_A_raw.bin").write_bytes(raw_a)

    log(f"AutoTune: capturing Channel B current for {prefix}")
    wf_b, raw_b = query_waveform(ser, client, "20")
    (export_dir / f"{prefix}_channel_B_raw.bin").write_bytes(raw_b)

    csv_path = export_dir / f"{prefix}_waveform.csv"
    save_waveform_csv(csv_path, wf_a, wf_b)
    return {
        "name": prefix,
        "wf_a": wf_a,
        "wf_b": wf_b,
        "csv_path": str(csv_path),
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_waveform_csv(path, wf_a, wf_b):
    n = min(len(wf_a["x"]), len(wf_a["y"]), len(wf_b["y"]))
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "voltage_ch_a", "current_ch_b"])
        for idx in range(n):
            writer.writerow([float(wf_a["x"][idx]), float(wf_a["y"][idx]), float(wf_b["y"][idx])])


def _finite_arrays(wf):
    if np is None:
        raise RuntimeError("NumPy is required for AutoTune waveform analysis.")
    x = np.asarray(wf["x"], dtype=float)
    y = np.asarray(wf["y"], dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    return x[finite], y[finite]


def _rms(y):
    return float(np.sqrt(np.mean(np.asarray(y, dtype=float) ** 2))) if len(y) else float("nan")


def _estimate_frequency(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 4:
        return float("nan")
    y = y - np.mean(y)
    crossings = []
    for idx in range(1, len(y)):
        if y[idx - 1] <= 0 < y[idx]:
            denom = y[idx] - y[idx - 1]
            if abs(denom) < 1e-12:
                crossings.append(x[idx])
            else:
                frac = -y[idx - 1] / denom
                crossings.append(x[idx - 1] + frac * (x[idx] - x[idx - 1]))
    if len(crossings) < 2:
        return float("nan")
    periods = np.diff(np.asarray(crossings))
    periods = periods[np.isfinite(periods) & (periods > 0)]
    return float(1.0 / np.median(periods)) if len(periods) else float("nan")


def _window_series(wf, nominal_freq=60.0):
    x, y = _finite_arrays(wf)
    if len(x) < 16:
        return []
    dt = float(np.median(np.diff(x)))
    if not np.isfinite(dt) or dt <= 0:
        return []
    cycles = 6.0
    freq = nominal_freq if np.isfinite(nominal_freq) and nominal_freq > 0 else 60.0
    window_n = int(max(16, min(len(y), round(cycles / freq / dt))))
    step_n = max(4, window_n // 4)

    series = []
    for start in range(0, len(y) - window_n + 1, step_n):
        end = start + window_n
        xs = x[start:end]
        ys = y[start:end]
        series.append({
            "time_s": float(xs[len(xs) // 2] - x[0]),
            "rms": _rms(ys),
            "freq_hz": _estimate_frequency(xs, ys),
        })
    return series


def _first_recovery_time(series, key, nominal, tolerance, start_index):
    if not series or not np.isfinite(nominal):
        return float("nan")
    for idx in range(max(0, start_index), len(series)):
        tail = series[idx:idx + 3]
        if len(tail) < 3:
            break
        if all(np.isfinite(p[key]) and abs(p[key] - nominal) <= tolerance for p in tail):
            return max(0.0, float(tail[0]["time_s"] - series[start_index]["time_s"]))
    return float("nan")


def _oscillation_detected(values, nominal, threshold):
    vals = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if len(vals) < 8 or not np.isfinite(nominal):
        return False
    centered = vals - nominal
    sign_changes = int(np.sum(np.diff(np.signbit(centered)) != 0))
    amplitude = float(np.nanmax(vals) - np.nanmin(vals))
    return sign_changes >= 4 and amplitude >= threshold


def _session_metrics(capture):
    wf_a = capture["wf_a"]
    wf_b = capture["wf_b"]
    freq_a, amp_a, _fs_a = compute_fft(wf_a["x"], wf_a["y"])
    freq_b, amp_b, _fs_b = compute_fft(wf_b["x"], wf_b["y"])
    thd_v, f1_v, _a1_v, _harm_v = thd_from_fft(freq_a, amp_a)
    thd_i, _f1_i, _a1_i, _harm_i = thd_from_fft(freq_b, amp_b)
    power = analyze_voltage_current(wf_a["y"], wf_b["y"], wf_a["delta_x"])
    return {
        "voltage_v": float(power["vrms_v"]),
        "frequency_hz": float(f1_v if np.isfinite(f1_v) else power["fundamental_hz"]),
        "current_a": float(power["irms_a"]),
        "power_factor": float(power["power_factor"]),
        "thd_v_percent": float(thd_v * 100.0) if np.isfinite(thd_v) else float("nan"),
        "thd_i_percent": float(thd_i * 100.0) if np.isfinite(thd_i) else float("nan"),
    }


def analyze_autotune_session(baseline_capture, load_capture):
    if np is None:
        raise RuntimeError("NumPy is required for AutoTune analysis.")

    baseline = _session_metrics(baseline_capture)
    load = _session_metrics(load_capture)
    nominal_v = baseline["voltage_v"]
    nominal_f = baseline["frequency_hz"]

    v_series = _window_series(load_capture["wf_a"], nominal_f)
    f_values = [p["freq_hz"] for p in v_series]
    v_values = [p["rms"] for p in v_series]

    min_v = float(np.nanmin(v_values)) if v_values else float("nan")
    max_v = float(np.nanmax(v_values)) if v_values else float("nan")
    min_f = float(np.nanmin(f_values)) if f_values else float("nan")
    max_f = float(np.nanmax(f_values)) if f_values else float("nan")
    min_v_index = int(np.nanargmin(v_values)) if v_values else 0
    min_f_index = int(np.nanargmin(f_values)) if f_values else 0

    voltage_dip_pct = max(0.0, (nominal_v - min_v) / nominal_v * 100.0) if nominal_v else float("nan")
    voltage_overshoot_pct = max(0.0, (max_v - nominal_v) / nominal_v * 100.0) if nominal_v else float("nan")
    frequency_dip_hz = max(0.0, nominal_f - min_f) if np.isfinite(nominal_f) else float("nan")
    frequency_overshoot_hz = max(0.0, max_f - nominal_f) if np.isfinite(nominal_f) else float("nan")
    voltage_recovery_s = _first_recovery_time(v_series, "rms", nominal_v, nominal_v * 0.02, min_v_index)
    frequency_recovery_s = _first_recovery_time(v_series, "freq_hz", nominal_f, 0.5, min_f_index)
    recovery_time_s = float(np.nanmax([voltage_recovery_s, frequency_recovery_s]))

    governor_hunting = _oscillation_detected(f_values[min_f_index:], nominal_f, 0.8)
    avr_oscillation = _oscillation_detected(v_values[min_v_index:], nominal_v, nominal_v * 0.04)
    slow_voltage_recovery = np.isfinite(voltage_recovery_s) and voltage_recovery_s > 5.0
    slow_frequency_recovery = np.isfinite(frequency_recovery_s) and frequency_recovery_s > 5.0
    excessive_thd = (
        np.isfinite(load["thd_v_percent"]) and load["thd_v_percent"] > 8.0
    ) or (
        np.isfinite(load["thd_i_percent"]) and load["thd_i_percent"] > 20.0
    )
    unstable_pf = np.isfinite(load["power_factor"]) and abs(load["power_factor"]) < 0.75
    voltage_collapse_underfrequency = voltage_dip_pct > 15.0 and frequency_dip_hz > 2.0
    excessive_overshoot = voltage_overshoot_pct > 8.0 or frequency_overshoot_hz > 1.0

    recommendations = []
    if governor_hunting:
        recommendations.append(("Reduce GOV Gain", "Frequency oscillation/hunting detected."))
    if slow_frequency_recovery:
        recommendations.append(("Increase GOV Gain slightly or GOV Integral", "Frequency recovery is slow after load step."))
    if avr_oscillation:
        recommendations.append(("Reduce REG Gain", "Voltage oscillation detected after load step."))
    if slow_voltage_recovery:
        recommendations.append(("Increase REG Integral or REG Gain slightly", "Voltage recovery is slow or droopy."))
    if voltage_collapse_underfrequency:
        recommendations.append(("Review REG V/Hz", "Voltage collapses during underfrequency."))
    if excessive_overshoot:
        if governor_hunting or avr_oscillation:
            recommendations.append(("Reduce gain", "Overshoot plus oscillation suggests excessive gain."))
        else:
            recommendations.append(("Reduce integral slightly", "Overshoot without sustained oscillation suggests integral windup."))
    if excessive_thd:
        recommendations.append(("Review load waveform and harmonic content", "THD is elevated during load step."))
    if unstable_pf:
        recommendations.append(("Review load power factor and CT scaling", "Power factor is unstable or unusually low."))
    if not recommendations:
        recommendations.append(("No tuning change recommended", "Response appears stable within V2 rule thresholds."))

    pass_fail = "PASS"
    if any([governor_hunting, avr_oscillation, slow_voltage_recovery, slow_frequency_recovery, voltage_collapse_underfrequency, excessive_overshoot]):
        pass_fail = "REVIEW"
    if voltage_dip_pct > 25.0 or frequency_dip_hz > 5.0:
        pass_fail = "FAIL"

    return {
        "baseline": baseline,
        "load": load,
        "series": v_series,
        "conditions": {
            "voltage_dip_percent": voltage_dip_pct,
            "frequency_dip_hz": frequency_dip_hz,
            "voltage_overshoot_percent": voltage_overshoot_pct,
            "frequency_overshoot_hz": frequency_overshoot_hz,
            "recovery_time_sec": recovery_time_s,
            "voltage_recovery_sec": voltage_recovery_s,
            "frequency_recovery_sec": frequency_recovery_s,
            "governor_hunting": governor_hunting,
            "avr_oscillation": avr_oscillation,
            "slow_voltage_recovery": slow_voltage_recovery,
            "slow_frequency_recovery": slow_frequency_recovery,
            "excessive_thd": excessive_thd,
            "unstable_power_factor": unstable_pf,
            "voltage_collapse_underfrequency": voltage_collapse_underfrequency,
            "excessive_overshoot": excessive_overshoot,
        },
        "recommendations": recommendations,
        "pass_fail": pass_fail,
    }


def _fmt(value, digits=2, suffix=""):
    if isinstance(value, bool):
        return "YES" if value else "NO"
    try:
        if math.isfinite(float(value)):
            return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        pass
    return "n/a"


def write_autotune_package(export_dir, baseline_capture, load_capture, analysis, settings, notes, technician=""):
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "safety_warning": SAFETY_WARNING,
        "technician": technician,
        "settings": settings,
        "notes": notes,
        "analysis": analysis,
        "baseline_csv": baseline_capture.get("csv_path"),
        "load_step_csv": load_capture.get("csv_path"),
    }
    (export_dir / "autotune_analysis.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    recovery_plot = export_dir / "voltage_frequency_recovery.png"
    fft_plot = export_dir / "fft_harmonic_plot.png"
    _write_plots(recovery_plot, fft_plot, load_capture, analysis)

    html_path = export_dir / "AUTOTUNE_REPORT.html"
    html_path.write_text(_report_html(metadata, recovery_plot, fft_plot), encoding="utf-8")
    pdf_path = _write_pdf_if_available(export_dir, metadata)
    return {
        "html": html_path,
        "pdf": pdf_path,
        "recovery_plot": recovery_plot if recovery_plot.exists() else None,
        "fft_plot": fft_plot if fft_plot.exists() else None,
        "json": export_dir / "autotune_analysis.json",
    }


def _write_plots(recovery_plot, fft_plot, load_capture, analysis):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return

    series = analysis.get("series", [])
    if series:
        t = [p["time_s"] for p in series]
        v = [p["rms"] for p in series]
        f = [p["freq_hz"] for p in series]
        fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        axes[0].plot(t, v, marker="o")
        axes[0].set_ylabel("Voltage RMS")
        axes[0].grid(True)
        axes[1].plot(t, f, marker="o")
        axes[1].set_ylabel("Frequency Hz")
        axes[1].set_xlabel("Time after capture start (s)")
        axes[1].grid(True)
        fig.suptitle("AutoTune Load-Step Recovery")
        fig.tight_layout()
        fig.savefig(recovery_plot, dpi=150)
        plt.close(fig)

    try:
        wf_a = load_capture["wf_a"]
        wf_b = load_capture["wf_b"]
        freq_a, amp_a, _ = compute_fft(wf_a["x"], wf_a["y"])
        freq_b, amp_b, _ = compute_fft(wf_b["x"], wf_b["y"])
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(freq_a, amp_a, label="Voltage")
        ax.plot(freq_b, amp_b, label="Current")
        ax.set_xlim(left=0, right=min(1000, max(freq_a[-1], freq_b[-1])))
        ax.set_xlabel("Hz")
        ax.set_ylabel("Amplitude")
        ax.grid(True)
        ax.legend()
        fig.suptitle("AutoTune FFT / Harmonic View")
        fig.tight_layout()
        fig.savefig(fft_plot, dpi=150)
        plt.close(fig)
    except Exception:
        pass


def _report_html(metadata, recovery_plot, fft_plot):
    analysis = metadata["analysis"]
    conditions = analysis["conditions"]
    recs = analysis["recommendations"]
    settings = metadata["settings"]

    cond_rows = "\n".join(
        f"<tr><th>{html.escape(k.replace('_', ' ').title())}</th><td>{html.escape(_fmt(v))}</td></tr>"
        for k, v in conditions.items()
    )
    rec_rows = "\n".join(
        f"<tr><td>{html.escape(action)}</td><td>{html.escape(reason)}</td></tr>"
        for action, reason in recs
    )
    setting_rows = "\n".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v.get('before', '')))}</td><td>{html.escape(str(v.get('after', '')))}</td></tr>"
        for k, v in settings.items()
    )

    images = []
    if Path(recovery_plot).exists():
        images.append(f'<h2>Voltage / Frequency Recovery</h2><img src="{html.escape(Path(recovery_plot).name)}" />')
    if Path(fft_plot).exists():
        images.append(f'<h2>FFT / Harmonic Plot</h2><img src="{html.escape(Path(fft_plot).name)}" />')

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Generator AutoTune Report</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 28px; color: #111; }}
.warning {{ padding: 14px; background: #fff3cd; border: 2px solid #9a6700; font-weight: bold; }}
.status {{ font-size: 24px; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 14px 0; }}
th, td {{ border: 1px solid #999; padding: 8px; text-align: left; }}
th {{ background: #f0f0f0; }}
img {{ max-width: 100%; border: 1px solid #aaa; }}
</style>
</head>
<body>
<h1>Generator AutoTune Report</h1>
<div class="warning">{html.escape(SAFETY_WARNING)}</div>
<p class="status">Pass / Fail Summary: {html.escape(analysis["pass_fail"])}</p>
<p><b>Created:</b> {html.escape(metadata["created_at"])}</p>
<p><b>Technician:</b> {html.escape(metadata.get("technician", ""))}</p>
<h2>Live Summary</h2>
<table>
<tr><th>Metric</th><th>Baseline</th><th>Load Step</th></tr>
<tr><td>Voltage</td><td>{_fmt(analysis["baseline"]["voltage_v"], 2, " V")}</td><td>{_fmt(analysis["load"]["voltage_v"], 2, " V")}</td></tr>
<tr><td>Frequency</td><td>{_fmt(analysis["baseline"]["frequency_hz"], 2, " Hz")}</td><td>{_fmt(analysis["load"]["frequency_hz"], 2, " Hz")}</td></tr>
<tr><td>Current</td><td>{_fmt(analysis["baseline"]["current_a"], 2, " A")}</td><td>{_fmt(analysis["load"]["current_a"], 2, " A")}</td></tr>
<tr><td>Power Factor</td><td>{_fmt(analysis["baseline"]["power_factor"], 3)}</td><td>{_fmt(analysis["load"]["power_factor"], 3)}</td></tr>
<tr><td>THD-V</td><td>{_fmt(analysis["baseline"]["thd_v_percent"], 2, "%")}</td><td>{_fmt(analysis["load"]["thd_v_percent"], 2, "%")}</td></tr>
<tr><td>THD-I</td><td>{_fmt(analysis["baseline"]["thd_i_percent"], 2, "%")}</td><td>{_fmt(analysis["load"]["thd_i_percent"], 2, "%")}</td></tr>
</table>
<h2>Detected Conditions</h2>
<table>{cond_rows}</table>
<h2>Recommendations</h2>
<table><tr><th>Recommendation</th><th>Reason</th></tr>{rec_rows}</table>
<h2>Before / After GOV and REG Settings</h2>
<table><tr><th>Setting</th><th>Before</th><th>After</th></tr>{setting_rows}</table>
<h2>Technician Notes</h2>
<pre>{html.escape(metadata.get("notes", ""))}</pre>
{''.join(images)}
</body>
</html>
"""


def _write_pdf_if_available(export_dir, metadata):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except Exception:
        return None

    path = Path(export_dir) / "AUTOTUNE_REPORT.pdf"
    analysis = metadata["analysis"]
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Generator AutoTune Report")
    y -= 28
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, SAFETY_WARNING[:110])
    y -= 28
    c.setFont("Helvetica", 11)
    c.drawString(50, y, f"Pass / Fail Summary: {analysis['pass_fail']}")
    y -= 18
    for action, reason in analysis["recommendations"][:8]:
        c.drawString(50, y, f"- {action}: {reason}"[:110])
        y -= 16
        if y < 60:
            c.showPage()
            y = height - 50
    c.save()
    return path
