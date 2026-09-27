#!/usr/bin/env python3
"""deploy/scripts/r343_settle_correct_as.py -- r3.4.3 append-only correction of ERCOT_AS P&L (release manager
runs it; lead + owner approved 2026-09-27).

Before fix-settle-spp (c641a0f, live from 02:21 CT), ERCOT_AS capacity revenue was priced at the
opportunity's stored value_per_mwh instead of the cleared DAM MCPC (e.g. 0.5 MW ECRS settled 0.125 per
15-min interval instead of 0.035). This script re-settles the affected intervals through the normal
`opengrid.settle.settle()` path -- which now prices at the cleared MCPC -- so every change is an
insert-only, versioned correction: a NEW og.pnl row (and, where the amount changed, new invoice_line
versions) that `supersedes` the old one. Existing rows are never edited; the only UPDATE is settle's own
standard `superseded_by` pointer on the replaced row (the same versioning every correction uses).

Default window: 2026-09-26 22:00 through 2026-09-27 02:21 America/Chicago (intervals STARTING in it).

Dry run by DEFAULT (reads only; prints each interval: old revenue, new revenue, price_flag, and totals):

    OG_CONFIG=/opt/opengrid/current/orchestrator/config/orchestrator.toml \\
      /opt/opengrid/venv/bin/python /opt/opengrid/current/deploy/scripts/r343_settle_correct_as.py

Apply (inserts the superseding rows, then prints the after-totals):

    ... r343_settle_correct_as.py --apply

Options: --start/--end (ISO-8601 with offset) to change the window.
Idempotent: a second --apply finds nothing left to change (settle short-circuits unchanged P&L).

Totals SQL (the script prints both; run by hand to cross-check):

    -- live (current) ERCOT_AS revenue in the window
    SELECT count(*), sum(p.revenue) FROM og.pnl p JOIN og.obligation o USING (obligation_id)
     WHERE o.service_type = 'ERCOT_AS' AND p.superseded_by IS NULL
       AND p.interval_start >= '2026-09-26 22:00-05' AND p.interval_start < '2026-09-27 02:21-05';
    -- the superseded (pre-correction) versions
    ... same with p.superseded_by IS NOT NULL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "orchestrator" / "src"))

from opengrid.platform.config import load_config  # noqa: E402
from opengrid.platform.db import make_pool  # noqa: E402
from opengrid.settle import configure, settle  # noqa: E402
from opengrid.settle.pg_backend import PgSettleBackend  # noqa: E402
from opengrid.settle.profitability import compute_revenue  # noqa: E402
from opengrid.trace.pg_backend import PgTraceBackend, journal_path_from_config  # noqa: E402
from opengrid.trace.store import TraceStore  # noqa: E402

DEFAULT_START = "2026-09-26T22:00:00-05:00"
DEFAULT_END = "2026-09-27T02:21:00-05:00"
INTERVAL = timedelta(minutes=15)

INTERVALS_SQL = """
SELECT p.pnl_id, p.obligation_id, p.interval_start, p.revenue,
       (SELECT mi.delivered_kwh FROM og.meter_interval mi
         WHERE mi.obligation_id = p.obligation_id AND mi.interval_start = p.interval_start
           AND mi.superseded_by IS NULL
         ORDER BY mi.version DESC LIMIT 1) AS delivered_kwh
FROM og.pnl p JOIN og.obligation o USING (obligation_id)
WHERE o.service_type = 'ERCOT_AS' AND p.superseded_by IS NULL
  AND p.interval_start >= %(start)s AND p.interval_start < %(end)s
ORDER BY p.interval_start, p.obligation_id
"""

TOTALS_SQL = """
SELECT count(*) FILTER (WHERE p.superseded_by IS NULL)             AS live_rows,
       coalesce(sum(p.revenue) FILTER (WHERE p.superseded_by IS NULL), 0)     AS live_revenue,
       count(*) FILTER (WHERE p.superseded_by IS NOT NULL)         AS superseded_rows,
       coalesce(sum(p.revenue) FILTER (WHERE p.superseded_by IS NOT NULL), 0) AS superseded_revenue
