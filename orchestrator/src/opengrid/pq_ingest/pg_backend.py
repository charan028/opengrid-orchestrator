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
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.pq import PqWaveformRawIndex, PqWaveformSummaryRow
from opengrid.pq_ingest.characterize import (
    NOMINAL_FREQ_HZ,
    NOMINAL_VOLTAGE_V,
    HubCharacterization,
    HubSummaryAggregate,
    _phase_connection_from_presence,
)

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

_UPSERT_HUB_INVERTER_PQ_SQL = """
INSERT INTO og.hub_inverter_pq (
    hub_id, phase_connection, kva_rating, freq_offset_hz, freq_offset_std_hz,
    voltage_offset_pct, voltage_offset_std_pct, thd_current_pct, dominant_harmonics,
    phase_angle_error_deg, quality_score, last_estimated_at
) VALUES (
    %(hub_id)s, %(phase_connection)s, %(kva_rating)s, %(freq_offset_hz)s, %(freq_offset_std_hz)s,
    %(voltage_offset_pct)s, %(voltage_offset_std_pct)s, %(thd_current_pct)s, %(dominant_harmonics)s,
    %(phase_angle_error_deg)s, %(quality_score)s, %(last_estimated_at)s
)
ON CONFLICT (hub_id) DO UPDATE SET
    freq_offset_hz = EXCLUDED.freq_offset_hz,
    freq_offset_std_hz = EXCLUDED.freq_offset_std_hz,
    voltage_offset_pct = EXCLUDED.voltage_offset_pct,
    voltage_offset_std_pct = EXCLUDED.voltage_offset_std_pct,
    thd_current_pct = EXCLUDED.thd_current_pct,
    dominant_harmonics = COALESCE(EXCLUDED.dominant_harmonics, og.hub_inverter_pq.dominant_harmonics),
    phase_angle_error_deg = EXCLUDED.phase_angle_error_deg,
    quality_score = EXCLUDED.quality_score,
    last_estimated_at = EXCLUDED.last_estimated_at
"""
# `kva_rating`/`phase_connection` on a first INSERT only; `pf_min_leading`/`pf_min_lagging`/
# `response_time_ms`/`ride_through_class`/`asset_state*` are omitted from BOTH the column list
# and the UPDATE SET -- the table's own DEFAULTs apply on first insert (0011_asset_health.sql),
# and a repeat characterization pass never clobbers whatever a later, better-informed writer of
# those nameplate/asset-health fields has set (see characterize.py's own docstring).

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

