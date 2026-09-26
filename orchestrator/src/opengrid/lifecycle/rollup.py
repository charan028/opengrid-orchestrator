"""Incremental 1-min and 15-min per-hub rollups of og.telemetry (the M&V source once raw rows age out).

Watermarks live in `og.lifecycle_watermark` (`telemetry_1m`, `telemetry_15m`): the exclusive end of the
last fully aggregated range. Each run re-aggregates from `watermark - rollup_late_minutes` so late
telemetry (hub backfill, flush lag) is folded in; the UPSERT recomputes whole buckets from source rows,
so re-running any range is idempotent (a bucket is rewritten only when its values changed). Only complete
minutes are aggregated (`hi` <= the start of the current minute), 15-min buckets only once the 1-min
watermark has passed their end. Work per run is capped at `rollup_max_hours_per_run`, in
`rollup_chunk_minutes` transactions, so a long backlog catches up over several runs.

Energy: p_kw > 0 is charging (energy in), p_kw < 0 discharging (energy out) -- settle's convention.
energy_*_kwh = the minute's mean charging/discharging kW x 1/60 h, the same per-hub per-minute averaging
settle meters with; sample_count records the coverage behind each bucket.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle.policy import LifecycleConfig

WM_1M = "telemetry_1m"
WM_15M = "telemetry_15m"
_EPOCH = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
_FIFTEEN = timedelta(minutes=15)

_UPSERT_1M_SQL = """
INSERT INTO og.telemetry_1m AS r (hub_id, bucket, p_kw_avg, p_kw_min, p_kw_max, soc_kwh_last,
                                  energy_in_kwh, energy_out_kwh, sample_count, computed_at)
SELECT hub_id,
       date_trunc('minute', ts) AS bucket,
       avg(p_kw),
       min(p_kw),
       max(p_kw),
       (array_agg(soc_kwh ORDER BY ts DESC) FILTER (WHERE soc_kwh IS NOT NULL))[1],
       coalesce(avg(greatest(p_kw, 0)) FILTER (WHERE p_kw IS NOT NULL), 0) / 60.0,
       coalesce(avg(greatest(-p_kw, 0)) FILTER (WHERE p_kw IS NOT NULL), 0) / 60.0,
       count(*),
       now()
FROM og.telemetry
WHERE ts >= %(lo)s AND ts < %(hi)s
GROUP BY hub_id, date_trunc('minute', ts)
ON CONFLICT (hub_id, bucket) DO UPDATE SET
    p_kw_avg = EXCLUDED.p_kw_avg, p_kw_min = EXCLUDED.p_kw_min, p_kw_max = EXCLUDED.p_kw_max,
    soc_kwh_last = EXCLUDED.soc_kwh_last, energy_in_kwh = EXCLUDED.energy_in_kwh,
    energy_out_kwh = EXCLUDED.energy_out_kwh, sample_count = EXCLUDED.sample_count,
    computed_at = EXCLUDED.computed_at
WHERE (r.sample_count, r.p_kw_avg, r.p_kw_min, r.p_kw_max, r.soc_kwh_last)
      IS DISTINCT FROM (EXCLUDED.sample_count, EXCLUDED.p_kw_avg, EXCLUDED.p_kw_min, EXCLUDED.p_kw_max,
                        EXCLUDED.soc_kwh_last)
"""

_UPSERT_15M_SQL = """
INSERT INTO og.telemetry_15m AS r (hub_id, bucket, p_kw_avg, p_kw_min, p_kw_max, soc_kwh_last,
                                   energy_in_kwh, energy_out_kwh, sample_count, computed_at)
SELECT hub_id,
       date_bin('15 minutes', bucket, %(epoch)s) AS b,
       sum(p_kw_avg * sample_count) FILTER (WHERE p_kw_avg IS NOT NULL)
           / nullif(sum(sample_count) FILTER (WHERE p_kw_avg IS NOT NULL), 0),
       min(p_kw_min),
       max(p_kw_max),
       (array_agg(soc_kwh_last ORDER BY bucket DESC) FILTER (WHERE soc_kwh_last IS NOT NULL))[1],
       sum(energy_in_kwh),
       sum(energy_out_kwh),
       sum(sample_count),
       now()
