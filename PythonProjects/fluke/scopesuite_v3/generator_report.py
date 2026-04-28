import csv
import html
import math
import shutil
import time
from pathlib import Path

import numpy as np

from .frequency_tools import select_power_frequency
from .professional_report import fmt, read_numeric_csv
from .waveform_protocol import try_import_plot


WARNING_TEXT = "Control adjustments must be performed by qualified personnel only."


SITE_FIELDS = [
    "customer",
    "site_name",
    "generator_id",
    "engine_model",
    "alternator_model",
    "controller_type",
    "kw_rating",
    "kva_rating",
    "voltage",
    "phase",
    "frequency",
    "technician",
    "date",
]

SETTING_FIELDS = [
    "gov_gain_percent",
    "gov_integral_percent",
    "gov_ramp_sec",
    "reg_gain_percent",
    "reg_integral_percent",
    "reg_vhz",
]

RESULT_FIELDS = [
    "no_load_voltage",
    "no_load_frequency",
    "full_load_frequency",
    "steady_state_voltage",
    "steady_state_frequency",
    "max_voltage_dip_percent",
    "max_frequency_dip_hz",
    "max_overshoot_percent",
    "recovery_time_sec",
    "settling_time_sec",
    "thd_under_load_percent",
    "pf_under_load",
    "step_load_acceptance_score",
]


def safe_float(value, default=math.nan):
    try:
        value = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return value if np.isfinite(value) else default


def copy_evidence(evidence_dir, report_dir, log=None):
    log = log or (lambda _msg: None)
    copied = {"waveforms": [], "screenshots": [], "plots": []}
    if not evidence_dir:
        return copied
    evidence_dir = Path(evidence_dir)
    if not evidence_dir.exists():
        log(f"Generator report evidence folder not found: {evidence_dir}")
        return copied

    waveform_dir = report_dir / "waveform_evidence"
    screenshot_dir = report_dir / "screenshots"
    plots_dir = report_dir / "plots"
    waveform_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)

    for path in evidence_dir.iterdir():
        if not path.is_file():
            continue
        lower = path.name.lower()
        if lower.endswith(".csv") or lower.endswith(".bin"):
            target = waveform_dir / path.name
            shutil.copyfile(path, target)
            copied["waveforms"].append(target)
        elif lower.endswith((".png", ".jpg", ".jpeg")):
            if "screen" in lower or "decoded" in lower:
                target = screenshot_dir / path.name
                copied["screenshots"].append(target)
            else:
                target = plots_dir / path.name
                copied["plots"].append(target)
            shutil.copyfile(path, target)
        elif lower.endswith((".html", ".txt", ".pdf")):
            target = report_dir / f"evidence_{path.name}"
            shutil.copyfile(path, target)

    return copied


def collect_waveform_rows(report_dir):
    single_a = report_dir / "waveform_evidence" / "single_waveform_A.csv"
    single_b = report_dir / "waveform_evidence" / "single_waveform_B.csv"
    if single_a.exists() and single_b.exists():
        combined = combine_single_waveform_csvs(single_a, single_b)
        if combined:
            return combined, single_a

    candidates = [
        report_dir / "waveform_evidence" / "waveform_samples.csv",
        report_dir / "waveform_evidence" / "single_waveform_A.csv",
        report_dir / "waveform_evidence" / "stitched_replay_waveforms.csv",
        report_dir / "waveform_evidence" / "replay_summary.csv",
    ]
    candidates.extend(sorted((report_dir / "waveform_evidence").glob("*waveforms.csv")))

    for path in candidates:
        if not path.exists():
            continue
        rows = read_numeric_csv(path)
        parsed = parse_waveform_rows(path, rows)
        if parsed:
            return parsed, path
    return [], None


