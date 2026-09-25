"""Process entry point for `og-settle` (BUILD.md S4, INTERFACES.md): `python -m opengrid.settle.main`.

Wires the real Postgres-backed `SettleBackend`/`TraceStore` via `opengrid.settle.configure`, then runs
two independent cadences on `opengrid.platform.process.run_forever`:

- every `settle.interval_s` (default 60s): `run_settle_cycle()` -- meter, bill and value every
  obligation-interval the backend reports as pending;
- every `settle.trace_prune_interval_s` (default 60s, 02a S8.3's checkpoint cadence): `
  run_trace_pruning_cycle()`.

Both loops write a heartbeat each tick (02b S6.4) so `health.evaluate_heartbeats()` can see `og-settle`
is alive even on a tick that finds nothing to do.
"""

from __future__ import annotations

import asyncio
import os

from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.process import run_forever
from opengrid.settle import configure, run_settle_cycle, run_trace_pruning_cycle
from opengrid.settle.pg_backend import PgSettleBackend
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend

_PROCESS_NAME = "og-settle"
_DEFAULT_SETTLE_INTERVAL_S = 60.0
_DEFAULT_TRACE_PRUNE_INTERVAL_S = 60.0


async def _run() -> None:
    logger = configure_logging(_PROCESS_NAME)
    cfg = load_config(os.environ.get("OG_CONFIG"))
    pool = await make_pool(cfg)
    configure(PgSettleBackend(pool), TraceStore(PgTraceBackend(pool)), trace_pool=pool)

    settle_interval_s = float(cfg.get("settle.interval_s", _DEFAULT_SETTLE_INTERVAL_S))
    trace_prune_interval_s = float(cfg.get("settle.trace_prune_interval_s", _DEFAULT_TRACE_PRUNE_INTERVAL_S))

    async def settle_tick() -> None:
        await write_heartbeat(pool, _PROCESS_NAME)
        settled_count = await run_settle_cycle()
        if settled_count:
            logger.info("settlement cycle complete", extra={"settled_count": settled_count})

    async def trace_prune_tick() -> None:
        deleted = await run_trace_pruning_cycle()
        if deleted:
            logger.info("trace pruning cycle complete", extra={"deleted_by_stream": deleted})

    await asyncio.gather(
        run_forever(settle_tick, interval_s=settle_interval_s, process_name=_PROCESS_NAME),
        run_forever(
            trace_prune_tick, interval_s=trace_prune_interval_s, process_name=f"{_PROCESS_NAME}-trace"
        ),
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
