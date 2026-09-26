"""Process entry point for `og-settle` (BUILD.md S4, INTERFACES.md): `python -m opengrid.settle.main`.

Wires the real Postgres-backed `SettleBackend`/`TraceStore` via `opengrid.settle.configure`, then drives
three cadences from ONE `opengrid.platform.process.run_forever` loop:

- every `health.heartbeat_interval_s` (default 5 s): the process heartbeat and `opengrid.health`'s
  evaluator (02b S6.4: heartbeats, hub health, the `ALR-*` alert rules);
- every `settle.interval_s` (default 60 s): `run_settle_cycle()` -- meter, bill and value every pending
  obligation-interval;
- every `settle.trace_prune_interval_s` (default 60 s, 02a S8.3): `run_trace_pruning_cycle()`.

One loop, not three: `run_forever` installs the process's SIGTERM handler, and a second concurrent
`run_forever` replaces the first one's -- the other loops then never stopped and systemd had to SIGKILL
og-settle after 90 s on every deploy (live 2026-09-26). The heartbeat is also written on the fast cadence:
at the 60 s settle cadence, health (miss threshold 3 x 5 s) intermittently reported settle as down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

import opengrid.contracts as contracts
import opengrid.health as health
from opengrid.assets.runner import run_once as run_asset_drift_sweep
from opengrid.assets.service import AssetHealthService
from opengrid.assets.wiring import build_asset_health_service
from opengrid.contracts.pg_repo import PgContractsRepo
from opengrid.health.model import AlertFinding
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.process import Cadence, run_forever
from opengrid.settle import close_settled_obligations, configure, run_settle_cycle, run_trace_pruning_cycle
from opengrid.settle.pg_backend import PgSettleBackend
from opengrid.settle.tariffs import TdspTariff, load_tdsp_tariffs, resolve_tdsp_tariffs_path
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend, journal_path_from_config

_PROCESS_NAME = "settle"  # must match opengrid.health.model.ALL_PROCESSES
_DEFAULT_SETTLE_INTERVAL_S = 60.0
_DEFAULT_TRACE_PRUNE_INTERVAL_S = 60.0
_DEFAULT_HEALTH_INTERVAL_S = 5.0

_logger = logging.getLogger(__name__)


class JobRunner:
    """Starts each due job as its own task, so a slow job (a health pass over 2,000 hubs on a slow disk,
    a large settlement backlog) never delays the others -- above all the heartbeat. A job is not started
    again while its previous run is still in flight; one job's failure is logged and never affects the
    others (K7). Only the one `run_forever` loop owns SIGTERM."""

    def __init__(
        self,
        jobs: list[tuple[str, Cadence, Callable[[], Awaitable[object]]]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs = jobs
        self._clock = clock
        self._running: dict[str, asyncio.Task[None]] = {}
        # Progress: when each job last completed successfully (start-up counts as a success).
        started = clock()
        self._last_success: dict[str, float] = {name: started for name, _, _ in jobs}

    async def run_due(self) -> None:
        for name, cadence, job in self._jobs:
            task = self._running.get(name)
            if task is not None and not task.done():
                continue
            if cadence.due():
                self._running[name] = asyncio.create_task(self._run(name, job))

    def stalled(self, max_age_s: dict[str, float]) -> dict[str, float]:
        """Jobs (among `max_age_s`'s keys) whose last successful run is older than their limit -- hung in
        flight or failing every time -- with the seconds since that success."""
        now = self._clock()
        return {
            name: now - self._last_success[name]
            for name, limit in max_age_s.items()
            if name in self._last_success and now - self._last_success[name] > limit
        }

    async def _run(self, name: str, job: Callable[[], Awaitable[object]]) -> None:
        try:
            await job()
        except Exception:
            _logger.exception("og-settle job failed", extra={"job": name})
        else:
            self._last_success[name] = self._clock()

    async def stop(self) -> None:
        tasks = [t for t in self._running.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_DEFAULT_ASSET_DRIFT_INTERVAL_S = 60.0


def configure_pq_ingest_reader(pool: AsyncConnectionPool, cfg: Any) -> None:
    """Wire `opengrid.pq_ingest` in og-settle for the asset drift sweep's summary reads (og-engine owns
    ingest and flushing; this process never buffers or writes summaries)."""
    import opengrid.pq_ingest as pq_ingest
    from opengrid.pq_ingest.blob_store import FileBlobStore
    from opengrid.pq_ingest.pg_backend import PgPqIngestBackend

    pq_ingest.configure(
        PgPqIngestBackend(pool),
        FileBlobStore(str(cfg.get("pq_ingest.blob_store_dir", "/var/lib/opengrid/pq_waveform"))),
    )


def make_asset_drift_job(service: AssetHealthService) -> Callable[[], Awaitable[None]]:
    """The asset-health drift sweep (`opengrid.assets.runner.run_once`) as an og-settle job: WATCH hubs
    get a PENDING calibration attempt for the guardian to sign (G-25), DEGRADED hubs a work order."""

    async def asset_drift_job() -> None:
        result = await run_asset_drift_sweep(service, now=datetime.now(UTC))
        if result.calibrations_requested or result.work_orders_opened or result.errors:
            _logger.info(
                "asset drift sweep",
                extra={
                    "evaluated": result.evaluated,
                    "calibrations_requested": result.calibrations_requested,
                    "work_orders_opened": result.work_orders_opened,
                    "errors": result.errors,
                },
            )

    return asset_drift_job


STALL_RULE = "ALR-SETTLE-STALLED"
_STALL_INTERVALS = 3  # a settlement job is stalled after 3 missed cadences without a successful run


class StallWatch:
    """Makes a stuck settlement loop visible. og-settle's heartbeat comes from its own job, and health
    (which runs in this process) reports "settle" ok for itself, so neither shows a settle job that hangs
    or fails on every run. On entry to a stall this logs an error and raises `ALR-SETTLE-STALLED` once;
    on recovery it clears that alert."""

    def __init__(
        self,
        stalled: Callable[[], dict[str, float]],
        *,
        raise_alert: Callable[[AlertFinding], Awaitable[int]],
        clear_alert: Callable[[int], Awaitable[None]],
        find_open: Callable[[], Awaitable[dict[str, int]]] | None = None,
    ) -> None:
        self._stalled = stalled
        self._raise = raise_alert
        self._clear = clear_alert
        self._find_open = find_open
        self._open: dict[str, int | None] = {}
        self._adopted = find_open is None

    async def check(self) -> None:
        if not self._adopted:
            # Alerts a previous og-settle raised are ours too: adopt them, so a recovered job clears them
            # and a still-stalled one is not raised twice (health never clears this rule).
            self._open.update(await self._find_open())  # type: ignore[misc]
            self._adopted = True
        stalled = self._stalled()
        for job, age_s in stalled.items():
            if job in self._open:
                continue
            _logger.error("og-settle job stalled", extra={"job": job, "since_success_s": round(age_s, 1)})
            self._open[job] = None
            self._open[job] = await self._raise(
                AlertFinding(
                    rule=STALL_RULE,
                    severity="critical",
                    summary=f"og-settle job '{job}' has not completed for {int(age_s)} s",
                    condition_key=f"{STALL_RULE}:settle",
                    detail={"process": "settle", "job": job, "since_success_s": round(age_s, 1)},
                )
            )
        for job in [j for j in self._open if j not in stalled]:
            alert_id = self._open.pop(job)
            _logger.info("og-settle job progressing again", extra={"job": job})
            if alert_id is not None:
                await self._clear(alert_id)


async def open_stall_alerts(pool: AsyncConnectionPool) -> dict[str, int]:
    """Open `ALR-SETTLE-STALLED` alerts by job (detail `job`), e.g. left by a previous og-settle."""
    return {
        str((a.detail or {}).get("job", "settle")): a.id
        for a in await fetch_open_alerts(pool)
        if a.rule == STALL_RULE and a.id is not None
    }


async def _run() -> None:
    logger = configure_logging(_PROCESS_NAME)
    cfg = load_config(os.environ.get("OG_CONFIG"))
    pool = await make_pool(cfg)
    trace_store = TraceStore(PgTraceBackend(pool, journal_path=journal_path_from_config(cfg)))
    # 09 D5's M1 TDSP delivery charge: loaded once at startup, never re-read per settlement cycle.
    # Missing/malformed tdsp_tariffs.toml degrades to "M1 disabled" (delivery_charge settles as 0),
    # logged loudly rather than crashing og-settle over a pricing config file (K7 "degrade, don't
    # trip") -- BUILD.md S5a still applies: this is a logged degradation, not a silent one.
    tdsp_tariffs: list[TdspTariff] | None = None
    zone_default_tdsp: dict[str, str] | None = None
    try:
        tdsp_tariffs, zone_default_tdsp = load_tdsp_tariffs(resolve_tdsp_tariffs_path(cfg.source_path))
    except (OSError, ValueError, KeyError):
        logger.exception("could not load tdsp_tariffs.toml: M1 delivery charge disabled this run")
    configure(
        PgSettleBackend(pool),
        trace_store,
        trace_pool=pool,
        tdsp_tariffs=tdsp_tariffs,
        zone_default_tdsp=zone_default_tdsp,
    )
    # FULFILLED/SHORTFALL -> SETTLED goes through opengrid.contracts (single writer of obligation
    # state). og-settle never calls admit()/admit_priced() itself, but this process-wide config value
    # is threaded through anyway for consistency with whichever process does (og-engine).
    data_center_activation_enabled = bool(cfg.get("contracts.activation.data_center", False))
    contracts.configure(
        PgContractsRepo(pool), trace_store, data_center_activation_enabled=data_center_activation_enabled
    )
    health.configure(pool, cfg)

    health_interval_s = float(cfg.get("health.heartbeat_interval_s", _DEFAULT_HEALTH_INTERVAL_S))
    settle_interval_s = float(cfg.get("settle.interval_s", _DEFAULT_SETTLE_INTERVAL_S))

    async def settle_job() -> None:
        settled_count = await run_settle_cycle()
        if settled_count:
            logger.info("settlement cycle complete", extra={"settled_count": settled_count})
        closed = await close_settled_obligations()
        if closed:
            logger.info("obligations settled", extra={"settled_obligations": closed})

    async def prune_job() -> None:
        deleted = await run_trace_pruning_cycle()
        if deleted:
            logger.info("trace pruning cycle complete", extra={"deleted_by_stream": deleted})

    jobs: list[tuple[str, Cadence, Callable[[], Awaitable[object]]]] = [
        ("heartbeat", Cadence(health_interval_s), lambda: write_heartbeat(pool, _PROCESS_NAME)),
        ("health", Cadence(health_interval_s), health.evaluate_once),
        ("settle", Cadence(settle_interval_s), settle_job),
        (
            "trace_prune",
            Cadence(float(cfg.get("settle.trace_prune_interval_s", _DEFAULT_TRACE_PRUNE_INTERVAL_S))),
            prune_job,
        ),
    ]
    if bool(cfg.get("assets.drift_enabled", False)):
        # The drift sweep reads measured waveform summaries through opengrid.pq_ingest (read-only here);
        # unconfigured, every hub's evaluation raised (live 2026-09-26: errors=2000 per sweep).
        configure_pq_ingest_reader(pool, cfg)
        jobs.append(
            (
                "asset_drift",
                Cadence(float(cfg.get("assets.drift_interval_s", _DEFAULT_ASSET_DRIFT_INTERVAL_S))),
                make_asset_drift_job(build_asset_health_service(pool, trace_store)),
            )
        )
    runner = JobRunner(jobs)
    watch = StallWatch(
        lambda: runner.stalled({"settle": _STALL_INTERVALS * settle_interval_s}),
        raise_alert=lambda finding: raise_alert(pool, finding, opened_at=datetime.now(UTC)),
        clear_alert=lambda alert_id: clear_alert(pool, alert_id),
        find_open=lambda: open_stall_alerts(pool),
    )
    jobs.append(("stall_watch", Cadence(health_interval_s), watch.check))
    try:
        await run_forever(runner.run_due, interval_s=health_interval_s, process_name=_PROCESS_NAME)
    finally:
        await runner.stop()
        await pool.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
