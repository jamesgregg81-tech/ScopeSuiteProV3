from pathlib import Path


APP_NAME = "Fluke ScopeSuite Pro V3 Industrial Edition"
INITIAL_BAUD = 1200
WORK_BAUD = 9600
BAUD = INITIAL_BAUD
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "FlukeScopeSuite_Captures"
PORT_PREFIXES = ("/dev/cu.", "/dev/tty.", "COM")
SERIAL_TIMEOUT = 10.0
SERIAL_TOTAL_TIMEOUT = 30.0
CURRENT_SCALE_A_PER_V = 1.0
TIMEBASE_CORRECTION = 1.0
EXPECTED_SCREEN_SIZE = (320, 240)
