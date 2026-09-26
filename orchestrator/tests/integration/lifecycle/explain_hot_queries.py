"""EXPLAIN evidence for the hot queries at a realistic synthetic volume (not a pytest module).

Workspace database only -- refuses to run unless OG_DB starts with `og_t_`:
    powershell -File tools\\remote.ps1 -Ws dlc -Cmd "cd orchestrator && python tests/integration/lifecycle/explain_hot_queries.py"

DO NOT seed large volumes on the base host (it shares the production disk). Seeds 2,000 hubs in 40 banks /
4 zones and OG_DLC_SEED_HOURS (default 0.05, capped at 0.07 = ~50k rows) of telemetry at 10 s, grants at the 2 s allocator cadence (one obligation per bank) and
2 s SCADA feed_obs for every bank, then prints EXPLAIN (ANALYZE, BUFFERS) for the fleet hub_state upsert,
the guardian reads, settle's metering by ts range, the fleet map and the lifecycle's own queries, plus
bytes/row and the archive compression ratio used in 15-data-lifecycle.md's capacity math.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from opengrid.lifecycle import archive, partitions, rollup
from opengrid.lifecycle.policy import LifecycleConfig
from opengrid.platform.config import Config
from opengrid.platform.db import build_dsn, migrate_sync

MAX_SEED_HOURS = 0.07  # 2,000 hubs x 25 samples = 50k telemetry rows; never more on the shared base host
CONTRACT = "00000000-0000-7000-8000-000000000d02"
OPPORTUNITY = "00000000-0000-7000-8000-0000000dc000"

SEED_SQL = [
    (
        "hubs",
        """
INSERT INTO og.bank (bank_id, zone, kva_rating)
SELECT 'xb-' || lpad(b::text, 3, '0'), (ARRAY['LZ_NORTH','LZ_SOUTH','LZ_HOUSTON','LZ_WEST'])[1 + mod(b, 4)], 600
FROM generate_series(0, 39) b ON CONFLICT DO NOTHING;
INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw)
SELECT 'xh-' || lpad(h::text, 5, '0'), 'xb-' || lpad(mod(h, 40)::text, 3, '0'),
       (ARRAY['LZ_NORTH','LZ_SOUTH','LZ_HOUSTON','LZ_WEST'])[1 + mod(mod(h, 40), 4)], 39.2, 7.8, 11
FROM generate_series(0, 1999) h ON CONFLICT DO NOTHING;
INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, last_seen_at)
SELECT hub_id, 20, 0, now() FROM og.hub WHERE hub_id LIKE 'xh-%' ON CONFLICT DO NOTHING;
""",
    ),
    (
        "obligations",
        """
INSERT INTO og.opportunity (opportunity_id, contract_id, window_start, window_end, requested_kw)
VALUES (%(opp)s, %(contract)s, now() - interval '2 days', now() + interval '1 day', 400) ON CONFLICT DO NOTHING;
INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
                           window_end, committed_qty_kw, state)
SELECT ('00000000-0000-7000-8000-0000000dc' || lpad(b::text, 3, '0'))::uuid, %(opp)s, %(contract)s,
       'ERCOT_ENERGY', 'T2', now() - interval '2 days', now() + interval '1 day', 100, 'DELIVERING'
FROM generate_series(0, 39) b ON CONFLICT DO NOTHING;
""",
    ),
    (
        "telemetry",
        """
INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health, home_load_kw, pv_kw, meter_kw)
SELECT 'xh-' || lpad(h::text, 5, '0'), g, 20 + mod(h, 7), -5 + mod(h, 11), 0, 0, 'online', 1.5, 0.8, 0.7
FROM generate_series(%(lo)s::timestamptz, %(hi)s::timestamptz - interval '10 seconds', interval '10 seconds') g,
     generate_series(0, 1999) h
""",
    ),
    (
        "grants",
        """
INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version, created_at)
SELECT gen_random_uuid(), extract(epoch FROM g)::bigint || '-1',
       ('00000000-0000-7000-8000-0000000dc' || lpad(b::text, 3, '0'))::uuid,
       'xb-' || lpad(b::text, 3, '0'), 50, 1, g
FROM generate_series(%(lo)s::timestamptz, %(hi)s::timestamptz - interval '2 seconds', interval '2 seconds') g,
     generate_series(0, 39) b
""",
    ),
    (
        "scada",
        """
INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
SELECT 'scada', 'xb-' || lpad(b::text, 3, '0'), s, g, 250, 'kVA', 'GOOD'
FROM generate_series(%(lo)s::timestamptz, %(hi)s::timestamptz - interval '2 seconds', interval '2 seconds') g,
     generate_series(0, 39) b, unnest(ARRAY['APPARENT_POWER_KVA', 'REAL_POWER_KW']) s
