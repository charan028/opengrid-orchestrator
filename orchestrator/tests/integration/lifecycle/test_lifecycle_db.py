"""Migration 0033 + `opengrid.lifecycle` against real Postgres (workspace `dlc`, DB og_t_dlc).

Run: powershell -File tools\\remote.ps1 -Ws dlc -Cmd "cd orchestrator && python -m pytest tests/integration/lifecycle -q"

Every test resets the lifecycle-managed state it touches (telemetry partitions, rollups, registries) --
this database belongs to the dlc workspace only. Archives go to a per-test tmp directory, never to
/srv/ogbackup.
"""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle import archive, partitions, retention, rollup
from opengrid.lifecycle.policy import LifecycleConfig, day_start, utc_today
from opengrid.lifecycle.runner import run_lifecycle

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1 -Ws dlc)"
)

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "0033_data_lifecycle.sql"
HUBS = ("lc-hub-1", "lc-hub-2", "lc-hub-3")


@pytest.fixture(scope="module")
def dsn(server_config: Any, _migrated: bool) -> str:
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False)
    await p.open(wait=True, timeout=10)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def lcfg(tmp_path: Path) -> LifecycleConfig:
    return LifecycleConfig(cold_root=tmp_path / "cold", cold_min_free_gb=0.1, lock_timeout_s=5)


def _reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT parent, partition FROM og.lifecycle_partitions "
            "WHERE parent IN ('telemetry', 'telemetry_1m') AND NOT is_default"
        ).fetchall()
        for parent, name in rows:
            conn.execute(
                sql.SQL("ALTER TABLE og.{} DETACH PARTITION og.{}").format(
                    sql.Identifier(parent), sql.Identifier(name)
                )
            )
            conn.execute(sql.SQL("DROP TABLE og.{}").format(sql.Identifier(name)))
        conn.execute("ALTER TABLE og.telemetry_default DROP CONSTRAINT IF EXISTS lifecycle_swap_range")
        conn.execute(
            "TRUNCATE og.telemetry_default, og.telemetry_1m_default, og.telemetry_15m, og.lifecycle_archive, "
            "og.lifecycle_watermark, og.lifecycle_run"
        )
        conn.execute("UPDATE og.data_retention SET legal_hold = false")
        conn.execute("DELETE FROM og.feed_obs WHERE source = 'lc-test'")


def _seed_telemetry(dsn: str, start: datetime, end: datetime, step: str = "10 minutes") -> int:
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(
            "INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health) "
            "SELECT h, g, 20.0, -5.0, 0, 0, 'online' FROM unnest(%s::text[]) h, "
            "generate_series(%s::timestamptz, %s::timestamptz, %s::interval) g",
            (list(HUBS), start, end, step),
        )
        conn.commit()
        return cur.rowcount


def _count(dsn: str, query: str, params: tuple[Any, ...] = ()) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(query, params).fetchone()  # type: ignore[arg-type]
    return int(row[0]) if row else 0


def _partition_of(dsn: str, ts: datetime) -> str:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO og.telemetry (hub_id, ts, p_kw, seq, epoch) VALUES ('lc-probe', %s, 0, 0, 0)", (ts,)
        )
        row = conn.execute(
            "SELECT tableoid::regclass::text FROM og.telemetry WHERE hub_id = 'lc-probe' AND ts = %s", (ts,)
        ).fetchone()
        conn.rollback()
    assert row is not None
    return str(row[0])


def _assert_archive_file(path: Path, sha: str, size: int, manifest_path: Path) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == 0o440
    assert archive.file_sha256(path) == (sha, size)
    assert f'"sha256": "{sha}"' in manifest_path.read_text(encoding="utf-8")


def _tamper(path: Path) -> None:
    path.chmod(0o640)
    path.write_bytes(path.read_bytes() + b"tampered")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------------------------------


#: The settlement/audit tables 0033 seeds as protected (never deleted).
PROTECTED_BY_0033 = frozenset({"invoice_line", "pnl", "meter_interval", "performance", "trace"})


