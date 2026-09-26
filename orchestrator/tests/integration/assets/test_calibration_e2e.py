"""Real-Postgres integration test for the asset-health/calibration workflow (07-delivery/06 S5.5, WP-I):
`opengrid.assets.runner.run_once` (detect -> WATCH -> request_calibration) and `opengrid.assets.
calibration_ack.handle_calibration_ack` (verify -> re-admit, or escalate -> work order), against the
REAL `og.hub_inverter_pq`/`og.pq_waveform_summary`/`og.calibration_attempt`/`og.maintenance_work_order`
tables and the REAL `opengrid.pq_ingest` ingestion path (no fakes) -- run via
`powershell -File tools\\remote.ps1 -Ws assets -Cmd "cd orchestrator && python -m pytest tests/integration/assets -q"`.

**Scope note (from the WP-I build report).** This drives the two scenarios end to end through every
piece this package owns: `calibration_drift_correctable` (WATCH -> CalibrationCommand candidate
recorded -> a CORRECTED ack -> back to OK) and `calibration_drift_hardware` (WATCH -> candidate ->
a NO_CHANGE ack -> DEGRADED + a maintenance work order). It does NOT drive a live `ogsim.fleet`
subprocess over real MQTT with a guardian-signed command: `guardian.service.evaluate_and_sign_
calibration` currently signs the wrong payload shape (see the build report), so a real signature would
be rejected by ogsim's own `verify_calibration_signature` regardless of anything this test does -- that
gap is guardian's to fix, not this package's. The ack payloads here are constructed directly, exactly the
shape `ogsim.fleet.calibration.build_calibration_ack` produces (narrowed to the wire schema), so this
test exercises the real contract on the orchestrator side of that boundary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _cfg():
    return load_config(_CONFIG_PATH)


def _dsn() -> str | None:
    try:
        return build_dsn(_cfg())
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_CFG = _cfg() if os.environ.get("OG_DB") else None
_DSN = _dsn()
requires_server = pytest.mark.skipif(
    _CFG is None or _DSN is None or not _db_reachable(_DSN),
    reason="Postgres not reachable locally; run via tools/remote.ps1 -Ws assets (BUILD.md S5)",
)


def _cleanup_stale_test_hubs(dsn: str) -> None:
    """`run_once` sweeps every hub with a characterization row -- a leftover `itassets-*` fixture from
    an earlier failed/interrupted run (this workspace's DB is shared across test sessions) can otherwise
    contaminate a later test's sweep counts (R2 incident test-hardening: this exact class of surprise is
    why the assertions below check the hub under test directly rather than global sweep totals, but
    cleaning up here too keeps repeated local runs from accumulating cruft)."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM og.asset_event WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.maintenance_work_order WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.calibration_command WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.calibration_attempt WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.pq_waveform_summary WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.hub_inverter_pq WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.hub WHERE hub_id LIKE 'itassets-%'")
        cur.execute("DELETE FROM og.bank WHERE bank_id LIKE 'itassets-%'")


def _seed_hub(dsn: str, hub_id: str) -> None:
    _cleanup_stale_test_hubs(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM og.asset_event WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.maintenance_work_order WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.calibration_command WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.calibration_attempt WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.pq_waveform_summary WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.hub_inverter_pq WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.hub WHERE hub_id = %s", (hub_id,))
        cur.execute("DELETE FROM og.bank WHERE bank_id = %s", (f"{hub_id}-bank",))
        cur.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva) VALUES (%s, %s, %s, %s)",
            (f"{hub_id}-bank", "LZ_NORTH", 600.0, 0.0),
        )
        cur.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES (%s, %s, %s, %s, %s, %s)",
            (hub_id, f"{hub_id}-bank", "LZ_NORTH", 39.2, 7.84, 11.0),
        )
        cur.execute(
            """
            INSERT INTO og.hub_inverter_pq (hub_id, phase_connection, kva_rating, freq_offset_std_hz)
            VALUES (%s, 'A', 12.0, 0.01)
            """,
            (hub_id,),
        )


