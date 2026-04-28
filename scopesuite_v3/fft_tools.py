import math


def bytes_to_samples(raw_bytes, limit=4096):
    payload = bytes(b for b in raw_bytes if b not in (10, 13))
    if len(payload) < 8:
        raise RuntimeError("Not enough waveform bytes for FFT.")

    if len(payload) > limit:
        step = max(1, len(payload) // limit)
        payload = payload[::step][:limit]

    mean = sum(payload) / len(payload)
    return [b - mean for b in payload]


def compute_fft_from_bytes(raw_bytes, sample_rate_hz=1.0, max_points=4096):
    samples = bytes_to_samples(raw_bytes, max_points)

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("FFT requires NumPy. Install it with: python -m pip install numpy") from exc

    y = np.asarray(samples, dtype=float)
    n = len(y)
    window = np.hanning(n)
    spectrum = np.fft.rfft(y * window)
    freq = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    amplitude = (2.0 / max(np.sum(window), 1.0)) * np.abs(spectrum)

    if len(freq) > 1:
        idx = int(np.argmax(amplitude[1:]) + 1)
        dominant = (float(freq[idx]), float(amplitude[idx]))
    else:
        dominant = (math.nan, math.nan)

    return {
        "samples": samples,
        "frequency": freq.tolist(),
        "amplitude": amplitude.tolist(),
        "dominant": dominant,
        "sample_rate_hz": sample_rate_hz,
    }