def combine_single_waveform_csvs(path_a, path_b):
    rows_a = read_numeric_csv(path_a)
    rows_b = read_numeric_csv(path_b)
    count = max(len(rows_a), len(rows_b))
    parsed = []
    for idx in range(count):
        t = math.nan
        v = math.nan
        i = math.nan
        if idx < len(rows_a):
            try:
                t = float(rows_a[idx]["time"])
                v = float(rows_a[idx]["channel_a_voltage"])
            except (KeyError, TypeError, ValueError):
                pass
        if idx < len(rows_b):
            try:
                if not np.isfinite(t):
                    t = float(rows_b[idx]["time"])
                i = float(rows_b[idx]["channel_b_current"])
            except (KeyError, TypeError, ValueError):
                pass
        if np.isfinite(t) and (np.isfinite(v) or np.isfinite(i)):
            parsed.append({"time_s": t, "voltage_v": v, "current_a": i, "source_file": path_a.name})
    return parsed


def parse_waveform_rows(path, rows):
    parsed = []
    for row in rows:
        try:
            if "time_s" in row:
                t = float(row["time_s"])
            elif "stitched_time" in row:
                t = float(row["stitched_time"])
            else:
                t = float(row["time"])

            if "channel_a_v" in row:
                v = float(row["channel_a_v"])
            elif "channel_a_voltage" in row:
                v = float(row["channel_a_voltage"])
            else:
                v = math.nan

            if "channel_b_a" in row:
                i = float(row["channel_b_a"])
            elif "channel_b_current" in row:
                i = float(row["channel_b_current"])
            else:
                i = math.nan
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(t) and (np.isfinite(v) or np.isfinite(i)):
            parsed.append({"time_s": t, "voltage_v": v, "current_a": i, "source_file": path.name})
    return parsed


def analyze_waveform_results(rows, nominal_voltage, nominal_frequency):
    if not rows:
        return {}, {
            "governor_hunting": False,
            "voltage_oscillation": False,
            "slow_recovery": False,
            "overdamped_response": False,
            "underdamped_response": False,
            "frequency_sag": False,
            "excess_thd": False,
        }

    t = np.asarray([r["time_s"] for r in rows], dtype=float)
    v = np.asarray([r["voltage_v"] for r in rows], dtype=float)
    i = np.asarray([r["current_a"] for r in rows], dtype=float)
    finite_v = v[np.isfinite(v)]
    finite_i = i[np.isfinite(i)]
    nominal_voltage = safe_float(nominal_voltage)
    nominal_frequency = safe_float(nominal_frequency, 60.0)

    freq_info = select_power_frequency(t, v)
    measured_frequency = freq_info["final_hz"]

    steady_voltage = float(np.sqrt(np.mean(finite_v ** 2))) if len(finite_v) else math.nan
    v_min = float(np.min(finite_v)) if len(finite_v) else math.nan
    v_max = float(np.max(finite_v)) if len(finite_v) else math.nan
    i_max = float(np.max(np.abs(finite_i))) if len(finite_i) else math.nan
    voltage_ref = nominal_voltage if np.isfinite(nominal_voltage) and nominal_voltage > 0 else steady_voltage

    voltage_dip_percent = math.nan
    overshoot_percent = math.nan
    if np.isfinite(voltage_ref) and voltage_ref > 0 and np.isfinite(v_min) and np.isfinite(v_max):
        voltage_dip_percent = max(0.0, (voltage_ref - v_min) / voltage_ref * 100.0)
        overshoot_percent = max(0.0, (v_max - voltage_ref) / voltage_ref * 100.0)

    settling_time = estimate_settling_time(t, v, voltage_ref, tolerance_percent=2.0)
    recovery_time = settling_time
    freq_dip = abs(nominal_frequency - measured_frequency) if np.isfinite(measured_frequency) else math.nan
    thd = estimate_thd_percent(t, v)
    pf = estimate_power_factor(v, i)

    diagnostics = detect_response_diagnostics(
        t,
        v,
        measured_frequency,
        nominal_frequency,
        voltage_dip_percent,
        overshoot_percent,
        settling_time,
        thd,
    )
    score = step_load_score(voltage_dip_percent, freq_dip, settling_time, overshoot_percent, thd)

    return {
        "steady_state_voltage": steady_voltage,
        "steady_state_frequency": measured_frequency,
        "no_load_voltage": steady_voltage,
        "no_load_frequency": measured_frequency,
        "full_load_frequency": measured_frequency,
        "max_voltage_dip_percent": voltage_dip_percent,
        "max_frequency_dip_hz": freq_dip,
        "max_overshoot_percent": overshoot_percent,
        "recovery_time_sec": recovery_time,
        "settling_time_sec": settling_time,
        "thd_under_load_percent": thd,
        "pf_under_load": pf,
        "step_load_acceptance_score": score,
        "current_inrush_a": i_max,
        "sample_count": len(rows),
        "frequency_method": freq_info["method"],
    }, diagnostics


