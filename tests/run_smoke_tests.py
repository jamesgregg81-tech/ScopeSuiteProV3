import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scopesuite_v3.self_tests import run_field_abuse_selftest


if __name__ == "__main__":
    raise SystemExit(run_field_abuse_selftest(sys.argv[1:]))
