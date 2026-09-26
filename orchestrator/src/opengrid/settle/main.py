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

import opengrid.health as health
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.process import run_forever
from opengrid.settle import configure, run_settle_cycle, run_trace_pruning_cycle
from opengrid.settle.pg_backend import PgSettleBackend
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend

_PROCESS_NAME = "settle"  # must match opengrid.health.model.ALL_PROCESSES
_DEFAULT_SETTLE_INTERVAL_S = 60.0
_DEFAULT_TRACE_PRUNE_INTERVAL_S = 60.0
_DEFAULT_HEALTH_INTERVAL_S = 5.0

_logger = logging.getLogger(__name__)


class Cadence:
    """Due-check for one periodic job inside a shared tick: due on the first call, then once at least
    `interval_s` has elapsed on the (injectable) monotonic clock since it last ran."""

    def __init__(self, interval_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = interval_s
        self._clock = clock
        self._last: float | None = None

    def due(self) -> bool:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval_s:
            return False
        self._last = now
        return True


class JobRunner:
    """Starts each due job as its own task, so a slow job (a health pass over 2,000 hubs on a slow disk,
    a large settlement backlog) never delays the others -- above all the heartbeat. A job is not started
    again while its previous run is still in flight; one job's failure is logged and never affects the
    others (K7). Only the one `run_forever` loop owns SIGTERM."""

    def __init__(self, jobs: list[tuple[str, Cadence, Callable[[], Awaitable[object]]]]) -> None:
        self._jobs = jobs
        self._running: dict[str, asyncio.Task[None]] = {}

    async def run_due(self) -> None:
        for name, cadence, job in self._jobs:
            task = self._running.get(name)
            if task is not None and not task.done():
                continue
            if cadence.due():
                self._running[name] = asyncio.create_task(self._run(name, job))

    @staticmethod
    async def _run(name: str, job: Callable[[], Awaitable[object]]) -> None:
        try:
            await job()
        except Exception:
            _logger.exception("og-settle job failed", extra={"job": name})

    async def stop(self) -> None:
        tasks = [t for t in self._running.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run() -> None:
    logger = configure_logging(_PROCESS_NAME)
    cfg = load_config(os.environ.get("OG_CONFIG"))
    pool = await make_pool(cfg)
    configure(PgSettleBackend(pool), TraceStore(PgTraceBackend(pool)), trace_pool=pool)
    health.configure(pool, cfg)

    health_interval_s = float(cfg.get("health.heartbeat_interval_s", _DEFAULT_HEALTH_INTERVAL_S))

    async def settle_job() -> None:
        settled_count = await run_settle_cycle()
        if settled_count:
            logger.info("settlement cycle complete", extra={"settled_count": settled_count})

    async def prune_job() -> None:
        deleted = await run_trace_pruning_cycle()
        if deleted:
            logger.info("trace pruning cycle complete", extra={"deleted_by_stream": deleted})

    jobs: list[tuple[str, Cadence, Callable[[], Awaitable[object]]]] = [
        ("heartbeat", Cadence(health_interval_s), lambda: write_heartbeat(pool, _PROCESS_NAME)),
        ("health", Cadence(health_interval_s), health.evaluate_once),
        ("settle", Cadence(float(cfg.get("settle.interval_s", _DEFAULT_SETTLE_INTERVAL_S))), settle_job),
        (
            "trace_prune",
            Cadence(float(cfg.get("settle.trace_prune_interval_s", _DEFAULT_TRACE_PRUNE_INTERVAL_S))),
            prune_job,
        ),
    ]
    runner = JobRunner(jobs)
    try:
        await run_forever(runner.run_due, interval_s=health_interval_s, process_name=_PROCESS_NAME)
    finally:
        await runner.stop()
        await pool.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