def estimate_settling_time(t, values, target, tolerance_percent=2.0):
    finite = np.isfinite(t) & np.isfinite(values)
    t = t[finite]
    y = values[finite]
    if len(t) < 4 or not np.isfinite(target) or target == 0:
        return math.nan
    band = abs(target) * tolerance_percent / 100.0
    outside = np.where(np.abs(y - target) > band)[0]
    if len(outside) == 0:
        return 0.0
    last = int(outside[-1])
    if last >= len(t) - 1:
        return math.nan
    return float(t[last + 1] - t[0])


def estimate_thd_percent(t, values):
    finite = np.isfinite(t) & np.isfinite(values)
    t = t[finite]
    y = values[finite]
    if len(t) < 16:
        return math.nan
    diffs = np.diff(t)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if len(diffs) == 0:
        return math.nan
    dt = float(np.median(diffs))
    freq = np.fft.rfftfreq(len(y), d=dt)
    window = np.hanning(len(y))
    spec = np.abs(np.fft.rfft((y - np.mean(y)) * window))
    if len(freq) <= 1:
        return math.nan
    fund_idx = int(np.argmax(spec[1:]) + 1)
    fund = spec[fund_idx]
    if fund <= 0:
        return math.nan
    total_sq = 0.0
    f1 = freq[fund_idx]
    for harmonic in range(2, 16):
        target = harmonic * f1
        if target > freq[-1]:
            break
        idx = int(np.argmin(np.abs(freq - target)))
        total_sq += spec[idx] ** 2
    return float(math.sqrt(total_sq) / fund * 100.0)


def estimate_power_factor(v, i):
    finite = np.isfinite(v) & np.isfinite(i)
    v = v[finite]
    i = i[finite]
    if len(v) < 2:
        return math.nan
    n = min(len(v), len(i))
    v = v[:n]
    i = i[:n]
    vrms = np.sqrt(np.mean(v ** 2))
    irms = np.sqrt(np.mean(i ** 2))
    apparent = vrms * irms
    if apparent <= 0:
        return math.nan
    pf = float(np.mean(v * i) / apparent)
    return max(-1.0, min(1.0, pf))


def detect_response_diagnostics(t, v, frequency, nominal_frequency, voltage_dip, overshoot, settling_time, thd):
    finite = np.isfinite(t) & np.isfinite(v)
    y = v[finite]
    if len(y) >= 8:
        centered = y - np.mean(y)
        sign_changes = int(np.sum(np.diff(np.signbit(centered)) != 0))
        oscillation_ratio = float(np.std(centered) / max(abs(np.mean(y)), 1.0))
    else:
        sign_changes = 0
        oscillation_ratio = 0.0

    freq_error = abs(frequency - nominal_frequency) if np.isfinite(frequency) and np.isfinite(nominal_frequency) else math.nan
    return {
        "governor_hunting": np.isfinite(freq_error) and freq_error > 0.5 and sign_changes > 6,
        "voltage_oscillation": oscillation_ratio > 0.03 and sign_changes > 8,
        "slow_recovery": np.isfinite(settling_time) and settling_time > 5.0,
        "overdamped_response": np.isfinite(settling_time) and settling_time > 3.0 and (not np.isfinite(overshoot) or overshoot < 1.0),
        "underdamped_response": np.isfinite(overshoot) and overshoot > 5.0,
        "frequency_sag": np.isfinite(freq_error) and freq_error > 0.5,
        "excess_thd": np.isfinite(thd) and thd > 8.0,
    }


