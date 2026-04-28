DEFAULT_TIMEBASE_CORRECTION = 1.0
FLUKE_REPLAY_TIMEBASE_PRESET = 1.111111111

_timebase_correction = DEFAULT_TIMEBASE_CORRECTION
_expected_line_frequency_hz = 60.0


def set_timebase_correction(value):
    global _timebase_correction
    try:
        parsed = float(value)
    except Exception:
        parsed = DEFAULT_TIMEBASE_CORRECTION
    if not (0.1 <= parsed <= 10.0):
        parsed = DEFAULT_TIMEBASE_CORRECTION
    _timebase_correction = parsed
    return _timebase_correction


def get_timebase_correction():
    return _timebase_correction


def set_expected_line_frequency(value):
    global _expected_line_frequency_hz
    try:
        parsed = float(value)
    except Exception:
        parsed = 60.0
    if parsed not in (0.0, 50.0, 60.0):
        parsed = 60.0
    _expected_line_frequency_hz = parsed
    return _expected_line_frequency_hz


def get_expected_line_frequency():
    return _expected_line_frequency_hz