# R3.4.1 PROD-IO fix: computes exactly what `characterize.characterize_hub`'s Python loop used to
# derive from ~101,761 raw sample rows (15 min window x ~3,509 hubs), but IN SQL, returning one row per
# hub (~3,509) instead of one row per sample. Every CTE here mirrors one piece of
# `characterize._aggregate_hub` (the Python reference this must match, proven by
# `tests/unit/pq_ingest/test_characterize.py`'s before/after equality test):
#   - freq_agg:        AVG/STDDEV_POP(freq_hz)                     <-> freq_offset_hz/freq_offset_std_hz
#   - voltage_flat/agg: unpivot v_rms_a/b/c, AVG/STDDEV_POP         <-> voltage_offset_pct/_std_pct
#     (the (v - %(nominal_voltage_v)s) / %(nominal_voltage_v)s * 100 transform is affine, so
#     STDDEV_POP of the transformed value equals the Python side's pstdev of the same transform)
#   - thd_flat/agg, angle_flat/agg: unpivot + AVG                   <-> thd_current_pct/phase_angle_error_deg
#   - phase_presence:  bool_or(v_rms_<phase> IS NOT NULL)           <-> `_infer_phase_connection`'s
#     "which v_rms_<phase> was EVER populated" set, via `characterize._phase_connection_from_presence`
#   - latest_harmonics: DISTINCT ON (hub_id) ... ORDER BY ts DESC   <-> `_dominant_harmonics`'s "most
#     recent non-null harmonics_i"
# freq_agg/voltage_agg are INNER JOINed (both required -- matches `_aggregate_hub`'s
# "if not freq_values or not voltage_devs: return None"); thd/angle/harmonics are LEFT JOINed with the
# same defaults Python uses when that list is empty (0.0 / None).
_LATEST_SUMMARY_AGGREGATES_SQL = """
WITH window_rows AS (
    SELECT hub_id, ts, freq_hz, v_rms_a, v_rms_b, v_rms_c,
           thd_i_pct_a, thd_i_pct_b, thd_i_pct_c,
           phase_angle_deg_a, phase_angle_deg_b, phase_angle_deg_c,
           harmonics_i
    FROM og.pq_waveform_summary
    WHERE hub_id = ANY(%(hub_ids)s) AND ts >= %(since)s
),
freq_agg AS (
    SELECT hub_id, AVG(freq_hz) AS freq_mean, STDDEV_POP(freq_hz) AS freq_std
    FROM window_rows WHERE freq_hz IS NOT NULL GROUP BY hub_id
),
voltage_flat AS (
    SELECT hub_id, (v - %(nominal_voltage_v)s) / %(nominal_voltage_v)s * 100.0 AS voltage_dev
    FROM window_rows, LATERAL (VALUES (v_rms_a), (v_rms_b), (v_rms_c)) AS phase_v(v)
    WHERE v IS NOT NULL
),
voltage_agg AS (
    SELECT hub_id, AVG(voltage_dev) AS voltage_mean, STDDEV_POP(voltage_dev) AS voltage_std
    FROM voltage_flat GROUP BY hub_id
),
thd_flat AS (
    SELECT hub_id, v AS thd
    FROM window_rows, LATERAL (VALUES (thd_i_pct_a), (thd_i_pct_b), (thd_i_pct_c)) AS phase_thd(v)
    WHERE v IS NOT NULL
),
thd_agg AS (
    SELECT hub_id, AVG(thd) AS thd_mean FROM thd_flat GROUP BY hub_id
),
angle_flat AS (
    SELECT hub_id, v AS angle
    FROM window_rows, LATERAL (VALUES (phase_angle_deg_a), (phase_angle_deg_b), (phase_angle_deg_c))
        AS phase_angle(v)
    WHERE v IS NOT NULL
),
angle_agg AS (
    SELECT hub_id, AVG(angle) AS angle_mean FROM angle_flat GROUP BY hub_id
),
phase_presence AS (
    SELECT hub_id,
           bool_or(v_rms_a IS NOT NULL) AS has_a,
           bool_or(v_rms_b IS NOT NULL) AS has_b,
           bool_or(v_rms_c IS NOT NULL) AS has_c
    FROM window_rows GROUP BY hub_id
),
latest_harmonics AS (
    SELECT DISTINCT ON (hub_id) hub_id, harmonics_i
    FROM window_rows
    WHERE harmonics_i IS NOT NULL
    ORDER BY hub_id, ts DESC
)
SELECT
    f.hub_id, f.freq_mean, f.freq_std, v.voltage_mean, v.voltage_std,
    COALESCE(t.thd_mean, 0.0) AS thd_mean, COALESCE(a.angle_mean, 0.0) AS angle_mean,
    p.has_a, p.has_b, p.has_c, lh.harmonics_i
FROM freq_agg f
JOIN voltage_agg v USING (hub_id)
JOIN phase_presence p USING (hub_id)
LEFT JOIN thd_agg t USING (hub_id)
LEFT JOIN angle_agg a USING (hub_id)
LEFT JOIN latest_harmonics lh USING (hub_id)
"""


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

    async def upsert_hub_inverter_pq_batch(self, rows: Sequence[HubCharacterization]) -> None:
        """S3.1/S5.1/S6.5, blocker fix: one `executemany` + one async commit for every hub
        characterized this pass -- never a per-hub UPDATE. Async commit for the same reason as
        `insert_summaries_batch`: `og.hub_inverter_pq` is "refreshed from a periodic sim/
        estimation job... never billed directly" (the migration's own comment)."""
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            for start in range(0, len(rows), _BATCH_CHUNK_SIZE):
                chunk = rows[start : start + _BATCH_CHUNK_SIZE]
                await cur.executemany(
                    _UPSERT_HUB_INVERTER_PQ_SQL, [self._characterization_params(row) for row in chunk]
                )
            await conn.commit()

    @staticmethod
    def _characterization_params(row: HubCharacterization) -> dict[str, object]:
        return {
            "hub_id": row.hub_id,
            "phase_connection": row.phase_connection,
            "kva_rating": row.kva_rating,
            "freq_offset_hz": row.freq_offset_hz,
            "freq_offset_std_hz": row.freq_offset_std_hz,
            "voltage_offset_pct": row.voltage_offset_pct,
            "voltage_offset_std_pct": row.voltage_offset_std_pct,
            "thd_current_pct": row.thd_current_pct,
            "dominant_harmonics": Jsonb(row.dominant_harmonics) if row.dominant_harmonics else None,
            "phase_angle_error_deg": row.phase_angle_error_deg,
            "quality_score": row.quality_score,
            "last_estimated_at": row.last_estimated_at,
        }

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

    async def latest_summary_aggregates(
        self, hub_ids: Sequence[str], *, since: datetime
    ) -> list[HubSummaryAggregate]:
        """R3.4.1 PROD-IO fix: `run_characterization_pass`'s production read -- the SAME aggregation
        `latest_summaries` + `characterize.characterize_fleet` used to do in Python over ~101,761 raw
        rows, computed in SQL instead (`_LATEST_SUMMARY_AGGREGATES_SQL`'s own comment maps each CTE to
        the Python reference it must match), returning ~1 row/hub."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _LATEST_SUMMARY_AGGREGATES_SQL,
                {"hub_ids": list(hub_ids), "since": since, "nominal_voltage_v": NOMINAL_VOLTAGE_V},
            )
            rows = await cur.fetchall()
        return [self._aggregate_from_row(row) for row in rows]

    @staticmethod
    def _aggregate_from_row(row: Sequence[Any]) -> HubSummaryAggregate:
        from opengrid.core.models.pq import HarmonicComponent

        (
            hub_id,
            freq_mean,
            freq_std,
            voltage_mean,
            voltage_std,
            thd_mean,
            angle_mean,
            has_a,
            has_b,
            has_c,
            harmonics_i,
        ) = row
        return HubSummaryAggregate(
            hub_id=str(hub_id),
            freq_offset_hz=float(freq_mean) - NOMINAL_FREQ_HZ,
            freq_offset_std_hz=float(freq_std) if freq_std is not None else 0.0,
            voltage_offset_pct=abs(float(voltage_mean)),
            voltage_offset_std_pct=float(voltage_std) if voltage_std is not None else 0.0,
            thd_current_pct=float(thd_mean),
            phase_angle_error_deg=float(angle_mean),
            phase_connection=_phase_connection_from_presence(bool(has_a), bool(has_b), bool(has_c)),
            dominant_harmonics=(
                {order: HarmonicComponent(**comp) for order, comp in harmonics_i.items()}
                if harmonics_i is not None
                else None
            ),
        )
