"""CLI: `python -m opengrid.lifecycle run|status|restore|export-billing` (config from OG_CONFIG).

run [--cycle fast|hourly|auto]     one cycle (default auto: hourly when due); exit 1 when a step failed
status [--json]                    sizes, partitions, oldest rows, next drops, disk headroom, last runs;
                                   exit 2 on CRIT, 1 on WARN
restore --archive NAME --day D     verify + load one archived day into og_restore.<name>_<yyyymmdd>
export-billing --month YYYY-MM     the write-once monthly invoice_line + pnl export (normally hourly)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from typing import Any

from opengrid.lifecycle.archive import export_billing_month, restore_day
from opengrid.lifecycle.policy import LifecycleConfig
from opengrid.lifecycle.runner import run_lifecycle
from opengrid.lifecycle.status import collect_status, render_status
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.log import configure_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m opengrid.lifecycle")
    parser.add_argument("--config", default=os.environ.get("OG_CONFIG"))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--cycle", choices=["fast", "hourly", "auto"], default="auto")
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument(
        "--archive", required=True, help="archive name, e.g. telemetry, verdict, billing_pnl"
    )
    restore.add_argument("--day", required=True, type=date.fromisoformat)
    restore.add_argument("--schema", default="og_restore")
    billing = sub.add_parser("export-billing")
    billing.add_argument("--month", required=True, help="YYYY-MM")
    return parser


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    lcfg = LifecycleConfig.from_config(cfg)
    pool = await make_pool(cfg)
    try:
        result: Any
        if args.command == "run":
            result = await run_lifecycle(pool, cfg, cycle=args.cycle)
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("ok", True) else 1
        if args.command == "status":
            result = await collect_status(pool, lcfg)
            print(json.dumps(result, indent=2, default=str) if args.json else render_status(result))
            return {"OK": 0, "WARN": 1}.get(result["level"], 2)
        if args.command == "restore":
            result = await restore_day(pool, args.archive, args.day, schema=args.schema)
        else:
            year, month = (int(p) for p in args.month.split("-"))
            result = await export_billing_month(pool, lcfg, date(year, month, 1))
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("lifecycle")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
