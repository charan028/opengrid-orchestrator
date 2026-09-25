"""Entry point: python -m ogsim.control <command>. Defaults to `serve` when
no subcommand is given, so `python -m ogsim.control` starts the control
plane on port 8091 per BUILD.md §4."""

from __future__ import annotations

import sys

from ogsim.control.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["serve"]
    raise SystemExit(main(argv))
