"""`python -m opengrid.engine.main` -- systemd unit `og-engine` (02b S1.2, INTERFACES.md)."""

from __future__ import annotations

import asyncio
import os

from opengrid.engine import main as engine_main
from opengrid.platform.config import load_config


def run() -> None:
    cfg = load_config(os.environ.get("OG_CONFIG"))
    asyncio.run(engine_main(cfg))


if __name__ == "__main__":
    run()
