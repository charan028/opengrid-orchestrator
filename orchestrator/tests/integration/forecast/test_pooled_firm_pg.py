"""Integration test for the short-history firm-fitness relaxation (`[forecast].pool_day_types_when_short`)
end to end against real Postgres: history seeded through `FeedStore` into `og.feed_obs`, read back by
`compute_and_persist` through the same `FeedStore` the live process uses, persisted by
`PgForecastBackend` into `og.forecast`. Run via:

    powershell -File tools\\remote.ps1 -Ws fcst -Cmd "cd orchestrator && python -m pytest tests/integration/forecast -q"

History is seeded relative to the real clock (so `FeedStore`'s read-time staleness never flags it),
covering the last 2.5 days: time-of-day slots inside the last 12 h have 3 daily samples, the rest 2.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import FeedObs
from opengrid.core.timeutil import floor_to_interval
from opengrid.feeds.store import FeedStore
from opengrid.forecast.models import ForecastRow
from opengrid.forecast.pg_backend import PgForecastBackend
from opengrid.forecast.service import compute_and_persist
from opengrid.platform.config import Config, load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.selector.db import _UNFIT_PRICE_SERIES_SQL

pytestmark = pytest.mark.asyncio

_SERIES = "TS-FCST-POOLED"
_PRODUCT = "np6-905-cd"


def _dsn_or_skip() -> str:
    config_path = os.environ.get("OG_CONFIG")
    if not config_path:
        pytest.skip("OG_CONFIG not set -- run via tools/remote.ps1 (BUILD.md S5 Server)")
    dsn = build_dsn(load_config(config_path))
    try:
        migrate_sync(dsn)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    return dsn


async def _cleanup(p: AsyncConnectionPool) -> None:
    async with p.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM og.forecast WHERE series_key = %s", (_SERIES,))
        await conn.execute("DELETE FROM og.feed_obs WHERE series = %s", (_SERIES,))


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(_dsn_or_skip(), min_size=1, max_size=2, open=False)
    await p.open(wait=True)
    await _cleanup(p)
    try:
        yield p
    finally:
        await _cleanup(p)
        await p.close()


def _cfg(*, pool_day_types: bool) -> Config:
    return Config(
        {
            "forecast": {
                "horizon_hours": 24,
                "resolution_min": 15,
                "price_series": [_SERIES],
                "load_series": [],
                "pool_day_types_when_short": pool_day_types,
                "min_samples_firm": 3,
            }
        }
    )


async def test_pooled_relaxation_round_trip(pool: AsyncConnectionPool) -> None:
    now = datetime.now(UTC)
    last = floor_to_interval(now, 15) - timedelta(minutes=15)
    seed = [
        FeedObs(
            source="ERCOT",
            product=_PRODUCT,
            series=_SERIES,
            ts=last - timedelta(minutes=15 * i),
            value=30.0 + (i % 7),
            unit="usd_per_mwh",
            quality="GOOD",
            recorded_at=now,
        )
        for i in range(int(2.5 * 96))
    ]
    store = FeedStore(pool, staleness_cfg={"ercot_price_fresh_s": 2700})
    await store.upsert_obs(seed)
    await store.upsert_obs(seed[:10])  # idempotent re-post: same natural key, no duplicates

    backend = PgForecastBackend(pool)
    strict_rows = await compute_and_persist(_cfg(pool_day_types=False), store, backend, now=now)
    pooled_rows = await compute_and_persist(_cfg(pool_day_types=True), store, backend, now=now)

    firm = ("FIRM_OK", "FIRM_POOLED")
    strict_firm = sum(r.firm_fitness in firm for r in strict_rows)
    pooled_firm = sum(r.firm_fitness in firm for r in pooled_rows)
    assert len(pooled_rows) == 96
    assert pooled_firm >= strict_firm
    assert pooled_firm > 0  # ~12 h of slots have 3 daily samples across any day-type mix
    assert pooled_firm < 96  # the other slots have only 2: still NOT_FOR_FIRM

    persisted = await backend.fetch_range(
        pooled_rows[0].interval_start_utc, pooled_rows[-1].interval_start_utc
    )
    by_ts = {r.interval_start_utc: r.firm_fitness for r in persisted if r.series_key == _SERIES}
    assert by_ts == {r.interval_start_utc: r.firm_fitness for r in pooled_rows[:-1]}  # basis stored as-is
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM og.feed_obs WHERE series = %s", (_SERIES,))
        row = await cur.fetchone()
    assert row is not None and row[0] == len(seed)


async def test_firm_pooled_is_stored_and_is_not_unfit_for_the_selector(pool: AsyncConnectionPool) -> None:
    """Migration 0040: FIRM_POOLED passes the CHECK, and the selector's unfit-series query (which
    withholds only NOT_FOR_FIRM) does not list a series whose slots are FIRM_POOLED."""
    base = floor_to_interval(datetime.now(UTC), 15) + timedelta(hours=1)
    rows = [
        ForecastRow(
            series_key=_SERIES,
            kind="price",
            interval_start_utc=base + timedelta(minutes=15 * i),
            horizon_step=i,
            p10=1.0,
            p50=2.0,
            p90=3.0,
            firm_fitness="FIRM_POOLED" if i % 2 else "FIRM_OK",
        )
        for i in range(4)
    ]
    backend = PgForecastBackend(pool)
    await backend.upsert_rows(rows)
    fetched = await backend.fetch_range(base, base + timedelta(hours=1))
    assert sorted(r.firm_fitness for r in fetched if r.series_key == _SERIES) == [
        "FIRM_OK",
        "FIRM_OK",
        "FIRM_POOLED",
        "FIRM_POOLED",
    ]
    params = {"horizon_start": base, "horizon_end": base + timedelta(hours=1)}
    async with pool.connection() as conn:
        cur = await conn.execute(_UNFIT_PRICE_SERIES_SQL, params)
        unfit = {r[0] for r in await cur.fetchall()}
        assert _SERIES not in unfit
        with pytest.raises(psycopg.errors.CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE og.forecast SET firm_fitness = 'MAYBE' WHERE series_key = %s", (_SERIES,)
                )
