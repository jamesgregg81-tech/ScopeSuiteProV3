import math

import numpy as np

from .calibration import get_expected_line_frequency, get_timebase_correction


def finite_xy(time_values, values):
    t = np.asarray(time_values, dtype=float)
    y = np.asarray(values, dtype=float)
    finite = np.isfinite(t) & np.isfinite(y)
    return t[finite], y[finite]


def fft_frequency_info(time_values, values):
    t, y = finite_xy(time_values, values)
    if len(t) < 8:
        return {
            "fft_dominant_hz": math.nan,
            "fft_bin_spacing_hz": math.nan,
            "sample_rate_hz": math.nan,
            "sample_count": len(t),
            "duration_s": math.nan,
        }

    diffs = np.diff(t)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if len(diffs) == 0:
        return {
            "fft_dominant_hz": math.nan,
            "fft_bin_spacing_hz": math.nan,
            "sample_rate_hz": math.nan,
            "sample_count": len(t),
            "duration_s": math.nan,
        }

    dt = float(np.median(diffs))
    sample_span = float(t[-1] - t[0])
    sample_rate = 1.0 / dt if dt > 0 else math.nan
    duration = float(sample_span + dt) if sample_span >= 0 else math.nan
    bin_spacing = sample_rate / len(t) if np.isfinite(sample_rate) else math.nan

    y = y - np.mean(y)
    window = np.hanning(len(y))
    spec = np.fft.rfft(y * window)
    freq = np.fft.rfftfreq(len(y), d=dt)
    amp = np.abs(spec)
    if len(freq) <= 1:
        dominant = math.nan
    else:
        idx = int(np.argmax(amp[1:]) + 1)
        dominant = float(freq[idx])

    return {
        "fft_dominant_hz": dominant,
        "fft_bin_spacing_hz": bin_spacing,
        "sample_rate_hz": sample_rate,
        "sample_count": len(t),
        "duration_s": duration,
    }


def zero_crossing_frequency(time_values, values):
    t, y = finite_xy(time_values, values)
    if len(t) < 4:
        return math.nan, 0

    y = y - np.mean(y)
    crossings = []
    for idx in range(1, len(y)):
        y0 = y[idx - 1]
        y1 = y[idx]
        if y0 < 0 <= y1 and y1 != y0:
            t0 = t[idx - 1]
            t1 = t[idx]
            frac = -y0 / (y1 - y0)
            crossings.append(float(t0 + frac * (t1 - t0)))

    if len(crossings) < 2:
        return math.nan, len(crossings)

    periods = np.diff(np.asarray(crossings, dtype=float))
    periods = periods[np.isfinite(periods) & (periods > 0)]
    if len(periods) == 0:
        return math.nan, len(crossings)

    median_period = float(np.median(periods))
    if median_period <= 0:
        return math.nan, len(crossings)
    return 1.0 / median_period, len(crossings)


