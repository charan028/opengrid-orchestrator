"""Postgres persistence for `opengrid.pq_ingest` (07-delivery/06 S6.5, over the tables
`orchestrator/migrations/0011_asset_health.sql` already created --
`og.pq_waveform_summary`/`og.pq_waveform_raw_index`, WP-A). Kept separate from
`opengrid.pq_ingest.__init__` so the ingest/validation/aggregation logic has no
`psycopg` import (mirrors `opengrid.fleet.pg_backend`, BUILD.md S5a "pure logic
separated from I/O").
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.pq import PqWaveformRawIndex, PqWaveformSummaryRow

# Waveform summaries are soft telemetry -- replayable from the hub's next publish and cross-checked by
# the S6.5-step-2 audit job against raw captures -- never the ledger/commitment/trace tables that keep
# synchronous commit. Same fix and same reasoning as `opengrid.fleet.pg_backend._ASYNC_COMMIT_SQL`
# (S9 wave-2 build report: the live fleet's WAL fsync took ~0.5s/commit, and `ingest_summary`'s old
# one-insert-per-message pattern hit that wall at ~1,000 msg/s); this package defines its own constant
# (not an import from `opengrid.fleet`, a peer module) per `opengrid.ledger.pg_backend`'s own precedent.
_ASYNC_COMMIT_SQL = "SET LOCAL synchronous_commit TO OFF"

# Matches `opengrid.fleet.pg_backend.upsert_hub_states`'s chunk size -- keeps a single `executemany`
# well under Postgres's per-statement parameter-count ceiling even at 10,000 hubs' worth of buffered rows.
_BATCH_CHUNK_SIZE = 500

_INSERT_SUMMARY_SQL = """
INSERT INTO og.pq_waveform_summary (
    hub_id, ts, v_rms_a, v_rms_b, v_rms_c, i_rms_a, i_rms_b, i_rms_c, freq_hz,
    pf_a, pf_b, pf_c, thd_v_pct_a, thd_v_pct_b, thd_v_pct_c, thd_i_pct_a, thd_i_pct_b, thd_i_pct_c,
    phase_angle_deg_a, phase_angle_deg_b, phase_angle_deg_c, harmonics_v, harmonics_i,
    sync_source, sync_quality_ns
) VALUES (
    %(hub_id)s, %(ts)s, %(v_rms_a)s, %(v_rms_b)s, %(v_rms_c)s, %(i_rms_a)s, %(i_rms_b)s, %(i_rms_c)s,
    %(freq_hz)s, %(pf_a)s, %(pf_b)s, %(pf_c)s, %(thd_v_pct_a)s, %(thd_v_pct_b)s, %(thd_v_pct_c)s,
    %(thd_i_pct_a)s, %(thd_i_pct_b)s, %(thd_i_pct_c)s, %(phase_angle_deg_a)s, %(phase_angle_deg_b)s,
    %(phase_angle_deg_c)s, %(harmonics_v)s, %(harmonics_i)s, %(sync_source)s, %(sync_quality_ns)s
)
ON CONFLICT (hub_id, ts) DO NOTHING
"""

_INSERT_RAW_INDEX_SQL = """
INSERT INTO og.pq_waveform_raw_index (
    capture_id, hub_id, ts, trigger_reason, blob_ref, channels, sample_rate_hz, cycles, retain_until
) VALUES (
    %(capture_id)s, %(hub_id)s, %(ts)s, %(trigger_reason)s, %(blob_ref)s, %(channels)s,
    %(sample_rate_hz)s, %(cycles)s, %(retain_until)s
)
"""

_SUMMARY_COLUMNS = (
    "hub_id",
    "ts",
    "v_rms_a",
    "v_rms_b",
    "v_rms_c",
    "i_rms_a",
    "i_rms_b",
    "i_rms_c",
    "freq_hz",
    "pf_a",
    "pf_b",
    "pf_c",
    "thd_v_pct_a",
    "thd_v_pct_b",
    "thd_v_pct_c",
    "thd_i_pct_a",
    "thd_i_pct_b",
    "thd_i_pct_c",
    "phase_angle_deg_a",
    "phase_angle_deg_b",
    "phase_angle_deg_c",
    "harmonics_v",
    "harmonics_i",
    "sync_source",
    "sync_quality_ns",
)

_LATEST_SUMMARIES_SQL = sql.SQL(
    """
    SELECT {columns}
    FROM og.pq_waveform_summary
    WHERE hub_id = ANY(%(hub_ids)s) AND ts >= %(since)s
    ORDER BY hub_id, ts DESC
    """
).format(columns=sql.SQL(", ").join(sql.Identifier(c) for c in _SUMMARY_COLUMNS))


class PgPqIngestBackend:
    """`opengrid.pq_ingest.PqIngestBackend` over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def insert_summary(self, row: PqWaveformSummaryRow) -> None:
        params = self._params(row)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_INSERT_SUMMARY_SQL, params)
            await conn.commit()

    async def insert_summaries_batch(self, rows: Sequence[PqWaveformSummaryRow]) -> None:
        """S9 wave-2 fix: one `executemany` (chunked at `_BATCH_CHUNK_SIZE`) and one commit for every
        buffered summary, instead of `insert_summary`'s one-round-trip-per-message pattern that stalled
        the live fleet at ~1,000 msg/s (WAL fsync ~0.5s/commit -- see `_ASYNC_COMMIT_SQL`'s own comment).
        Async commit: this is soft telemetry (replayable, cross-checked by the S6.5 audit job), never the
        ledger/commitment/trace tables that keep synchronous commit."""
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            for start in range(0, len(rows), _BATCH_CHUNK_SIZE):
                chunk = rows[start : start + _BATCH_CHUNK_SIZE]
                await cur.executemany(_INSERT_SUMMARY_SQL, [self._params(row) for row in chunk])
            await conn.commit()

    @staticmethod
    def _params(row: PqWaveformSummaryRow) -> dict[str, object]:
        params = row.model_dump(mode="json")
        params["harmonics_v"] = Jsonb(params["harmonics_v"]) if row.harmonics_v is not None else None
        params["harmonics_i"] = Jsonb(params["harmonics_i"]) if row.harmonics_i is not None else None
        return params

    async def insert_raw_index(self, row: PqWaveformRawIndex) -> None:
        params = row.model_dump(mode="json")
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_INSERT_RAW_INDEX_SQL, params)
            await conn.commit()

    async def latest_summaries(
        self, hub_ids: Sequence[str], *, since: datetime
    ) -> list[PqWaveformSummaryRow]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LATEST_SUMMARIES_SQL, {"hub_ids": list(hub_ids), "since": since})
            rows = await cur.fetchall()
        return [PqWaveformSummaryRow(**dict(zip(_SUMMARY_COLUMNS, row, strict=True))) for row in rows]
