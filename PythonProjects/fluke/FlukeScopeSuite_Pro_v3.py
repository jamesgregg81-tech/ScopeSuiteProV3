#!/usr/bin/env python3
"""Launcher for Fluke ScopeSuite Pro V3."""

import sys

sys.dont_write_bytecode = True

try:
    from scopesuite_v3.app import main
except ModuleNotFoundError as exc:
    if exc.name != "scopesuite_v3":
        raise
    from fluke.scopesuite_v3.app import main


if __name__ == "__main__":
    main()
