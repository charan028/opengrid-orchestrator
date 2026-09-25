"""Integration test for `PgForecastBackend` against a real Postgres `og.forecast` (02b S3), per
BUILD.md's "Server (integration, DB, MQTT, live APIs)" instructions. Run via:

    powershell -File tools\\remote.ps1 -Ws fcst -Cmd "cd orchestrator && python -m opengrid.platform.db migrate && python -m pytest tests/integration/forecast -q"

Skips itself (rather than failing) when `OG_CONFIG`/Postgres are not reachable, so `pytest tests` still
runs cleanly on a laptop with no DB (unit tests only, per BUILD.md S5 "Local").
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from opengrid.forecast.models import ForecastRow
from opengrid.forecast.pg_backend import PgForecastBackend
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = pytest.mark.asyncio

_TEST_SERIES_KEY = "TS-02-INTEGRATION-TEST"


def _dsn_or_skip() -> str:
    config_path = os.environ.get("OG_CONFIG")
    if not config_path:
        pytest.skip("OG_CONFIG not set -- run via tools/remote.ps1 (BUILD.md S5 Server)")
    cfg = load_config(config_path)
    dsn = build_dsn(cfg)
    try:
        migrate_sync(dsn)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    return dsn


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    dsn = _dsn_or_skip()
    p = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
    await p.open(wait=True)
    try:
        yield p
    finally:
        async with p.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM og.forecast WHERE series_key = %s", (_TEST_SERIES_KEY,))
        await p.close()


def _row(step: int, base_ts: datetime, *, p10: float, p50: float, p90: float) -> ForecastRow:
    return ForecastRow(
        series_key=_TEST_SERIES_KEY,
        kind="price",
        interval_start_utc=base_ts + timedelta(minutes=15 * step),
        horizon_step=step,
        p10=p10,
        p50=p50,
        p90=p90,
    )


async def test_ts_02_06_upsert_and_fetch_round_trip(pool: AsyncConnectionPool) -> None:
    backend = PgForecastBackend(pool)
    base_ts = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    rows = [_row(step, base_ts, p10=10.0 + step, p50=20.0 + step, p90=30.0 + step) for step in range(96)]

    await backend.upsert_rows(rows)
    fetched = await backend.fetch_range(base_ts, base_ts + timedelta(hours=24))

    assert len(fetched) == 96
    assert {r.horizon_step for r in fetched} == set(range(96))
    for r in fetched:
        assert r.p10 <= r.p50 <= r.p90
        assert r.firm_fitness == "FIRM_OK"


async def test_upsert_overwrites_prior_quantiles_for_same_slot(pool: AsyncConnectionPool) -> None:
    backend = PgForecastBackend(pool)
    base_ts = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)

    await backend.upsert_rows([_row(0, base_ts, p10=1.0, p50=2.0, p90=3.0)])
    await backend.upsert_rows(
        [_row(0, base_ts, p10=1.0, p50=2.0, p90=3.0).model_copy(update={"firm_fitness": "NOT_FOR_FIRM"})]
    )

    fetched = await backend.fetch_range(base_ts, base_ts + timedelta(minutes=15))
    assert len(fetched) == 1  # recompute overwrote in place, not a second row
    assert fetched[0].firm_fitness == "NOT_FOR_FIRM"


async def test_forecast_table_rejects_quantile_disorder(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO og.forecast (series_key, kind, interval_start_utc, horizon_step, p10, p50, p90)
                    VALUES (%s, 'price', now(), 0, 100, 50, 10)
                    """,
                    (_TEST_SERIES_KEY,),
                )
