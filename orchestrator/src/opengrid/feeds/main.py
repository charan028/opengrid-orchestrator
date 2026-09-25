"""`python -m opengrid.feeds.main` -- systemd unit `og-feeds` (BUILD.md S4, INTERFACES.md).

02b S1.2: `og-feeds` also hosts `opengrid.forecast`'s recompute cycle ("forecast runs inside feeds'
cycle -- no separate process, MVP-S"). Per the lead's no-duplication ruling, this file's *only* role
in that is to call `opengrid.forecast.configure(...)` once and `opengrid.forecast.run_forecast_cycle()`
on a 15-min cadence -- `opengrid.feeds` itself is passed as the `HistoryProvider` (it satisfies that
protocol structurally via `latest`/`window`), and `opengrid.forecast.pg_backend.PgForecastBackend` (the
forecast package's own persistence, not feeds') is the `ForecastBackend`. No forecast logic lives here.
"""

from __future__ import annotations

import asyncio
import os

import opengrid.forecast as forecast
from opengrid.core.timeutil import INTERVAL_MINUTES
from opengrid.feeds import run_feeds_process
from opengrid.forecast.pg_backend import PgForecastBackend
from opengrid.platform.config import Config, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.log import configure_logging
from opengrid.platform.process import run_forever

FORECAST_RECOMPUTE_INTERVAL_S = INTERVAL_MINUTES * 60


async def _run_forecast_cycle(cfg: Config) -> None:
    """Wire and drive `opengrid.forecast` on its own connection pool (`run_feeds_process` owns its own
    pool for ERCOT/EIA/NWS + `feed_obs`/`feed_status`; forecast's pool is separate so neither package's
    lifecycle depends on the other's, per BUILD.md S1 "no duplicated functions" / "no cross-imports")."""
    import opengrid.feeds as feeds_module

    pool = await make_pool(cfg)
    try:
        forecast.configure(cfg, history=feeds_module, backend=PgForecastBackend(pool))

        async def _tick() -> None:
            await forecast.run_forecast_cycle()

        await run_forever(_tick, interval_s=FORECAST_RECOMPUTE_INTERVAL_S, process_name="feeds.forecast")
    finally:
        await pool.close()


async def _async_main(cfg: Config) -> None:
    await asyncio.gather(run_feeds_process(cfg), _run_forecast_cycle(cfg))


def main() -> None:
    configure_logging("feeds")
    cfg = load_config(os.environ.get("OG_CONFIG"))
    asyncio.run(_async_main(cfg))


if __name__ == "__main__":
    main()