def test_0033_reapplies_cleanly(dsn: str) -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    # Re-applied twice inside ONE transaction that the rollback below discards: this proves 0033 is re-runnable
    # without persisting its 0033-era definitions over later migrations. A committed re-run put back og.trace's
    # decision_type CHECK without AUTHZ_DENY (widened by 0041), which broke any suite run after this one
    # (test_followups_sql::test_trace_accepts_authz_deny). The shared database keeps exactly the migrated schema.
    with psycopg.connect(dsn) as conn:
        conn.execute(text)
        conn.execute(text)
        applied = conn.execute(
            "SELECT 1 FROM og.schema_migrations WHERE filename = '0033_data_lifecycle.sql'"
        ).fetchone()
        assert applied is not None
        # By name, not by count: later migrations add their own protected rows (0052: the call audit trail).
        protected = {
            row[0] for row in conn.execute("SELECT table_name FROM og.data_retention WHERE protected")
        }
        assert PROTECTED_BY_0033 <= protected
        assert {"dispatch_call", "as_deployment"} <= protected  # 0052, still protected after a 0033 re-run
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE og.data_retention SET keep_days = 30 WHERE table_name = 'invoice_line'")
        conn.rollback()


async def test_default_swap_keeps_every_row_visible(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    inserted = _seed_telemetry(dsn, now - timedelta(days=3), now)
    before = _count(dsn, "SELECT count(*) FROM og.telemetry")
    assert _count(dsn, "SELECT count(*) FROM og.telemetry_default") >= inserted

    result = await partitions.ensure_partitions(pool, lcfg, "telemetry", now)
    legacy = result["swapped"]
    assert isinstance(legacy, str) and legacy.startswith("telemetry_pre")
    assert _count(dsn, "SELECT count(*) FROM og.telemetry") == before
    assert _count(dsn, "SELECT count(*) FROM og.telemetry_default") == 0

    infos = {p.name: p for p in await partitions.list_partitions(pool, "telemetry")}
    upper = infos[legacy].upper
    assert infos[legacy].lower is None and upper is not None and upper > now
    dailies = sorted(n for n, p in infos.items() if not p.is_default and n != legacy)
    assert len(dailies) >= lcfg.days_ahead  # tomorrow .. today + days_ahead
    assert _partition_of(dsn, now) == f"og.{legacy}"
    assert _partition_of(dsn, upper + timedelta(hours=1)) == f"og.telemetry_p{upper.astimezone(UTC):%Y%m%d}"

    again = await partitions.ensure_partitions(pool, lcfg, "telemetry", now)
    assert again == {"swapped": None, "created": 0}


async def test_partition_retention_archives_verifies_then_drops(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    _seed_telemetry(dsn, now - timedelta(days=3), now)
    await partitions.ensure_partitions(pool, lcfg, "telemetry", now)
    keep_row_ts = day_start(utc_today(now)) + timedelta(days=6, hours=1)
    _seed_telemetry(dsn, keep_row_ts, keep_row_ts)
    total = _count(dsn, "SELECT count(*) FROM og.telemetry")

    future = now + timedelta(days=12)  # cutoff = today + 5 days
    cutoff = day_start(utc_today(future)) - timedelta(days=7)
    outcomes = await retention.run_retention(pool, lcfg, future, only={"telemetry"})
    (outcome,) = outcomes
    assert outcome.error is None, outcome.error
    assert any(n.startswith("telemetry_pre") for n in outcome.dropped_partitions)

    remaining = _count(dsn, "SELECT count(*) FROM og.telemetry")
    assert remaining == len(HUBS)  # only the rows after the cutoff survive
    assert _count(dsn, "SELECT count(*) FROM og.telemetry WHERE ts < %s", (cutoff,)) == 0
    left = await partitions.list_partitions(pool, "telemetry")
    assert all(p.upper is None or p.upper > cutoff for p in left)

    with psycopg.connect(dsn) as conn:
        archived = conn.execute(
            "SELECT day, path, row_count, sha256, bytes, manifest_path FROM og.lifecycle_archive "
            "WHERE archive_name = 'telemetry'"
        ).fetchall()
    assert sum(r[2] for r in archived) >= total - len(HUBS)
    for _day, path, _rows, sha, size, manifest_path in archived:
        _assert_archive_file(Path(path), sha, size, Path(manifest_path))

    busiest = max(archived, key=lambda r: r[2])
    restored = await archive.restore_day(pool, "telemetry", busiest[0], schema="og_restore_dlc")
    assert restored["rows"] == busiest[2]


async def test_tampered_archive_blocks_the_drop(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    _seed_telemetry(dsn, now - timedelta(days=2), now - timedelta(days=2) + timedelta(hours=1))
    legacy = (await partitions.ensure_partitions(pool, lcfg, "telemetry", now))["swapped"]
    day = utc_today(now - timedelta(days=2))
    spec = archive.archive_spec("telemetry")
    manifest = await archive.export_range(
        pool, lcfg, spec, day=day, lo=day_start(day), hi=day_start(day) + timedelta(days=1)
    )
    _tamper(Path(manifest.path))
    before = _count(dsn, "SELECT count(*) FROM og.telemetry")

    (outcome,) = await retention.run_retention(pool, lcfg, now + timedelta(days=12), only={"telemetry"})
    assert outcome.error is not None and "checksum mismatch" in outcome.error
    assert legacy not in outcome.dropped_partitions
    names = {p.name for p in await partitions.list_partitions(pool, "telemetry")}
    assert legacy in names  # reattached
    assert _count(dsn, "SELECT count(*) FROM og.telemetry") == before


async def test_legal_hold_blocks_and_delete_mode_is_exact(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    with psycopg.connect(dsn) as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality) "
                "VALUES ('lc-test', 'p', 's', %s, %s, 'u', 'GOOD')",
                (old + timedelta(minutes=i), float(i)),
            )
        conn.execute(
            "INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality) "
            "VALUES ('lc-test', 'p', 's', %s, 1, 'u', 'GOOD')",
            (now - timedelta(days=1),),
        )
        conn.execute("UPDATE og.data_retention SET legal_hold = true WHERE table_name = 'feed_obs'")
        conn.commit()
    small = LifecycleConfig(cold_root=lcfg.cold_root, cold_min_free_gb=0.1, batch_rows=2)

    (held,) = await retention.run_retention(pool, small, now, only={"feed_obs"})
    assert held.skipped == "legal_hold" and held.deleted_rows == 0
    assert _count(dsn, "SELECT count(*) FROM og.feed_obs WHERE source = 'lc-test'") == 6

    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE og.data_retention SET legal_hold = false WHERE table_name = 'feed_obs'")
        conn.commit()
    (freed,) = await retention.run_retention(pool, small, now, only={"feed_obs"})
    assert freed.error is None
    assert _count(dsn, "SELECT count(*) FROM og.feed_obs WHERE source = 'lc-test'") == 1
    assert _count(dsn, "SELECT count(*) FROM og.feed_obs WHERE ts < %s", (now - timedelta(days=31),)) == 0
    reg = await archive.registry_get(pool, "feed_obs", utc_today(old))
    assert reg is not None and reg.row_count >= 5


async def test_command_batches_delete_with_verdicts_and_keep_calibration_evidence(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    old = now - timedelta(days=70)
    ids = [uuid4() for _ in range(3)]
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES ('lc-hub-1', 'b', 'LZ_NORTH', 39.2, 5, 11) "
            "ON CONFLICT DO NOTHING"
        )
        for i, cb in enumerate(ids):
            conn.execute(
                "INSERT INTO og.command_batch (command_batch_id, cycle_id, ledger_version, submission_id, "
                "command_count, merkle_root, created_at) VALUES (%s, 'c', 1, %s, 1, 'r', %s)",
                (cb, f"lc-{cb}", old + timedelta(minutes=i)),
            )
            conn.execute(
                "INSERT INTO og.verdict (verdict_id, command_batch_id, outcome, latency_ms, inputs_hash) "
                "VALUES (%s, %s, 'PASS', 1, 'h')",
                (uuid4(), cb),
            )
        conn.execute(
            "INSERT INTO og.calibration_attempt (hub_id, reference_phase_deg, reference_freq_hz, "
            "reference_amplitude_v, command_batch_id) VALUES ('lc-hub-1', 0, 60, 240, %s)",
            (ids[2],),
        )
        conn.commit()
    small = LifecycleConfig(cold_root=lcfg.cold_root, cold_min_free_gb=0.1, batch_rows=1)
    (outcome,) = await retention.run_retention(pool, small, now, only={"command_batch"})
    assert outcome.error is None
    left = _count(dsn, "SELECT count(*) FROM og.command_batch WHERE command_batch_id = ANY(%s)", (ids,))
    assert left == 1  # the calibration-referenced batch stays
    assert _count(dsn, "SELECT count(*) FROM og.verdict WHERE command_batch_id = ANY(%s)", (ids,)) == 1
    assert await archive.registry_get(pool, "verdict", utc_today(old)) is not None
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM og.calibration_attempt WHERE command_batch_id = %s", (ids[2],))
        conn.execute("DELETE FROM og.verdict WHERE command_batch_id = %s", (ids[2],))
        conn.execute("DELETE FROM og.command_batch WHERE command_batch_id = %s", (ids[2],))
        conn.commit()


async def test_rollups_are_correct_and_idempotent(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    now = datetime.now(UTC)
    await partitions.ensure_partitions(pool, lcfg, "telemetry", now)
    await partitions.ensure_partitions(pool, lcfg, "telemetry_1m", now, days_back=2)
    base = rollup.floor_15(now) - timedelta(minutes=14)  # a closed 15-min bucket
    a, b = base, base + timedelta(minutes=1)
    samples = [
        (a, 10.0, 1.0),
        (a + timedelta(seconds=20), -20.0, 2.0),
        (a + timedelta(seconds=40), 30.0, 3.0),
        (b, -6.0, 4.0),
        (b + timedelta(seconds=30), -6.0, None),
    ]
    with psycopg.connect(dsn) as conn:
        for ts, p, soc in samples:
            conn.execute(
                "INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch) VALUES ('lc-roll', %s, %s, %s, 0, 0)",
                (ts, soc, p),
            )
        conn.commit()

    rcfg = LifecycleConfig(cold_root=lcfg.cold_root, rollup_backfill_hours=1)
    await rollup.run_rollups(pool, rcfg, now)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT bucket, p_kw_avg, p_kw_min, p_kw_max, soc_kwh_last, energy_in_kwh, energy_out_kwh, sample_count, "
            "computed_at FROM og.telemetry_1m WHERE hub_id = 'lc-roll' ORDER BY bucket"
        ).fetchall()
        q = conn.execute(
            "SELECT bucket, p_kw_avg, p_kw_min, p_kw_max, soc_kwh_last, energy_in_kwh, energy_out_kwh, sample_count "
            "FROM og.telemetry_15m WHERE hub_id = 'lc-roll'"
        ).fetchall()
    assert len(rows) == 2
    ra, rb = rows
    assert ra[0] == a and ra[1] == pytest.approx(20 / 3) and (ra[2], ra[3], ra[4]) == (-20.0, 30.0, 3.0)
    assert ra[5] == pytest.approx(40 / 3 / 60) and ra[6] == pytest.approx(20 / 3 / 60) and ra[7] == 3
    assert rb[1] == pytest.approx(-6.0) and rb[4] == 4.0 and rb[5] == 0 and rb[6] == pytest.approx(0.1)
    assert rb[7] == 2
    assert len(q) == 1
    q15 = q[0]
    assert q15[0] == rollup.floor_15(a)
    assert q15[1] == pytest.approx(1.6) and (q15[2], q15[3], q15[4]) == (-20.0, 30.0, 4.0)
    assert q15[5] == pytest.approx(40 / 3 / 60) and q15[6] == pytest.approx(20 / 3 / 60 + 0.1) and q15[7] == 5

    await rollup.run_rollups(pool, rcfg, now + timedelta(seconds=5))
    with psycopg.connect(dsn) as conn:
        again = conn.execute(
            "SELECT bucket, computed_at FROM og.telemetry_1m WHERE hub_id = 'lc-roll' ORDER BY bucket"
        ).fetchall()
    assert [(r[0], r[1]) for r in again] == [(r[0], r[8]) for r in rows]  # unchanged: nothing rewritten


async def test_billing_export_is_write_once(
    pool: AsyncConnectionPool, lcfg: LifecycleConfig, dsn: str
) -> None:
    _reset(dsn)
    first = await archive.export_billing_month(pool, lcfg, date(2020, 1, 1))
    assert "digest" in first and set(first["rows"]) == {"billing_invoice_line", "billing_pnl"}
    manifest = lcfg.cold_root / "billing" / "2020" / "01" / "manifest.json"
    assert _mode(manifest) == 0o440
    second = await archive.export_billing_month(pool, lcfg, date(2020, 1, 1))
    assert second["skipped"] == "exists"


async def test_run_lifecycle_records_and_traces(
    dsn: str, pool: AsyncConnectionPool, lcfg: LifecycleConfig
) -> None:
    _reset(dsn)
    summary = await run_lifecycle(pool, lcfg, cycle="hourly")
    assert summary["ok"], summary["errors"]
    assert "retention" in summary and "rollups" in summary
    with psycopg.connect(dsn) as conn:
        run = conn.execute("SELECT ok FROM og.lifecycle_run ORDER BY started_at DESC LIMIT 1").fetchone()
        traced = conn.execute(
            "SELECT decision_type FROM og.trace WHERE event_class = 'lifecycle' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert run is not None and run[0] is True
    assert traced is not None and traced[0] == "DATA_LIFECYCLE"
    # auto: an hourly run just succeeded, so the next timer tick is a fast cycle
    follow_up = await run_lifecycle(pool, lcfg, cycle="auto")
    assert follow_up["cycle"] == "fast" and follow_up["ok"]
