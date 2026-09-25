"""Process entry point `python -m opengrid.api` / `opengrid.api.main` -> systemd unit `og-api`
(BUILD.md S4, `orchestrator/INTERFACES.md`).

Unlike the other six processes, `api` does not use `opengrid.platform.process.run_forever` (there is
no periodic tick to drive -- `uvicorn` owns the event loop and request lifecycle; `create_app`'s
lifespan handles startup/shutdown, 02b S7).
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from opengrid.api.app import create_app
from opengrid.platform.config import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m opengrid.api")
    parser.add_argument("--config", default=os.environ.get("OG_CONFIG"))
    args = parser.parse_args(argv)

    if args.config:
        os.environ["OG_CONFIG"] = args.config
    cfg = load_config(args.config)

    uvicorn.run(
        create_app(),
        host=cfg.get("api.bind_host", "127.0.0.1"),
        port=cfg.get("api.bind_port", 8080),
        log_config=None,  # opengrid.platform.log's JSON formatter is installed in create_app's lifespan
    )


if __name__ == "__main__":
    main()