def step_load_score(voltage_dip, freq_dip, recovery_time, overshoot, thd):
    score = 100.0
    penalties = [
        max(0.0, safe_float(voltage_dip, 0.0) - 15.0) * 1.5,
        max(0.0, safe_float(freq_dip, 0.0) - 1.0) * 8.0,
        max(0.0, safe_float(recovery_time, 0.0) - 5.0) * 4.0,
        max(0.0, safe_float(overshoot, 0.0) - 5.0) * 2.0,
        max(0.0, safe_float(thd, 0.0) - 8.0) * 2.0,
    ]
    return max(0.0, min(100.0, score - sum(penalties)))


def pass_fail(results, diagnostics, recovery_limit):
    recovery_limit = safe_float(recovery_limit, 5.0)
    return {
        "Frequency stable +/-0.5 Hz": safe_float(results.get("max_frequency_dip_hz")) <= 0.5,
        "Voltage stable +/-2%": safe_float(results.get("max_voltage_dip_percent")) <= 2.0,
        f"Recovery under {recovery_limit:g} sec": safe_float(results.get("recovery_time_sec")) <= recovery_limit,
        "No sustained hunting": not diagnostics.get("governor_hunting", False),
        "No unstable AVR oscillation": not diagnostics.get("voltage_oscillation", False),
    }


def recommendations(results, diagnostics):
    recs = []
    if diagnostics.get("frequency_sag") or diagnostics.get("slow_recovery"):
        recs.append("Increase GOV gain in small increments and verify recovery without hunting.")
    if diagnostics.get("governor_hunting"):
        recs.append("Reduce GOV gain or adjust GOV integral to remove sustained speed hunting.")
    if diagnostics.get("voltage_oscillation") or diagnostics.get("underdamped_response"):
        recs.append("Reduce REG gain or adjust REG integral to reduce AVR oscillation and overshoot.")
    if diagnostics.get("overdamped_response"):
        recs.append("Increase response cautiously or adjust integral to improve slow recovery.")
    if safe_float(results.get("max_voltage_dip_percent")) > 10.0:
        recs.append("Tune V/Hz knee and verify load-step acceptance against alternator capability.")
    if diagnostics.get("excess_thd"):
        recs.append("Investigate nonlinear load content and verify THD under load after tuning.")
    if not recs:
        recs.append("Settings appear stable based on captured waveform evidence. Retest at required load steps.")
    return recs


def write_settings_summary(report_dir, site, factory, adjusted, manual_results, analyzed_results, diagnostics, status):
    path = report_dir / "settings_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["section", "field", "value"])
        for key in SITE_FIELDS:
            writer.writerow(["site_information", key, site.get(key, "")])
        for key in SETTING_FIELDS:
            writer.writerow(["factory_defaults", key, factory.get(key, "")])
        for key in SETTING_FIELDS:
            writer.writerow(["adjusted_final_values", key, adjusted.get(key, "")])
        for key in RESULT_FIELDS:
            writer.writerow(["waveform_results_manual", key, manual_results.get(key, "")])
        for key, value in analyzed_results.items():
            writer.writerow(["waveform_results_analyzed", key, value])
        for key, value in diagnostics.items():
            writer.writerow(["automatic_detection", key, value])
        for key, value in status.items():
            writer.writerow(["pass_fail", key, "PASS" if value else "FAIL"])
    return path


