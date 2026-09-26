"""`run_lifecycle(pool, cfg)` -- the single entry point (CLI, systemd timer or an in-process loop).

Cycles:
  * fast   (every 10 min): daily partitions ahead (+ default swap), time-column indexes, rollups;
  * hourly (every hour):   fast + hot->cold export, retention, the monthly billing export;
  * auto:                  hourly when the last successful hourly run started >= 55 min ago, else fast --
                           one 10-min systemd timer drives both.

One runner at a time (session advisory lock; a second concurrent run returns `{"skipped": "locked"}`).
Every run is recorded in og.lifecycle_run and traced (stream `lifecycle`, event_class `lifecycle`,
decision_type `DATA_LIFECYCLE`) with its counts. A failing step never stops the others; the run is then
`ok = false` (the CLI exits 1 so systemd's OnFailure/health alerting can pick it up).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle import partitions
from opengrid.lifecycle.archive import export_billing_month
from opengrid.lifecycle.indexes import ensure_indexes
from opengrid.lifecycle.policy import LifecycleConfig, closed_months
from opengrid.lifecycle.retention import run_retention
from opengrid.lifecycle.rollup import run_rollups
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

Cycle = Literal["fast", "hourly", "auto"]
HOURLY_EVERY = timedelta(minutes=55)

ADVISORY_LOCK_KEY = 0x0613_1FEC_0033  # "og lifecycle 0033"
TRACE_STREAM = "lifecycle"
TRACE_EVENT_CLASS = "lifecycle"
TRACE_DECISION_TYPE = "DATA_LIFECYCLE"


def _trace_store(pool: AsyncConnectionPool, cfg: Any) -> TraceStore:
    from opengrid.trace.pg_backend import PgTraceBackend, journal_path_from_config

    return TraceStore(PgTraceBackend(pool, journal_path=journal_path_from_config(cfg)))


async def _billing(pool: AsyncConnectionPool, lcfg: LifecycleConfig, now: datetime) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT least((SELECT min(created_at) FROM og.invoice_line), (SELECT min(created_at) FROM og.pnl))"
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return []
    first: date = row[0].astimezone(UTC).date()
    done = []
    for month in closed_months(first, now):
        result = await export_billing_month(pool, lcfg, month)
        if "skipped" not in result:
            done.append(result)
    return done


async def run_lifecycle(
    pool: AsyncConnectionPool,
    cfg: Any,
    *,
    cycle: Cycle = "hourly",
    now: datetime | None = None,
    trace_store: TraceStore | None = None,
) -> dict[str, Any]:
    """Run one lifecycle cycle. `cfg` is the platform Config (or anything with `.get`) or a ready
    LifecycleConfig."""
    lcfg = cfg if isinstance(cfg, LifecycleConfig) else LifecycleConfig.from_config(cfg)
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {"cycle": cycle, "at": now.isoformat(), "errors": []}

    async with pool.connection() as lock_conn:
        await lock_conn.set_autocommit(True)
        try:
            cur = await lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            got = await cur.fetchone()
            if not got or not got[0]:
                return {"cycle": cycle, "skipped": "locked"}
            try:
                if cycle == "auto":
                    cur = await lock_conn.execute(
                        "SELECT max(started_at) FROM og.lifecycle_run WHERE cycle = 'hourly' AND ok"
                    )
                    last = await cur.fetchone()
                    due = last is None or last[0] is None or now - last[0] >= HOURLY_EVERY
                    cycle = "hourly" if due else "fast"
                    summary["cycle"] = cycle
                run_id = uuid4()
                await lock_conn.execute(
                    "INSERT INTO og.lifecycle_run (run_id, cycle, started_at) VALUES (%s, %s, %s)",
                    (run_id, cycle, now),
                )
                await _steps(pool, lcfg, cycle, now, summary)
                ok = not summary["errors"]
                summary["ok"] = ok
                await lock_conn.execute(
                    "UPDATE og.lifecycle_run SET finished_at = now(), ok = %s, summary = %s WHERE run_id = %s",
                    (ok, Jsonb(summary), run_id),
                )
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        finally:
            await lock_conn.set_autocommit(False)

    store = trace_store if trace_store is not None else _trace_store(pool, cfg)
    try:
        await store.append(
            TRACE_STREAM,
            TRACE_DECISION_TYPE,
            TRACE_EVENT_CLASS,
            {"run_id": str(run_id), **summary},
            reason_codes=None if summary["ok"] else ["LIFECYCLE_STEP_FAILED"],
        )
    except Exception as exc:  # a trace outage must not turn a finished run into a crash
        logger.error("lifecycle run could not be traced", extra={"error": str(exc)})
        summary["errors"].append(f"trace: {exc}")
        summary["ok"] = False
    log = logger.info if summary["ok"] else logger.error
    log("lifecycle run finished", extra={"summary": summary})
    return summary


async def _steps(
    pool: AsyncConnectionPool, lcfg: LifecycleConfig, cycle: Cycle, now: datetime, summary: dict[str, Any]
) -> None:
    async def step(name: str, coro: Any) -> None:
        try:
            summary[name] = await coro
        except Exception as exc:
            logger.exception("lifecycle step failed", extra={"step": name})
            summary["errors"].append(f"{name}: {exc}")

    rollup_back = math.ceil(lcfg.rollup_backfill_hours / 24) + 1
    await step("partitions_telemetry", partitions.ensure_partitions(pool, lcfg, "telemetry", now))
    await step(
        "partitions_telemetry_1m",
        partitions.ensure_partitions(pool, lcfg, "telemetry_1m", now, days_back=rollup_back),
    )
    await step("indexes", ensure_indexes(pool))
    await step("rollups", run_rollups(pool, lcfg, now))
    if cycle == "hourly":
        outcomes: list[Any] = []

        async def retention() -> list[dict[str, Any]]:
            result = await run_retention(pool, lcfg, now)
            outcomes.extend(result)
            return [o.as_dict() for o in result]

        await step("retention", retention())
        for o in outcomes:
            if o.error:
                summary["errors"].append(f"retention {o.table}: {o.error}")
        await step("billing_export", _billing(pool, lcfg, now))
