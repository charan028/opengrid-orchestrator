"""Postgres-backed `FleetBackend` (02b S4.2). Kept separate from `opengrid.fleet.__init__` so the
twin's classification/aggregation logic has no `psycopg` import and stays unit-testable without a
database (mirrors `opengrid.trace.pg_backend`, BUILD.md S5a "pure logic separated from I/O").
"""

from __future__ import annotations

from typing import Any

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.mqtt import ScadaBankSignal
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.fleet import TelemetryRow

_INSERT_SCADA_FEED_OBS_SQL = """
INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
VALUES ('scada', %(bank_id)s, %(series)s, %(ts)s, %(value)s, %(unit)s, %(quality)s)
ON CONFLICT (source, product, series, ts) DO NOTHING
"""

_SCADA_QUALITY_TO_FEED_OBS: dict[str, str] = {
    "good": "GOOD",
    "stale": "STALE",
    "missing": "STALE",
    "out_of_range": "ESTIMATED",
    "comm_fail": "STALE",
}

_LOAD_HUBS_SQL = "SELECT hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d, lat, lon FROM og.hub"
_LOAD_BANKS_SQL = "SELECT bank_id, zone, kva_rating, reserve_kva, feeder_id FROM og.bank"
_LOAD_HUB_STATES_SQL = """
SELECT hub_id, soc_kwh, p_kw, health, lease_epoch, lease_expires_at, last_command_id, last_seen_at,
       fault_code
FROM og.hub_state
"""
_UPSERT_HUB_STATE_COLUMNS = (
    "hub_id",
    "soc_kwh",
    "p_kw",
    "health",
    "lease_epoch",
    "lease_expires_at",
    "last_command_id",
    "last_seen_at",
    "fault_code",
)

_UPSERT_HUB_STATE_CONFLICT_SET = sql.SQL(
    """
    ON CONFLICT (hub_id) DO UPDATE SET
        soc_kwh = EXCLUDED.soc_kwh,
        p_kw = EXCLUDED.p_kw,
        health = EXCLUDED.health,
        lease_epoch = EXCLUDED.lease_epoch,
        lease_expires_at = EXCLUDED.lease_expires_at,
        last_command_id = EXCLUDED.last_command_id,
        last_seen_at = EXCLUDED.last_seen_at,
        fault_code = EXCLUDED.fault_code
    """
)
_COPY_TELEMETRY_SQL = "COPY og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health) FROM STDIN"


class PgFleetBackend:
    """`FleetBackend` implementation over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load_hubs(self) -> list[Hub]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LOAD_HUBS_SQL)
            rows = await cur.fetchall()
        columns = ("hub_id", "bank_id", "zone", "e_kwh", "r_kwh", "p_kw", "eta_c", "eta_d", "lat", "lon")
        return [Hub(**dict(zip(columns, row, strict=True))) for row in rows]

    async def load_banks(self) -> list[Bank]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LOAD_BANKS_SQL)
            rows = await cur.fetchall()
        columns = ("bank_id", "zone", "kva_rating", "reserve_kva", "feeder_id")
        return [Bank(**dict(zip(columns, row, strict=True))) for row in rows]

    async def load_hub_states(self) -> list[HubState]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LOAD_HUB_STATES_SQL)
            rows = await cur.fetchall()
        columns = (
            "hub_id",
            "soc_kwh",
            "p_kw",
            "health",
            "lease_epoch",
            "lease_expires_at",
            "last_command_id",
            "last_seen_at",
            "fault_code",
        )
        return [HubState(**dict(zip(columns, row, strict=True))) for row in rows]

    async def upsert_hub_states(self, states: list[HubState]) -> None:
        """Single multi-row `INSERT ... VALUES (...), (...), ... ON CONFLICT`, not one round trip per
        hub (merge task, dispatch-live pass: `og-engine`'s 2 s tick calls this for up to ~2,000 hubs
        every cycle -- one-row-at-a-time `executemany()` here made the flush cycle take far longer than
        `[allocator].cycle_interval_s`, so every hub's `last_seen_at` was already older than
        `health.hub_stale_s` by the time the *next* flush classified it, leaving every hub permanently
        "stale" no matter how fresh its telemetry actually was). Chunked at 500 rows/statement so this
        still works if `hub_count` grows well past MVP-S's 2,000 (`02b S4.1`'s 10,000-hub load-test
        note) without hitting Postgres's parameter-count ceiling.
        """
        if not states:
            return
        chunk_size = 500
        columns_sql = sql.SQL(", ").join(sql.Identifier(c) for c in _UPSERT_HUB_STATE_COLUMNS)
        row_placeholder = sql.SQL("({})").format(
            sql.SQL(", ").join([sql.Placeholder()] * len(_UPSERT_HUB_STATE_COLUMNS))
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            for start in range(0, len(states), chunk_size):
                chunk = states[start : start + chunk_size]
                values_sql = sql.SQL(", ").join([row_placeholder] * len(chunk))
                statement = sql.SQL(
                    "INSERT INTO og.hub_state ({columns}) VALUES {values} {on_conflict}"
                ).format(columns=columns_sql, values=values_sql, on_conflict=_UPSERT_HUB_STATE_CONFLICT_SET)
                params: list[Any] = []
                for s in chunk:
                    params.extend(
                        (
                            s.hub_id,
                            s.soc_kwh,
                            s.p_kw,
                            s.health,
                            s.lease_epoch,
                            s.lease_expires_at,
                            s.last_command_id,
                            s.last_seen_at,
                            s.fault_code,
                        )
                    )
                await cur.execute(statement, params)

    async def copy_telemetry(self, rows: list[TelemetryRow]) -> None:
        if not rows:
            return
        async with (
            self._pool.connection() as conn,
            conn.cursor() as cur,
            cur.copy(_COPY_TELEMETRY_SQL) as copy,
        ):
            for row in rows:
                await copy.write_row(
                    (row.hub_id, row.ts, row.soc_kwh, row.p_kw, row.seq, row.epoch, row.health)
                )

    async def record_scada_observation(self, signal: ScadaBankSignal) -> None:
        quality = _SCADA_QUALITY_TO_FEED_OBS.get(signal.quality, "ESTIMATED")
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_SCADA_FEED_OBS_SQL,
                {
                    "bank_id": signal.bank_id,
                    "series": signal.signal,
                    "ts": signal.ts,
                    "value": signal.value,
                    "unit": signal.unit,
                    "quality": quality,
                },
            )
            await conn.commit()