def plot_generator_trends(report_dir, rows, sunlight=False):
    plt = try_import_plot()
    if plt is None or not rows:
        return []
    plots_dir = report_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    t = np.asarray([r["time_s"] for r in rows], dtype=float)
    v = np.asarray([r["voltage_v"] for r in rows], dtype=float)
    i = np.asarray([r["current_a"] for r in rows], dtype=float)
    line_width = 2.4 if sunlight else 1.6
    grid_color = "#222222" if sunlight else "#cccccc"
    paths = []

    def save_plot(name, title, y, ylabel, color):
        path = plots_dir / name
        fig, ax = plt.subplots(figsize=(12, 5), dpi=160)
        ax.plot(t, y, color=color, linewidth=line_width)
        ax.set_title(title)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel(ylabel)
        ax.grid(True, color=grid_color, alpha=0.55)
        fig.tight_layout()
        fig.savefig(path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        paths.append(path)

    if np.any(np.isfinite(v)):
        save_plot("voltage_load_step.png", "Voltage During Load Step", v, "Voltage (V)", "#0047ab")
        save_plot("recovery_curve.png", "Recovery Curve", v, "Voltage (V)", "#0b7a3b")
    if np.any(np.isfinite(i)):
        save_plot("current_inrush.png", "Current Inrush", i, "Current (A)", "#a33f00")
    if np.any(np.isfinite(v)):
        freq_path = plots_dir / "frequency_load_step.png"
        freq_info = select_power_frequency(t, v)
        fig, ax = plt.subplots(figsize=(12, 4), dpi=160)
        ax.axhline(freq_info["final_hz"], color="#7b1fa2", linewidth=line_width)
        ax.set_title("Frequency During Load Step")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Frequency (Hz)")
        ax.grid(True, color=grid_color, alpha=0.55)
        fig.tight_layout()
        fig.savefig(freq_path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        paths.append(freq_path)
    return paths


def render_table(rows):
    body = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in rows
    )
    return f"<table>{body}</table>"


def write_html(report_dir, site, factory, adjusted, results, diagnostics, status, recs, plots):
    path = report_dir / "GENERATOR_COMMISSIONING_REPORT.html"
    title = "Generator AVR / Governor Commissioning Report"
    site_rows = [(label.replace("_", " ").title(), site.get(label, "")) for label in SITE_FIELDS]
    factory_rows = [(label.replace("_", " ").upper(), factory.get(label, "")) for label in SETTING_FIELDS]
    adjusted_rows = [(label.replace("_", " ").upper(), adjusted.get(label, "")) for label in SETTING_FIELDS]
    result_rows = [
        ("Steady state voltage", fmt(results.get("steady_state_voltage"), 2, " V")),
        ("Steady state frequency", fmt(results.get("steady_state_frequency"), 2, " Hz")),
        ("Max voltage dip", fmt(results.get("max_voltage_dip_percent"), 2, " %")),
        ("Max frequency dip", fmt(results.get("max_frequency_dip_hz"), 2, " Hz")),
        ("Max overshoot", fmt(results.get("max_overshoot_percent"), 2, " %")),
        ("Recovery time", fmt(results.get("recovery_time_sec"), 2, " sec")),
        ("Settling time", fmt(results.get("settling_time_sec"), 2, " sec")),
        ("THD under load", fmt(results.get("thd_under_load_percent"), 2, " %")),
        ("PF under load", fmt(results.get("pf_under_load"), 3, "")),
        ("Step-load acceptance score", fmt(results.get("step_load_acceptance_score"), 1, " / 100")),
    ]
    status_rows = [(key, "PASS" if value else "FAIL") for key, value in status.items()]
    detection_rows = [(key.replace("_", " ").title(), "YES" if value else "NO") for key, value in diagnostics.items()]
    plot_html = "\n".join(
        f"<figure><img src='plots/{html.escape(path.name)}'><figcaption>{html.escape(path.name)}</figcaption></figure>"
        for path in plots
    )
    rec_html = "".join(f"<li>{html.escape(rec)}</li>" for rec in recs)
    generated = time.strftime("%Y-%m-%d %H:%M:%S")

    path.write_text(f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; color: #1b1b1b; }}
h1 {{ margin-bottom: 4px; }}
.warning {{ padding: 12px; background: #fff3cd; border: 1px solid #d39e00; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 14px 0 24px; }}
th, td {{ border: 1px solid #c8c8c8; padding: 8px; text-align: left; }}
th {{ width: 36%; background: #f1f4f8; }}
.pass {{ color: #0a7f2e; font-weight: bold; }}
.fail {{ color: #b00020; font-weight: bold; }}
img {{ max-width: 100%; border: 1px solid #ddd; }}
figure {{ margin: 18px 0; }}
.signature {{ margin-top: 56px; display: grid; grid-template-columns: 1fr 1fr; gap: 40px; }}
.line {{ border-top: 1px solid #333; padding-top: 8px; }}
</style>
</head>
<body>
<h1>Fluke ScopeSuite Pro V3</h1>
<h2>{html.escape(title)}</h2>
<p>Generated: {html.escape(generated)}</p>
<p class="warning">{html.escape(WARNING_TEXT)}</p>
<h2>A. Site Information</h2>
{render_table(site_rows)}
<h2>B. Controller Settings Table</h2>
<h3>Factory Defaults</h3>
{render_table(factory_rows)}
<h3>Adjusted Final Values</h3>
{render_table(adjusted_rows)}
<h2>C. Waveform Results</h2>
{render_table(result_rows)}
<h3>Pass / Fail</h3>
{render_table(status_rows)}
<h3>Automatic Detection</h3>
{render_table(detection_rows)}
<h2>D. Recommendations</h2>
<ul>{rec_html}</ul>
<h2>E. Final Approved Settings</h2>
{render_table(adjusted_rows)}
<h2>Trend Plots</h2>
{plot_html if plot_html else "<p>No waveform trend plots were available.</p>"}
<h2>F. Technician Signature</h2>
<div class="signature">
<div class="line">Technician Signature</div>
<div class="line">Customer / Authorized Representative</div>
</div>
</body>
</html>
""", encoding="utf-8")
    return path


def write_text_summary(report_dir, site, results, diagnostics, status, recs):
    path = report_dir / "GENERATOR_COMMISSIONING_REPORT.txt"
    lines = [
        "GENERATOR AVR / GOVERNOR COMMISSIONING REPORT",
        "==============================================",
        "",
        WARNING_TEXT,
        "",
        f"Customer: {site.get('customer', '')}",
        f"Site: {site.get('site_name', '')}",
        f"Generator ID: {site.get('generator_id', '')}",
        f"Technician: {site.get('technician', '')}",
        f"Date: {site.get('date', '')}",
        "",
        "Waveform Results",
        "----------------",
        f"Steady state voltage: {fmt(results.get('steady_state_voltage'), 2, ' V')}",
        f"Steady state frequency: {fmt(results.get('steady_state_frequency'), 2, ' Hz')}",
        f"Voltage dip: {fmt(results.get('max_voltage_dip_percent'), 2, ' %')}",
        f"Frequency dip: {fmt(results.get('max_frequency_dip_hz'), 2, ' Hz')}",
        f"Recovery time: {fmt(results.get('recovery_time_sec'), 2, ' sec')}",
        f"Overshoot: {fmt(results.get('max_overshoot_percent'), 2, ' %')}",
        f"THD under load: {fmt(results.get('thd_under_load_percent'), 2, ' %')}",
        f"PF under load: {fmt(results.get('pf_under_load'), 3, '')}",
        f"Step-load acceptance score: {fmt(results.get('step_load_acceptance_score'), 1, ' / 100')}",
        "",
        "Pass / Fail",
        "-----------",
    ]
    lines.extend(f"{key}: {'PASS' if value else 'FAIL'}" for key, value in status.items())
    lines += ["", "Automatic Detection", "-------------------"]
    lines.extend(f"{key.replace('_', ' ').title()}: {'YES' if value else 'NO'}" for key, value in diagnostics.items())
    lines += ["", "Recommendations", "---------------"]
    lines.extend(f"- {rec}" for rec in recs)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_pdf(report_dir, site, results, status):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except Exception:
        return None

    path = report_dir / "GENERATOR_COMMISSIONING_REPORT.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    y = height - 0.75 * inch
    c.setFont("Helvetica-Bold", 15)
    c.drawString(0.75 * inch, y, "Generator AVR / Governor Commissioning Report")
    y -= 0.35 * inch
    c.setFont("Helvetica", 9)
    for line in [
        WARNING_TEXT,
        f"Customer: {site.get('customer', '')}",
        f"Site: {site.get('site_name', '')}",
        f"Generator ID: {site.get('generator_id', '')}",
        f"Technician: {site.get('technician', '')}",
        f"Voltage dip: {fmt(results.get('max_voltage_dip_percent'), 2, ' %')}",
        f"Frequency dip: {fmt(results.get('max_frequency_dip_hz'), 2, ' Hz')}",
        f"Recovery time: {fmt(results.get('recovery_time_sec'), 2, ' sec')}",
        f"THD under load: {fmt(results.get('thd_under_load_percent'), 2, ' %')}",
        f"Step-load acceptance score: {fmt(results.get('step_load_acceptance_score'), 1, ' / 100')}",
    ]:
        c.drawString(0.75 * inch, y, line[:110])
        y -= 0.22 * inch
    y -= 0.1 * inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(0.75 * inch, y, "Pass / Fail")
    y -= 0.24 * inch
    c.setFont("Helvetica", 9)
    for key, value in status.items():
        c.drawString(0.9 * inch, y, f"{key}: {'PASS' if value else 'FAIL'}")
        y -= 0.2 * inch
    c.line(0.75 * inch, 1.35 * inch, 3.6 * inch, 1.35 * inch)
    c.line(4.0 * inch, 1.35 * inch, 7.0 * inch, 1.35 * inch)
    c.drawString(0.75 * inch, 1.1 * inch, "Technician Signature")
    c.drawString(4.0 * inch, 1.1 * inch, "Customer / Authorized Representative")
    c.save()
    return path


def build_generator_commissioning_report(report_dir, site, factory, adjusted, manual_results, evidence_dir=None, options=None, log=None):
    log = log or (lambda _msg: None)
    options = options or {}
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "waveform_evidence").mkdir(exist_ok=True)
    (report_dir / "screenshots").mkdir(exist_ok=True)
    (report_dir / "plots").mkdir(exist_ok=True)

    copied = copy_evidence(evidence_dir, report_dir, log=log)
    rows, source = collect_waveform_rows(report_dir)
    if source:
        log(f"Generator report waveform evidence source: {source}")
    else:
        log("Generator report: no numeric waveform evidence found; using manual result fields.")

    analyzed, diagnostics = analyze_waveform_results(
        rows,
        manual_results.get("steady_state_voltage") or site.get("voltage"),
        site.get("frequency") or manual_results.get("steady_state_frequency") or "60",
    )
    results = dict(analyzed)
    for key in RESULT_FIELDS:
        manual = manual_results.get(key, "")
        if str(manual).strip():
            results[key] = safe_float(manual)

    status = pass_fail(results, diagnostics, options.get("recovery_limit_sec", "5"))
    recs = recommendations(results, diagnostics)
    settings_csv = write_settings_summary(report_dir, site, factory, adjusted, manual_results, results, diagnostics, status)
    plots = plot_generator_trends(report_dir, rows, sunlight=options.get("sunlight_mode", False))
    html_path = write_html(report_dir, site, factory, adjusted, results, diagnostics, status, recs, plots)
    text_path = write_text_summary(report_dir, site, results, diagnostics, status, recs)
    pdf_path = write_pdf(report_dir, site, results, status)

    log(f"Generator report generated: {html_path}")
    if pdf_path:
        log(f"Generator PDF generated: {pdf_path}")
    log(f"Generator settings summary generated: {settings_csv}")
    log(
        "Generator evidence copied: "
        f"{len(copied['waveforms'])} waveform files, {len(copied['screenshots'])} screenshots, {len(copied['plots'])} plots"
    )
    return {
        "html": html_path,
        "pdf": pdf_path,
        "text": text_path,
        "settings_csv": settings_csv,
        "plots": plots,
        "diagnostics": diagnostics,
        "status": status,
        "results": results,
    }
