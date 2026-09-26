"""Postgres-backed `MarketDataPort` (`og.feed_obs` reads only -- `intake` never writes that table,
`opengrid.feeds` owns it)."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from opengrid.contracts.intake.ports import (
    AS_PRICE_PRODUCT,
    ENERGY_PRICE_PRODUCT,
    FEED_SOURCE_ERCOT,
    PriceObservation,
)

_LATEST_OBS_SQL = """
SELECT value, ts FROM og.feed_obs
WHERE source = %(source)s AND product = %(product)s AND series = %(series)s
ORDER BY ts DESC LIMIT 1
"""


class PgMarketDataPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _latest(self, *, product: str, series: str) -> float | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _LATEST_OBS_SQL, {"source": FEED_SOURCE_ERCOT, "product": product, "series": series}
            )
            row = await cur.fetchone()
            return float(row[0]) if row else None

    async def latest_energy_price_usd_per_mwh(self, series_key: str) -> float | None:
        return await self._latest(product=ENERGY_PRICE_PRODUCT, series=series_key)

    async def latest_as_mcpc_usd_per_mwh(self, product_code: str) -> float | None:
        return await self._latest(product=AS_PRICE_PRODUCT, series=product_code)

    async def latest_as_mcpc(self, product_code: str) -> PriceObservation | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _LATEST_OBS_SQL,
                {"source": FEED_SOURCE_ERCOT, "product": AS_PRICE_PRODUCT, "series": product_code},
            )
            row = await cur.fetchone()
            return PriceObservation(float(row[0]), row[1]) if row else None