FROM og.telemetry_1m
WHERE bucket >= %(lo)s AND bucket < %(hi)s
GROUP BY hub_id, date_bin('15 minutes', bucket, %(epoch)s)
ON CONFLICT (hub_id, bucket) DO UPDATE SET
    p_kw_avg = EXCLUDED.p_kw_avg, p_kw_min = EXCLUDED.p_kw_min, p_kw_max = EXCLUDED.p_kw_max,
    soc_kwh_last = EXCLUDED.soc_kwh_last, energy_in_kwh = EXCLUDED.energy_in_kwh,
    energy_out_kwh = EXCLUDED.energy_out_kwh, sample_count = EXCLUDED.sample_count,
    computed_at = EXCLUDED.computed_at
WHERE (r.sample_count, r.p_kw_avg, r.p_kw_min, r.p_kw_max, r.soc_kwh_last, r.energy_in_kwh, r.energy_out_kwh)
      IS DISTINCT FROM (EXCLUDED.sample_count, EXCLUDED.p_kw_avg, EXCLUDED.p_kw_min, EXCLUDED.p_kw_max,
                        EXCLUDED.soc_kwh_last, EXCLUDED.energy_in_kwh, EXCLUDED.energy_out_kwh)
"""

_GET_WM_SQL = "SELECT upto FROM og.lifecycle_watermark WHERE name = %(name)s"
_SET_WM_SQL = """
INSERT INTO og.lifecycle_watermark (name, upto, updated_at) VALUES (%(name)s, %(upto)s, now())
ON CONFLICT (name) DO UPDATE SET upto = EXCLUDED.upto, updated_at = now()
"""


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def floor_15(ts: datetime) -> datetime:
    return _EPOCH + ((ts - _EPOCH) // _FIFTEEN) * _FIFTEEN


def plan_chunks(
    start: datetime, end: datetime, chunk: timedelta, max_span: timedelta
) -> list[tuple[datetime, datetime]]:
    """[start, end) cut into `chunk`-sized ranges, at most `max_span` in total. Pure."""
    stop = min(end, start + max_span)
    out: list[tuple[datetime, datetime]] = []
    lo = start
    while lo < stop:
        hi = min(lo + chunk, stop)
        out.append((lo, hi))
        lo = hi
    return out


async def _watermark(pool: AsyncConnectionPool, name: str) -> datetime | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_GET_WM_SQL, {"name": name})
        row = await cur.fetchone()
    return row[0] if row else None


async def _run_chunks(
    pool: AsyncConnectionPool, name: str, upsert_sql: str, chunks: list[tuple[datetime, datetime]]
) -> int:
    rows = 0
    for lo, hi in chunks:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(upsert_sql, {"lo": lo, "hi": hi, "epoch": _EPOCH})
            rows += max(cur.rowcount, 0)
            await cur.execute(_SET_WM_SQL, {"name": name, "upto": hi})
    return rows


async def run_rollups(pool: AsyncConnectionPool, cfg: LifecycleConfig, now: datetime) -> dict[str, object]:
    """Advance the 1-min rollup to the last complete minute, then the 15-min rollup behind it."""
    chunk = timedelta(minutes=cfg.rollup_chunk_minutes)
    late = timedelta(minutes=cfg.rollup_late_minutes)
    max_span = timedelta(hours=cfg.rollup_max_hours_per_run)

    end_1m = floor_minute(now)
    wm_1m = await _watermark(pool, WM_1M)
    start_1m = (
        floor_minute(now - timedelta(hours=cfg.rollup_backfill_hours)) if wm_1m is None else wm_1m - late
    )
    chunks_1m = plan_chunks(start_1m, end_1m, chunk, max_span + late)
    rows_1m = await _run_chunks(pool, WM_1M, _UPSERT_1M_SQL, chunks_1m)

    wm_1m_after = chunks_1m[-1][1] if chunks_1m else (wm_1m or start_1m)
    end_15m = floor_15(wm_1m_after)
    wm_15m = await _watermark(pool, WM_15M)
    start_15m = floor_15(start_1m) if wm_15m is None else floor_15(wm_15m - late)
    chunks_15m = plan_chunks(start_15m, end_15m, max(chunk, _FIFTEEN) * 4, max_span + late)
    rows_15m = await _run_chunks(pool, WM_15M, _UPSERT_15M_SQL, chunks_15m)
    return {
        "rows_1m": rows_1m,
        "rows_15m": rows_15m,
        "watermark_1m": wm_1m_after.isoformat(),
        "watermark_15m": (chunks_15m[-1][1] if chunks_15m else (wm_15m or start_15m)).isoformat(),
    }
