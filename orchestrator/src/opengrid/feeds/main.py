"""`python -m opengrid.feeds.main` -- systemd unit `og-feeds` (BUILD.md S4, INTERFACES.md).

02b S1.2: `og-feeds` also hosts `opengrid.forecast`'s recompute cycle ("forecast runs inside feeds'
cycle -- no separate process, MVP-S"). Per the lead's no-duplication ruling, this file's *only* role
in that is to call `opengrid.forecast.configure(...)` once and `opengrid.forecast.run_forecast_cycle()`
on a 15-min cadence -- `opengrid.feeds` itself is passed as the `HistoryProvider` (it satisfies that
protocol structurally via `latest`/`window`), and `opengrid.forecast.pg_backend.PgForecastBackend` (the
forecast package's own persistence, not feeds') is the `ForecastBackend`. No forecast logic lives here.

The recompute cadence is folded into `run_feeds_process`'s own `run_forever` loop via `extra_tick`
(one loop, one shutdown path) rather than run on a second, independent `run_forever` loop in this
process -- two such loops each install their own SIGTERM/SIGINT handlers on the same asyncio loop, and
asyncio allows only one handler per signal, so the second registration silently replaced the first's:
whichever loop registered first never saw the shutdown request. That was a live defect (og-feeds
ignored SIGTERM and was SIGKILLed by systemd after its 90s grace period): fixed by never having two
`run_forever` calls in this process. See `opengrid.feeds.run_feeds_process`'s docstring.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

import opengrid.forecast as forecast
from opengrid.core.timeutil import INTERVAL_MINUTES
from opengrid.feeds import run_feeds_process
from opengrid.forecast.pg_backend import PgForecastBackend
from opengrid.platform.config import Config, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.log import configure_logging

FORECAST_RECOMPUTE_INTERVAL_S = INTERVAL_MINUTES * 60


def _forecast_tick() -> Callable[[], Awaitable[None]]:
    """Build the `extra_tick` hook `run_feeds_process` calls every cycle: forecast's own 15-minute
    recompute cadence, self-timed against `time.monotonic()` since this hook rides on feeds' own 5s
    tick rather than a dedicated `run_forever` interval."""
    next_due_at = 0.0  # due immediately on the first tick

    async def _tick() -> None:
        nonlocal next_due_at
        now = time.monotonic()
        if now < next_due_at:
            return
        next_due_at = now + FORECAST_RECOMPUTE_INTERVAL_S
        await forecast.run_forecast_cycle()

    return _tick


async def _async_main(cfg: Config) -> None:
    """Wire `opengrid.forecast` onto its own connection pool (kept separate from `run_feeds_process`'s
    -- ERCOT/EIA/NWS + `feed_obs`/`feed_status` on one, forecast's own persistence on the other -- so
    neither package's lifecycle depends on the other's, per BUILD.md S1 "no duplicated functions" /
    "no cross-imports"), then run feeds' single `run_forever` loop with forecast's recompute folded in
    as `extra_tick` (see module docstring for why this must be one loop, not two)."""
    import opengrid.feeds as feeds_module

    pool = await make_pool(cfg)
    try:
        forecast.configure(cfg, history=feeds_module, backend=PgForecastBackend(pool))
        await run_feeds_process(cfg, extra_tick=_forecast_tick())
    finally:
        await pool.close()


def main() -> None:
    configure_logging("feeds")
    cfg = load_config(os.environ.get("OG_CONFIG"))
    asyncio.run(_async_main(cfg))


if __name__ == "__main__":
    main()
