"""Entry point: python -m ogsim.market"""

from __future__ import annotations

import uvicorn

from ogsim.market.config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run("ogsim.market.app:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
