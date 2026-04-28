#!/usr/bin/env python3
"""Launcher for Fluke ScopeSuite Pro V3."""

import sys

sys.dont_write_bytecode = True

try:
    from scopesuite_v3.app import main
    from scopesuite_v3.self_tests import run_field_abuse_selftest, run_live_single_report_smoke
except ModuleNotFoundError as exc:
    if exc.name != "scopesuite_v3":
        raise
    from fluke.scopesuite_v3.app import main
    from fluke.scopesuite_v3.self_tests import run_field_abuse_selftest, run_live_single_report_smoke


if __name__ == "__main__":
    if "--field-abuse-self-test" in sys.argv:
        sys.exit(run_field_abuse_selftest(sys.argv))
    if "--live-single-report-smoke" in sys.argv:
        sys.exit(run_live_single_report_smoke(sys.argv))
    main()
