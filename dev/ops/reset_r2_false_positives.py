#!/usr/bin/env python3
"""One-off, audited R2 incident remediation (2026-09-26): resets hub-01996/01997/01998 from their
wrong `QUARANTINED` state back to `OK`, and cancels their 3 open maintenance work orders as a
FALSE_POSITIVE.

**Root cause (fixed in this release).** `opengrid.assets.repo.PgDriftObservationRepo.observation_
window` read `rows[-1]` for "the latest measured offset", but `pq_ingest.latest_summaries`/
`PgPqIngestBackend.latest_summaries` return rows NEWEST FIRST (`ORDER BY hub_id, ts DESC`) -- so
`rows[-1]` was actually the OLDEST row in the window, from before any real drift. hub-01996 measured
0.017 Hz pre-calibration offset against a real 0.2 Hz drift; the resulting wrong correction then
verified `WORSE_ROLLED_BACK`, quarantining a hub that was never actually drifting. The same ordering
bug produced 161 `NO_CHANGE` outcomes fleet-wide (false positives, not real hardware faults) before
the drift sweep was disabled (`opengrid.assets.runner.DRIFT_SWEEP_ENABLED_DEFAULT = False`). See
`orchestrator/src/opengrid/assets/repo.py`'s "LIVE BUG FIX" comment for the fix itself.

**For the live-path agent, after this release is deployed.** Not run by the build agent, and not
run against a test workspace -- this targets the PRODUCTION database, using the same config/env the
real og-settle/og-guardian processes use.

Every action goes through `opengrid.assets.service.AssetHealthService.record_false_positive_reset` --
the exact same code path this release ships, not a separate one-off SQL script -- so the audit trail
(K10/K11: `ASSET_STATE_TRANSITION` + `OPERATOR_ACTION`, both written to `og.trace`) matches what any
other false-positive reset will produce from here on. It refuses (raises, makes no changes) if a
listed hub is not currently `QUARANTINED` -- never resets a hub for an unrelated, possibly-legitimate
reason.

**Dry-run by default.** Running with no arguments only prints each hub's current state and open work
order id and makes no changes. Pass `--apply` to actually perform the reset.

Run on the base server, as the `opengrid` user, with the production env sourced (the same env every
og-* systemd unit gets):

    su -s /bin/bash opengrid
    set -a; . /etc/opengrid/secrets.env; . /etc/opengrid/api_keys.env; set +a
    export OG_CONFIG=/etc/opengrid/config.toml   # or wherever the deployed config.toml actually lives
    /opt/opengrid/venv/bin/python dev/ops/reset_r2_false_positives.py            # dry run (default)
    /opt/opengrid/venv/bin/python dev/ops/reset_r2_false_positives.py --apply    # then for real
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime

from opengrid.assets.repo import (
    PgAssetEventRepo,
    PgAssetHealthRepo,
    PgCalibrationAttemptRepo,
    PgDriftObservationRepo,
    PgSensitiveGrantPort,
    PgWorkOrderRepo,
    TraceStoreAssetTracePort,
)
from opengrid.assets.service import AssetHealthPorts, AssetHealthService
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend
from psycopg_pool import AsyncConnectionPool

# The three hubs the lead confirmed were false-positived by the R2 ordering bug. Edit this list only
# after confirming (via `SELECT hub_id, asset_state FROM og.hub_inverter_pq WHERE asset_state =
# 'QUARANTINED'`) that no OTHER hub is in scope for this specific incident.
AFFECTED_HUB_IDS = ["hub-01996", "hub-01997", "hub-01998"]

REASON = (
    "R2 incident 2026-09-26: PgDriftObservationRepo read the OLDEST summary in the observation window "
    "as the latest measured offset (rows returned newest-first by pq_ingest.latest_summaries); the "
    "resulting wrong calibration correction verified WORSE_ROLLED_BACK, quarantining a hub that was "
    "never actually drifting. Fixed in opengrid.assets.repo (explicit ts-sort). Reset audited via "
    "AssetHealthService.record_false_positive_reset."
)


async def _reset_one(service: AssetHealthService, hub_id: str, *, apply: bool) -> None:
    record = await service.ports.asset_health.get(hub_id)
    if record is None:
        print(f"{hub_id}: NOT FOUND -- skipping")
        return
    if record.asset_state != "QUARANTINED":
        raise RuntimeError(
            f"{hub_id} is {record.asset_state!r}, not QUARANTINED -- refusing to reset an unexpected "
            "state; confirm this hub is actually part of the R2 incident before touching it manually"
        )
    work_order = await service.ports.work_orders.open_for_hub(hub_id)
    work_order_id = work_order.work_order_id if work_order else None
    print(f"{hub_id}: state={record.asset_state} open_work_order={work_order_id}")
    if not apply:
        print(f"{hub_id}: DRY RUN -- no changes made (pass --apply to reset for real)")
        return
    new_state = await service.record_false_positive_reset(hub_id, reason=REASON, now=datetime.now(UTC))
    print(f"{hub_id}: reset to {new_state}")


async def main(apply: bool) -> None:
    cfg = load_config(os.environ.get("OG_CONFIG"))
    dsn = build_dsn(cfg)
    pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    try:
        trace_store = TraceStore(PgTraceBackend(pool))
        service = AssetHealthService(
            ports=AssetHealthPorts(
                drift=PgDriftObservationRepo(pool),
                asset_health=PgAssetHealthRepo(pool),
                calibration_attempts=PgCalibrationAttemptRepo(pool),
                work_orders=PgWorkOrderRepo(pool),
                asset_events=PgAssetEventRepo(pool),
                trace=TraceStoreAssetTracePort(trace_store),
                sensitive_grants=PgSensitiveGrantPort(pool),
            )
        )
        for hub_id in AFFECTED_HUB_IDS:
            await _reset_one(service, hub_id, apply=apply)
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually perform the reset (default: dry run, prints current state and makes no changes)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply))
