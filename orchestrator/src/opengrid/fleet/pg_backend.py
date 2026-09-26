"""Postgres-backed `FleetBackend` (02b S4.2). Kept separate from `opengrid.fleet.__init__` so the
twin's classification/aggregation logic has no `psycopg` import and stays unit-testable without a
database (mirrors `opengrid.trace.pg_backend`, BUILD.md S5a "pure logic separated from I/O").
"""

from __future__ import annotations

from typing import Any

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.mqtt import FLOW_TELEMETRY_FIELDS, Ack, ScadaBankSignal
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.fleet import TelemetryRow

_INSERT_SCADA_FEED_OBS_SQL = """
INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
VALUES ('scada', %(bank_id)s, %(series)s, %(ts)s, %(value)s, %(unit)s, %(quality)s)
ON CONFLICT (source, product, series, ts) DO NOTHING
"""

_INSERT_ACK_SQL = """
INSERT INTO og.command_ack (batch_id, hub_id, accepted, applied_p_kw, reject_reason, ts)
VALUES (%(batch_id)s, %(hub_id)s, %(accepted)s, %(applied_p_kw)s, %(reject_reason)s, %(ts)s)
ON CONFLICT (batch_id, hub_id) DO NOTHING
"""

_SCADA_QUALITY_TO_FEED_OBS: dict[str, str] = {
    "good": "GOOD",
    "stale": "STALE",
    "missing": "STALE",
    "out_of_range": "ESTIMATED",
    "comm_fail": "STALE",
}

# utility_scale mirrors guardian.repo._ALL_HUB_PARAMS_SQL: the hub's bank is an og.asset SUBSTATION.
_LOAD_HUBS_SQL = """
SELECT h.hub_id, h.bank_id, h.zone, h.e_kwh, h.r_kwh, h.p_kw, h.eta_c, h.eta_d, h.lat, h.lon, h.units,
       EXISTS (SELECT 1 FROM og.asset a WHERE a.bank_id = h.bank_id AND a.asset_class = 'SUBSTATION')
FROM og.hub h
"""
_LOAD_HUBS_COLUMNS = (
    "hub_id",
    "bank_id",
    "zone",
    "e_kwh",
    "r_kwh",
    "p_kw",
    "eta_c",
    "eta_d",
    "lat",
    "lon",
    "units",
    "utility_scale",
)
_LOAD_BANKS_SQL = "SELECT bank_id, zone, kva_rating, reserve_kva, feeder_id FROM og.bank"
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
    *FLOW_TELEMETRY_FIELDS,
)
# Every optional telemetry column (FLOW_TELEMETRY_FIELDS: flow 0027, charge source 0034) comes from that one
# tuple, so the SELECT, the upsert and the COPY can never disagree on the column list.
_LOAD_HUB_STATES_SQL = sql.SQL("SELECT {} FROM og.hub_state").format(
    sql.SQL(", ").join(sql.Identifier(column) for column in _UPSERT_HUB_STATE_COLUMNS)
)

_UPSERT_HUB_STATE_CONFLICT_SET = sql.SQL("ON CONFLICT (hub_id) DO UPDATE SET {}").format(
    sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
        for column in _UPSERT_HUB_STATE_COLUMNS[1:]  # every column but the conflict key
    )
)
_COPY_TELEMETRY_SQL = sql.SQL("COPY og.telemetry ({}) FROM STDIN").format(
    sql.SQL(", ").join(
        sql.Identifier(column)
        for column in ("hub_id", "ts", "soc_kwh", "p_kw", "seq", "epoch", "health", *FLOW_TELEMETRY_FIELDS)
    )
)

# Telemetry, hub_state and SCADA readings are soft state re-sent every 2 s: their transactions commit
# asynchronously (WAL still written, just not fsync-waited). On the base server a WAL fsync took ~0.5 s
# (live 2026-09-26: 4 commits/s, disk 100% busy), so waiting on it in every 2 s flush stalled the engine
# tick for seconds and aged every hub to stale. Ledger/commitment/trace writes keep synchronous commit.
_ASYNC_COMMIT_SQL = "SET LOCAL synchronous_commit TO OFF"


class PgFleetBackend:
    """`FleetBackend` implementation over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load_hubs(self) -> list[Hub]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LOAD_HUBS_SQL)
            rows = await cur.fetchall()
        return [Hub(**dict(zip(_LOAD_HUBS_COLUMNS, row, strict=True))) for row in rows]

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
        return [HubState(**dict(zip(_UPSERT_HUB_STATE_COLUMNS, row, strict=True))) for row in rows]

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
            await cur.execute(_ASYNC_COMMIT_SQL)
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
                            *(getattr(s, name) for name in FLOW_TELEMETRY_FIELDS),
                        )
                    )
                await cur.execute(statement, params)

    async def copy_telemetry(self, rows: list[TelemetryRow]) -> None:
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            async with cur.copy(_COPY_TELEMETRY_SQL) as copy:
                for row in rows:
                    await copy.write_row(
                        (row.hub_id, row.ts, row.soc_kwh, row.p_kw, row.seq, row.epoch, row.health, *row.flow)
                    )

    async def insert_acks(self, acks: list[Ack]) -> None:
        """One statement batch, one asynchronous commit (acks are an audit record of hub behaviour,
        re-derivable from the hubs' own state; see `_ASYNC_COMMIT_SQL`)."""
        if not acks:
            return
        rows = [
            {
                "batch_id": a.batch_id,
                "hub_id": a.hub_id,
                "accepted": a.accepted,
                "applied_p_kw": a.applied_p_kw,
                "reject_reason": a.reject_reason,
                "ts": a.ts,
            }
            for a in acks
        ]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            await cur.executemany(_INSERT_ACK_SQL, rows)
            await conn.commit()

    async def record_scada_observations(self, signals: list[ScadaBankSignal]) -> None:
        """All buffered readings in one statement batch and one commit (called from `fleet.flush`)."""
        if not signals:
            return
        rows = [
            {
                "bank_id": s.bank_id,
                "series": s.signal,
                "ts": s.ts,
                "value": s.value,
                "unit": s.unit,
                "quality": _SCADA_QUALITY_TO_FEED_OBS.get(s.quality, "ESTIMATED"),
            }
            for s in signals
        ]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            await cur.executemany(_INSERT_SCADA_FEED_OBS_SQL, rows)
            await conn.commit()