FROM og.pnl p JOIN og.obligation o USING (obligation_id)
WHERE o.service_type = 'ERCOT_AS'
  AND p.interval_start >= %(start)s AND p.interval_start < %(end)s
"""


async def _fetch(pool: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def _totals(pool: Any, window: dict[str, Any], label: str) -> tuple[Any, ...]:
    (row,) = await _fetch(pool, TOTALS_SQL, window)
    print(f"{label}: live rows={row[0]} live revenue={row[1]} | superseded rows={row[2]} revenue={row[3]}")
    return row


async def correct(
    pool: Any, backend: PgSettleBackend, *, start: datetime, end: datetime, apply: bool
) -> dict[str, Any]:
    """Plan (and with `apply`, perform) the correction. Returns a summary for callers/tests."""
    window = {"start": start, "end": end}
    await _totals(pool, window, "BEFORE")
    rows = await _fetch(pool, INTERVALS_SQL, window)
    planned: list[dict[str, Any]] = []
    for _pnl_id, obligation_id, interval_start, old_revenue, delivered_kwh in rows:
        ctx = await backend.fetch_context(obligation_id, interval_start)
        hours = Decimal(INTERVAL.total_seconds()) / Decimal(3600)
        new_revenue = compute_revenue(
            service_type=ctx.service_type,
            delivered_kwh=Decimal(str(delivered_kwh or 0)),
            committed_kwh=ctx.committed_kw * hours,
            price_per_kwh=ctx.price_per_kwh,
            discharge_spp_per_kwh=ctx.wholesale_price_per_kwh,
        ).quantize(Decimal("0.000001"))
        planned.append(
            {
                "obligation_id": obligation_id,
                "interval_start": interval_start,
                "old_revenue": Decimal(str(old_revenue)),
                "new_revenue": new_revenue,
                "price_flag": ctx.price_flag,
            }
        )
    changed = [p for p in planned if p["new_revenue"] != p["old_revenue"]]
    print(f"{'interval_start':32} {'obligation_id':38} {'old':>12} {'new':>12}  price_flag")
    for p in planned:
        mark = "*" if p in changed else " "
        print(
            f"{mark}{p['interval_start'].isoformat():31} {p['obligation_id']!s:38} "
            f"{p['old_revenue']:>12} {p['new_revenue']:>12}  {p['price_flag']}"
        )
    print(
        f"{len(planned)} ERCOT_AS interval(s) in window, {len(changed)} to correct; "
        f"revenue {sum((p['old_revenue'] for p in changed), Decimal(0))} -> "
        f"{sum((p['new_revenue'] for p in changed), Decimal(0))}"
    )
    no_mcpc = [p for p in changed if p["price_flag"] != "MCPC"]
    if no_mcpc:
        print(f"WARNING: {len(no_mcpc)} interval(s) have no cleared MCPC for their hour (price_flag != MCPC)")
    if not apply:
        print("DRY RUN: nothing written. Re-run with --apply to insert the superseding rows.")
        return {"planned": planned, "changed": changed, "applied": 0}
    applied = 0
    for p in changed:
        await settle(p["obligation_id"], p["interval_start"], p["interval_start"] + INTERVAL)
        applied += 1
    print(f"APPLIED: re-settled {applied} interval(s) (insert-only superseding versions).")
    await _totals(pool, window, "AFTER")
    return {"planned": planned, "changed": changed, "applied": applied}


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(os.environ.get("OG_CONFIG"))
    pool = await make_pool(cfg)
    try:
        backend = PgSettleBackend(pool)
        trace_store = TraceStore(PgTraceBackend(pool, journal_path=journal_path_from_config(cfg)))
        configure(backend, trace_store, trace_pool=pool)
        await correct(
            pool,
            backend,
            start=datetime.fromisoformat(args.start),
            end=datetime.fromisoformat(args.end),
            apply=args.apply,
        )
    finally:
        await pool.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", default=DEFAULT_START, help="window start, ISO-8601 with offset")
    parser.add_argument("--end", default=DEFAULT_END, help="window end (exclusive), ISO-8601 with offset")
    parser.add_argument("--apply", action="store_true", help="insert the superseding rows (default: dry run)")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
