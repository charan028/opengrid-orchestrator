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
from opengrid.platform.config import Config, load_config
from opengrid.platform.net import is_loopback_host


def resolve_bind_host(cfg: Config) -> str:
    """og-api trusts `X-Remote-User` because only Apache on this host can reach it: a non-loopback
    `[api].bind_host` would let anyone on the network assert any identity. Refuses to start on one unless
    `[api].allow_non_loopback_bind = true` is set explicitly (review #16)."""
    host = str(cfg.get("api.bind_host", "127.0.0.1"))
    if not is_loopback_host(host) and not bool(cfg.get("api.allow_non_loopback_bind", False)):
        raise SystemExit(
            f"refusing to bind og-api to non-loopback {host!r}: identity headers are trusted only from "
            "the local Apache proxy (set [api].allow_non_loopback_bind = true to override)"
        )
    return host


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m opengrid.api")
    parser.add_argument("--config", default=os.environ.get("OG_CONFIG"))
    args = parser.parse_args(argv)

    if args.config:
        os.environ["OG_CONFIG"] = args.config
    cfg = load_config(args.config)

    uvicorn.run(
        create_app(),
        host=resolve_bind_host(cfg),
        port=cfg.get("api.bind_port", 8080),
        log_config=None,  # opengrid.platform.log's JSON formatter is installed in create_app's lifespan
    )


if __name__ == "__main__":
    main()