ON CONFLICT DO NOTHING
""",
    ),
]

UPSERT_HUB_STATE = """
INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, lease_epoch, last_seen_at)
SELECT hub_id, 21, 1, 'online', 0, now() FROM og.hub WHERE hub_id LIKE 'xh-%' ORDER BY hub_id LIMIT 500
ON CONFLICT (hub_id) DO UPDATE SET soc_kwh = EXCLUDED.soc_kwh, p_kw = EXCLUDED.p_kw,
    health = EXCLUDED.health, last_seen_at = EXCLUDED.last_seen_at
"""

QUERIES: list[tuple[str, str]] = [
    ("fleet: hub_state upsert (500-row chunk)", UPSERT_HUB_STATE),
    (
        "guardian G-03: latest GOOD bank SCADA reading",
        "SELECT value, extract(epoch FROM now() - ts) FROM og.feed_obs WHERE source = 'scada' "
        "AND product = 'xb-007' AND series = 'APPARENT_POWER_KVA' AND quality = 'GOOD' ORDER BY ts DESC LIMIT 1",
    ),
    (
        "guardian: prior grant of an obligation (ORDER BY created_at DESC LIMIT 1)",
        "SELECT granted_kw FROM og.grant WHERE obligation_id = '00000000-0000-7000-8000-0000000dc007' "
        "ORDER BY created_at DESC LIMIT 1",
    ),
    (
        "settle: bank telemetry per minute over a 15-min interval (tel CTE)",
        "SELECT date_trunc('minute', t.ts) AS m, h.bank_id, t.hub_id, avg(t.p_kw) FROM og.telemetry t "
        "JOIN og.hub h USING (hub_id) WHERE h.bank_id IN ('xb-007') AND t.ts >= now() - interval '2 hours' "
        "AND t.ts < now() - interval '105 minutes' GROUP BY 1, 2, 3",
    ),
    (
        "settle: grant share by bank over a 15-min interval (cyc CTE)",
        "SELECT date_trunc('minute', created_at), cycle_id, bank_id, sum(granted_kw) FROM og.grant "
        "WHERE bank_id IN ('xb-007') AND created_at >= now() - interval '2 hours' "
        "AND created_at < now() - interval '105 minutes' GROUP BY 1, 2, 3",
    ),
    (
        "settle: zone charge energy over 24 h",
        "SELECT coalesce(sum(t.p_kw), 0) FROM og.telemetry t JOIN og.hub h USING (hub_id) "
        "WHERE h.zone = 'LZ_WEST' AND t.ts >= now() - interval '24 hours' AND t.ts < now() AND t.p_kw > 0",
    ),
    (
        "api: hub sparkline (60 min)",
        "SELECT ts, soc_kwh, p_kw FROM og.telemetry WHERE hub_id = 'xh-01234' "
        "AND ts >= now() - interval '60 minutes' ORDER BY ts",
    ),
    (
        "api: fleet map (views_ext._FLEET_MAP_SQL, grant/hub part)",
        "WITH latest_cycle AS (SELECT max(cycle_id) AS cycle_id FROM og.grant), "
        "bank_grant AS (SELECT g.bank_id, sum(g.granted_kw) AS kw FROM og.grant g JOIN latest_cycle l "
        "ON g.cycle_id = l.cycle_id GROUP BY g.bank_id) "
        "SELECT h.hub_id, s.soc_kwh, coalesce(bg.kw, 0) FROM og.hub h LEFT JOIN og.hub_state s USING (hub_id) "
        "LEFT JOIN bank_grant bg ON bg.bank_id = h.bank_id ORDER BY h.hub_id",
    ),
    (
        "lifecycle: DELETE-mode oldest row before cutoff (feed_obs, BRIN)",
        "SELECT min(ts) FROM og.feed_obs t WHERE t.ts < now() - interval '20 hours'",
    ),
]


def _cfg() -> Config:
    db = os.environ.get("OG_DB", "")
    if not db.startswith("og_t_"):
        raise SystemExit("refusing: OG_DB must be a workspace database (og_t_*)")
    return Config({"postgres": {"host": "127.0.0.1", "port": 5432, "database": db}})


def _explain(conn: psycopg.Connection[Any], title: str, query: str) -> None:
    with conn.transaction():
        rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + query).fetchall()  # type: ignore[arg-type]
        print(f"\n### {title}")
        for (line,) in rows:
            print("   ", line)
        raise psycopg.Rollback  # EXPLAIN ANALYZE of the upsert must not persist


def _cleanup(cold: Path) -> None:
    for p in cold.rglob("*"):
        if p.is_file():
            p.chmod(0o640)
            p.unlink()


async def _measure(dsn: str, lo: datetime) -> None:
    pool = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False)
    await pool.open(wait=True, timeout=10)
    try:
        cold = Path(tempfile.mkdtemp(prefix="og_dlc_cold_"))
        lcfg = LifecycleConfig(cold_root=cold, cold_min_free_gb=0.5, rollup_backfill_hours=1)
        t0 = time.monotonic()
        manifest = await archive.export_range(
            pool, lcfg, archive.archive_spec("telemetry"), day=lo.date(), lo=lo, hi=lo + timedelta(hours=2)
        )
        dt = time.monotonic() - t0
        print(
            f"\n### archive: 2 h of telemetry ({manifest.row_count:,} rows) -> {manifest.bytes / 1e6:.1f} MB "
            f"{manifest.codec} in {dt:.1f} s = {manifest.bytes / max(manifest.row_count, 1):.1f} B/row"
        )
        t0 = time.monotonic()
        result = await rollup.run_rollups(pool, lcfg, datetime.now(UTC))
        print(f"### rollups: first run (1 h backfill) {result} in {time.monotonic() - t0:.1f} s")
        t0 = time.monotonic()
        result = await rollup.run_rollups(pool, lcfg, datetime.now(UTC))
        print(f"### rollups: steady-state run {result} in {time.monotonic() - t0:.1f} s")
        _cleanup(cold)
    finally:
        await pool.close()


def main() -> int:
    cfg = _cfg()
    dsn = build_dsn(cfg)
    migrate_sync(dsn)
    now = datetime.now(UTC).replace(microsecond=0)
    # Capped at ~50k telemetry rows: a full synthetic day (17.3 M rows) saturated the shared production
    # disk on 2026-09-26 and was aborted at the lead's request. Read estimated plans at this size.
    hours = min(float(os.environ.get("OG_DLC_SEED_HOURS", "0.05")), MAX_SEED_HOURS)
    lo, hi = now - timedelta(hours=hours), now

    async def _parts() -> None:
        pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
        await pool.open(wait=True, timeout=10)
        try:
            lcfg = LifecycleConfig()
            print("partitions:", await partitions.ensure_partitions(pool, lcfg, "telemetry", now))
            print(
                "partitions 1m:",
                await partitions.ensure_partitions(pool, lcfg, "telemetry_1m", now, days_back=2),
            )
        finally:
            await pool.close()

    asyncio.run(_parts())
    with psycopg.connect(dsn, autocommit=True) as conn:
        seeded = conn.execute("SELECT count(*) FROM og.telemetry WHERE hub_id LIKE 'xh-%'").fetchone()
        if not seeded or seeded[0] == 0:
            conn.execute("SET synchronous_commit = off")
            for name, stmt in SEED_SQL:
                t0 = time.monotonic()
                params = {"lo": lo, "hi": hi, "opp": OPPORTUNITY, "contract": CONTRACT}
                rows = 0
                for part in (s for s in stmt.split(";") if s.strip()):
                    cur = conn.execute(part, params if "%(" in part else None)  # type: ignore[arg-type]
                    rows += max(cur.rowcount, 0)
                print(f"seed {name}: {rows:,} rows in {time.monotonic() - t0:.1f} s", flush=True)
            conn.execute("VACUUM ANALYZE og.telemetry, og.grant, og.feed_obs, og.hub, og.hub_state")
        from opengrid.lifecycle.indexes import ensure_indexes

        async def _idx() -> list[str]:
            pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
            await pool.open(wait=True, timeout=10)
            try:
                return await ensure_indexes(pool)
            finally:
                await pool.close()

        print("indexes built:", asyncio.run(_idx()))
        sizes = conn.execute(
            "SELECT partition, total_bytes, (SELECT count(*) FROM og.telemetry) FROM og.lifecycle_partitions "
            "WHERE parent = 'telemetry' AND total_bytes > 1000000 ORDER BY 1"
        ).fetchall()
        total_rows = sizes[0][2] if sizes else 0
        total_bytes = sum(s[1] for s in sizes)
        print(
            f"\n### telemetry: {total_rows:,} rows, {total_bytes / 1e9:.2f} GB incl. PK + BRIN "
            f"= {total_bytes / max(total_rows, 1):.0f} B/row"
        )
        for name, table in (("grant", "og.grant"), ("feed_obs", "og.feed_obs")):
            row = conn.execute(
                "SELECT pg_total_relation_size(%s::regclass), (SELECT reltuples FROM pg_class WHERE oid = %s::regclass)",
                (table, table),
            ).fetchone()
            assert row is not None
            print(
                f"### {name}: {row[0] / 1e9:.2f} GB, ~{int(row[1]):,} rows = {row[0] / max(row[1], 1):.0f} B/row"
            )
        for title, query in QUERIES:
            _explain(conn, title, query)
    asyncio.run(_measure(dsn, lo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