def _seed_drifting_summaries(dsn: str, hub_id: str, now: datetime, *, freq_hz: float) -> None:
    """Twelve summaries, one per minute over the trailing 12 minutes (inside the default 15-minute
    observation window), each with `freq_hz` well past the hub's `freq_offset_std_hz`/floor -- enough to
    satisfy `is_persistent_drift`'s >=90% threshold."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for i in range(12):
            ts = now - timedelta(minutes=11 - i)
            cur.execute(
                """
                INSERT INTO og.pq_waveform_summary (hub_id, ts, freq_hz, v_rms_a, thd_i_pct_a, phase_angle_deg_a)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (hub_id, ts) DO NOTHING
                """,
                (hub_id, ts, Decimal(str(freq_hz)), Decimal("240.0"), Decimal("2.0"), Decimal("0.0")),
            )


def _build_service(pool):
    from opengrid.assets.repo import (
        PgAssetEventRepo,
        PgAssetHealthRepo,
        PgCalibrationAttemptRepo,
        PgDriftObservationRepo,
        PgSensitiveGrantPort,
        PgWorkOrderRepo,
        TraceStoreAssetTracePort,
    )
    from opengrid.assets.service import AssetHealthPorts, AssetHealthService
    from opengrid.trace import TraceStore
    from opengrid.trace.pg_backend import PgTraceBackend

    trace_store = TraceStore(PgTraceBackend(pool))
    return AssetHealthService(
        ports=AssetHealthPorts(
            drift=PgDriftObservationRepo(pool),
            asset_health=PgAssetHealthRepo(pool),
            calibration_attempts=PgCalibrationAttemptRepo(pool),
            work_orders=PgWorkOrderRepo(pool),
            asset_events=PgAssetEventRepo(pool),
            trace=TraceStoreAssetTracePort(trace_store),
            sensitive_grants=PgSensitiveGrantPort(pool),
        )
    )


async def _configure_pq_ingest(pool, tmp_path) -> None:
    from opengrid import pq_ingest
    from opengrid.pq_ingest.blob_store import FileBlobStore
    from opengrid.pq_ingest.pg_backend import PgPqIngestBackend

    pq_ingest.configure(PgPqIngestBackend(pool), FileBlobStore(str(tmp_path)))


async def _issue_like_the_guardian(pool, calibration_id, hub_id: str, ack: dict) -> tuple[dict, object]:
    """What og-guardian does on signing (its real ledger adapter): claim the attempt with the hub's next
    (epoch, seq) in og.calibration_command and mark it SIGNED. The hub echoes (epoch, seq) in its ack."""
    from opengrid.assets.repo import PgCalibrationAckGuard
    from opengrid.guardian.pq_repo import PgCalibrationLedgerPort

    ledger = PgCalibrationLedgerPort(pool)
    sequence = await ledger.reserve(calibration_id, hub_id)
    assert sequence is not None
    await ledger.mark_signed(calibration_id)
    epoch, seq = sequence
    return {**ack, "epoch": epoch, "seq": seq}, PgCalibrationAckGuard(pool)


@requires_server
async def test_calibration_drift_correctable_detect_calibrate_verify_readmit(tmp_path) -> None:
    """ES16/TS-16a: a persistent frequency drift is detected (`run_once` -> WATCH, a `PENDING`
    `og.calibration_attempt` recorded), and a `CORRECTED` ack returns the hub to `OK`."""
    assert _DSN is not None
    migrate_sync(_DSN)
    hub_id = "itassets-correctable"
    now = datetime.now(UTC)
    _seed_hub(_DSN, hub_id)
    _seed_drifting_summaries(_DSN, hub_id, now, freq_hz=60.2)

    from psycopg_pool import AsyncConnectionPool

    from opengrid.assets import runner
    from opengrid.assets.calibration_ack import handle_calibration_ack

    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        await _configure_pq_ingest(pool, tmp_path)
        service = _build_service(pool)

        # `run_once` sweeps every hub with a characterization row -- this test's DB workspace may carry
        # other hubs' leftover fixtures from other test runs, so assert on THIS hub, not global counts.
        result = await runner.run_once(service, now=now, enabled=True)
        assert result.evaluated >= 1
        assert result.calibrations_requested >= 1

        record = await service.ports.asset_health.get(hub_id)
        assert record is not None and record.asset_state == "WATCH"

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT calibration_id FROM og.calibration_attempt WHERE hub_id = %s AND outcome = 'PENDING'",
                (hub_id,),
            )
            row = cur.fetchone()
        assert row is not None
        calibration_id = row[0]

        ack = {
            "calibration_id": str(calibration_id),
            "hub_id": hub_id,
            "applied": True,
            "applied_at": now.isoformat(),
            "resulting_offsets": {"freq_hz": 0.0, "voltage_pct": 0.0, "phase_deg": 0.0},
            "status": "APPLIED",
        }
        ack, guard = await _issue_like_the_guardian(pool, calibration_id, hub_id, ack)
        outcome = await handle_calibration_ack(service, ack, now=now, topic_hub_id=hub_id, guard=guard)
        assert await handle_calibration_ack(service, ack, now=now, topic_hub_id=hub_id, guard=guard) is None
        assert outcome is not None and outcome.value == "CORRECTED"

        record = await service.ports.asset_health.get(hub_id)
        assert record is not None and record.asset_state == "OK"

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM og.maintenance_work_order WHERE hub_id = %s", (hub_id,))
            (work_orders,) = cur.fetchone()
        assert work_orders == 0
    finally:
        await pool.close()


@requires_server
async def test_calibration_drift_hardware_escalates_to_work_order(tmp_path) -> None:
    """ES17/TS-17a (calibration half): a drift a `NO_CHANGE` ack cannot correct moves the hub to
    `DEGRADED` and opens exactly one maintenance work order carrying the calibration evidence."""
    assert _DSN is not None
    migrate_sync(_DSN)
    hub_id = "itassets-hardware"
    now = datetime.now(UTC)
    _seed_hub(_DSN, hub_id)
    _seed_drifting_summaries(_DSN, hub_id, now, freq_hz=60.2)

    from psycopg_pool import AsyncConnectionPool

    from opengrid.assets import runner
    from opengrid.assets.calibration_ack import handle_calibration_ack

    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        await _configure_pq_ingest(pool, tmp_path)
        service = _build_service(pool)

        result = await runner.run_once(service, now=now, enabled=True)
        assert result.calibrations_requested >= 1

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT calibration_id FROM og.calibration_attempt WHERE hub_id = %s AND outcome = 'PENDING'",
                (hub_id,),
            )
            (calibration_id,) = cur.fetchone()

        # calibration_drift_hardware: the command has no effect -- resulting_offsets == the pre-drift
        # measured offset, so classify_calibration_outcome resolves NO_CHANGE.
        ack = {
            "calibration_id": str(calibration_id),
            "hub_id": hub_id,
            "applied": True,
            "applied_at": now.isoformat(),
            "resulting_offsets": {"freq_hz": 0.2, "voltage_pct": 0.0, "phase_deg": 0.0},
            "status": "APPLIED",
        }
        ack, guard = await _issue_like_the_guardian(pool, calibration_id, hub_id, ack)
        outcome = await handle_calibration_ack(service, ack, now=now, topic_hub_id=hub_id, guard=guard)
        assert outcome is not None and outcome.value == "NO_CHANGE"

        record = await service.ports.asset_health.get(hub_id)
        assert record is not None and record.asset_state == "DEGRADED"

        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, severity, evidence FROM og.maintenance_work_order WHERE hub_id = %s",
                (hub_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        status, _severity, evidence = rows[0]
        assert status == "OPEN"
        assert evidence["outcome"] == "NO_CHANGE"

        # Running the sweep again must not open a second work order for the same hub.
        await runner.run_once(service, now=now + timedelta(minutes=1), enabled=True)
        with psycopg.connect(_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM og.maintenance_work_order WHERE hub_id = %s", (hub_id,))
            (count,) = cur.fetchone()
        assert count == 1
    finally:
        await pool.close()