def select_power_frequency(time_values, voltage_values, fallback_values=None):
    timebase_correction = get_timebase_correction()
    expected_line_hz = get_expected_line_frequency()
    fft_v = fft_frequency_info(time_values, voltage_values)
    zc_hz, crossing_count = zero_crossing_frequency(time_values, voltage_values)

    final_hz = math.nan
    method = "unavailable"
    nominal_snap_hz = math.nan
    mismatch = False
    source = "Channel A voltage rising zero crossings"

    if np.isfinite(zc_hz):
        final_hz = zc_hz
        method = "zero_crossing"
        if 55.0 <= zc_hz <= 65.0:
            final_hz = 60.0
            nominal_snap_hz = 60.0
            method = "zero_crossing_near_60"
        elif 45.0 <= zc_hz <= 55.0:
            final_hz = 50.0
            nominal_snap_hz = 50.0
            method = "zero_crossing_near_50"
    elif fallback_values is not None:
        zc_hz, crossing_count = zero_crossing_frequency(time_values, fallback_values)
        source = "fallback waveform rising zero crossings"
        if np.isfinite(zc_hz):
            final_hz = zc_hz
            method = "fallback_zero_crossing"
            if 55.0 <= zc_hz <= 65.0:
                final_hz = 60.0
                nominal_snap_hz = 60.0
                method = "fallback_zero_crossing_near_60"
            elif 45.0 <= zc_hz <= 55.0:
                final_hz = 50.0
                nominal_snap_hz = 50.0
                method = "fallback_zero_crossing_near_50"

    if not np.isfinite(final_hz):
        final_hz = fft_v["fft_dominant_hz"]
        method = "fft_dominant"
        source = "FFT dominant bin"

    fft_hz = fft_v["fft_dominant_hz"]
    if np.isfinite(fft_hz) and np.isfinite(zc_hz):
        mismatch = abs(fft_hz - zc_hz) > 2.0

    calibration_suggestion = ""
    measured_candidates = [v for v in (zc_hz, fft_hz, final_hz) if np.isfinite(v)]
    if (
        expected_line_hz == 60.0
        and abs(timebase_correction - 1.0) < 1e-9
        and any(65.0 <= value <= 68.0 for value in measured_candidates)
    ):
        calibration_suggestion = (
            "Expected 60 Hz but measured 65-68 Hz. "
            "Apply Fluke replay timebase correction 1.111111111."
        )

    return {
        "final_hz": final_hz,
        "method": method,
        "source": source,
        "zero_crossing_hz": zc_hz,
        "zero_crossing_count": crossing_count,
        "fft_dominant_hz": fft_hz,
        "fft_bin_spacing_hz": fft_v["fft_bin_spacing_hz"],
        "sample_rate_hz": fft_v["sample_rate_hz"],
        "sample_count": fft_v["sample_count"],
        "duration_s": fft_v["duration_s"],
        "nominal_snap_hz": nominal_snap_hz,
        "fft_timebase_mismatch": mismatch,
        "timebase_correction": timebase_correction,
        "expected_line_frequency_hz": expected_line_hz,
        "calibration_suggestion": calibration_suggestion,
    }


def log_frequency_debug(log, label, info, delta_x=None):
    sample_rate = 1.0 / delta_x if delta_x and delta_x > 0 else info["sample_rate_hz"]
    log(f"{label}: delta_x from QW/admin or sample spacing={delta_x if delta_x else 'estimated from samples'}")
    log(f"{label}: sample count={info['sample_count']}")
    log(f"{label}: calculated sample rate={sample_rate:.6g} Hz")
    log(f"{label}: waveform duration={info['duration_s']:.6g} s")
    log(f"{label}: FFT bin spacing={info['fft_bin_spacing_hz']:.6g} Hz")
    log(f"{label}: FFT dominant frequency={info['fft_dominant_hz']:.6g} Hz")
    log(f"{label}: zero-crossing frequency={info['zero_crossing_hz']:.6g} Hz")
    log(f"{label}: final selected report frequency={info['final_hz']:.6g} Hz ({info['method']})")
    log(f"{label}: cycles detected={max(0, info['zero_crossing_count'] - 1)}")
    if info["timebase_correction"] == 1.0:
        log(f"{label}: TIMEBASE_CORRECTION=1.0 (no blind correction applied)")
    else:
        log(
            f"{label}: TIMEBASE_CORRECTION={info['timebase_correction']} "
            "(configured correction is applied to QW delta_x; verify against scope time/div)"
        )
    if info["fft_timebase_mismatch"]:
        log(
            f"{label}: FFT/timebase mismatch flagged; FFT={info['fft_dominant_hz']:.6g} Hz, "
            f"zero-crossing={info['zero_crossing_hz']:.6g} Hz"
        )
    if info.get("calibration_suggestion"):
        log(f"{label}: CALIBRATION CHECK: {info['calibration_suggestion']}")
