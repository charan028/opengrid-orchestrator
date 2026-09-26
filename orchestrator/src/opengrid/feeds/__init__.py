"""opengrid.feeds -- process og-feeds (02b S2). Owner: feeds agent (BUILD.md S4).

Polls ERCOT Public API / EIA v2 / NWS per `interfaces/http/market-api.md`, normalizes into
`feed_obs`/`feed_status`, and runs the staleness/circuit-breaker/key-rotation logic (`opengrid.feeds
.scheduler.FeedsScheduler`). `latest()`/`window()` are the fixed public read interface (02b S2) --
backed by a single `FeedStore` instance set up once by `run_feeds_process` (or by tests via
`configure_for_testing`).

This module has no `opengrid.forecast` awareness: `opengrid.feeds` itself (this module object) satisfies
`opengrid.forecast.HistoryProvider` structurally, purely by exposing `latest`/`window` with the matching
shape -- `feeds/main.py` passes the module in when it wires `opengrid.forecast.configure(...)` (a
separately-owned package, BUILD.md S4; this module never imports it, per the lead's no-duplication
ruling)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.eia import EiaClient
from opengrid.feeds.ercot import DEFAULT_TOKEN_URL, ErcotClient
from opengrid.feeds.nws import NwsClient
from opengrid.feeds.scheduler import FeedsScheduler
from opengrid.feeds.store import FeedStore
from opengrid.feeds.token_bucket import TokenBucket
from opengrid.platform.config import Config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.process import run_forever
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

PROCESS_NAME = "feeds"
SCHEDULER_TICK_INTERVAL_S = 5.0

_store: FeedStore | None = None


def configure_for_testing(store: FeedStore | None) -> None:
    """Test-only hook: inject a `FeedStore` (real or fake-backed) so `latest()`/`window()` work without
    going through `run_feeds_process`. Never called from production code."""
    global _store
    _store = store


def _require_store() -> FeedStore:
    if _store is None:
        raise RuntimeError(
            "opengrid.feeds is not initialized -- run_feeds_process() must run first "
            "(or call configure_for_testing() in tests)"
        )
    return _store


async def latest(series: str) -> FeedObs:
    """Return the most recent accepted observation for `series` (02b S2 interface: `latest(series)`).

    Raises `LookupError` if no observation has ever been recorded for `series`.
    """
    return await _require_store().latest(series)


async def window(series: str, t0: datetime, t1: datetime) -> list[FeedObs]:
    """Return observations for `series` with `t0 <= ts < t1`, ordered by `ts` ascending."""
    return await _require_store().window(series, t0, t1)


def _build_scheduler(
    cfg: Config, store: FeedStore, http_client: httpx.AsyncClient, trace: TraceStore | None
) -> FeedsScheduler:
    ercot_cfg = cfg.get("feeds.ercot", {})
    eia_cfg = cfg.get("feeds.eia", {})
    nws_cfg = cfg.get("feeds.nws", {})
    staleness_cfg = cfg.get("feeds.staleness", {})

    ercot = ErcotClient(
        base_url=ercot_cfg["base_url"],
        username_env=ercot_cfg["username_env"],
        password_env=ercot_cfg["password_env"],
        primary_key_env=ercot_cfg["subscription_key_env"],
        secondary_key_env=ercot_cfg.get("subscription_key_secondary_env", "ERCOT_PUBLIC_API_KEY_SECONDARY"),
        http_client=http_client,
        token_url=ercot_cfg.get("token_url", DEFAULT_TOKEN_URL),
    )
    eia = EiaClient(
        base_url=eia_cfg["base_url"],
        api_key_env=eia_cfg["api_key_env"],
        respondent=eia_cfg.get("respondent", "ERCO"),
        http_client=http_client,
    )
    nws = NwsClient(base_url=nws_cfg["base_url"], user_agent=nws_cfg["user_agent"], http_client=http_client)

    budget_per_min = float(ercot_cfg.get("budget_requests_per_min", 24))
    bucket = TokenBucket(capacity=max(1.0, budget_per_min / 6), refill_per_s=budget_per_min / 60.0)

    return FeedsScheduler(
        ercot=ercot,
        eia=eia,
        nws=nws,
        store=store,
        staleness_cfg=staleness_cfg,
        nws_grid_point_pinned=nws_cfg.get("grid_point"),
        ercot_bucket=bucket,
        trace=trace,
    )


async def _tick_with_heartbeat(
    run_cycle: Callable[[], Awaitable[None]],
    write_hb: Callable[[], Awaitable[None]],
    extra_tick: Callable[[], Awaitable[None]] | None,
) -> None:
    """One `og-feeds` tick: run the scheduler cycle, then *always* write the heartbeat -- `finally`,
    not sequential, so a `run_cycle()` exception can't also skip it (live defect: og-feeds' heartbeat
    was never written because every cycle's NWS poll raised `KeyError` before reaching the write, and
    `run_forever` logs-and-continues on a bad tick rather than stopping the process, so this silently
    starved `health`'s heartbeat check on every single cycle, not just the failing one). `extra_tick`
    (forecast's recompute cadence, folded in by `feeds/main.py`) only runs after a clean cycle."""
    try:
        await run_cycle()
    finally:
        await write_hb()
    if extra_tick is not None:
        await extra_tick()


async def run_feeds_process(cfg: Config, *, extra_tick: Callable[[], Awaitable[None]] | None = None) -> None:
    """Entry point for `python -m opengrid.feeds.main`: scheduler loop polling ERCOT/EIA/NWS per
    `[feeds.*]` config, writing `feed_obs`/`feed_status`, and tracing feed-quality changes/key
    rotations (`event_class="FEED_CHANGE"`).

    `extra_tick`, when given, runs after each cycle's heartbeat write, on the same `run_forever` loop
    -- this is how `feeds/main.py` folds `opengrid.forecast`'s recompute cadence (02b S1.2: "forecast
    runs inside feeds' cycle") into feeds' own tick instead of running a second, independent
    `run_forever` loop in this process. Two such loops each call `asyncio.loop.add_signal_handler` for
    SIGTERM/SIGINT, and asyncio allows only one handler per signal -- the second registration silently
    replaced the first's, so one of the two loops never saw the shutdown request at all (live defect:
    og-feeds ignored SIGTERM and was SIGKILLed by systemd after 90s). A single `run_forever` loop means
    a single shutdown path. This function still has no import-time dependency on the separately-owned
    `forecast` package (BUILD.md S1 no-duplication ruling); `extra_tick` is an opaque callable."""
    global _store
    pool = await make_pool(cfg)
    staleness_cfg = cfg.get("feeds.staleness", {})
    store = FeedStore(pool, staleness_cfg=staleness_cfg)
    _store = store

    trace_store: TraceStore | None = None
    try:
        from opengrid.trace.pg_backend import PgTraceBackend  # health-owned (BUILD.md S4)

        trace_store = TraceStore(PgTraceBackend(pool))
    except ImportError:
        # Degrade, don't trip (K7): if health's backend is ever unavailable, log FEED_CHANGE events
        # instead of writing to the trace table rather than crashing the feeds process over it.
        logger.warning(
            "opengrid.trace.pg_backend not available -- feeds will log FEED_CHANGE events "
            "instead of writing to the trace table (degraded, visible in logs)"
        )

    try:
        async with httpx.AsyncClient() as http_client:
            scheduler = _build_scheduler(cfg, store, http_client, trace_store)

            async def _tick() -> None:
                await _tick_with_heartbeat(
                    scheduler.run_cycle, lambda: write_heartbeat(pool, PROCESS_NAME), extra_tick
                )

            await run_forever(_tick, interval_s=SCHEDULER_TICK_INTERVAL_S, process_name=PROCESS_NAME)
    finally:
        await pool.close()
