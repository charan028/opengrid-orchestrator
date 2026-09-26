"""Instantiation/round-trip (shape) tests for the DB-row pydantic contracts in opengrid.core.models.pq
that have no standalone JSON Schema of their own (they mirror orchestrator/migrations/0010_service_profile.sql
and 0011_asset_health.sql directly). See test_pq_schema_validation.py for the contract-side objects and
wire messages that DO validate against interfaces/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from opengrid.core.models import pq

NOW = datetime.now(UTC)


def test_hub_inverter_pq_defaults_to_ok_asset_state():
    row = pq.HubInverterPq(hub_id="hub-1", phase_connection="A", kva_rating=Decimal("11.5"))
    assert row.asset_state == "OK"
    assert row.quality_score == Decimal("1.0")
    assert row.dominant_harmonics is None


def test_hub_inverter_pq_with_dominant_harmonics_and_asset_state():
    row = pq.HubInverterPq(
        hub_id="hub-1",
        phase_connection="ABC",
        kva_rating=Decimal("600"),
        dominant_harmonics={
            "3": pq.HarmonicComponent(mag_pct=Decimal("1.2"), angle_deg=Decimal("40")),
            "5": pq.HarmonicComponent(mag_pct=Decimal("0.8"), angle_deg=Decimal("120")),
        },
        asset_state="WATCH",
        asset_state_since=NOW,
        consecutive_correctable_drifts=1,
    )
    assert row.dominant_harmonics is not None
    assert row.dominant_harmonics["3"].mag_pct == Decimal("1.2")
    assert row.asset_state == "WATCH"


def test_pq_waveform_summary_row_allows_partial_fast_block():
    row = pq.PqWaveformSummaryRow(hub_id="hub-1", ts=NOW, v_rms_a=Decimal("240.1"), freq_hz=Decimal("60.01"))
    assert row.harmonics_v is None
    assert row.sync_source is None


def test_pq_waveform_raw_index_defaults():
    idx = pq.PqWaveformRawIndex(
        capture_id=uuid4(), hub_id="hub-1", ts=NOW, trigger_reason="ROTATING_AUDIT", blob_ref="s3://bucket/key",
        channels=6,
    )
    assert idx.sample_rate_hz == Decimal("7680")
    assert idx.cycles == 10
    assert idx.retain_until is None


def test_calibration_attempt_defaults_pending():
    attempt = pq.CalibrationAttempt(
        calibration_id=uuid4(), hub_id="hub-1", requested_at=NOW,
        reference_phase_deg=Decimal("0"), reference_freq_hz=Decimal("60"), reference_amplitude_v=Decimal("240"),
    )
    assert attempt.outcome == "PENDING"
    assert attempt.command_batch_id is None


def test_maintenance_work_order_and_asset_event():
    work_order = pq.MaintenanceWorkOrder(
        work_order_id=uuid4(), hub_id="hub-1", severity="HIGH",
        evidence={"calibration_attempt_ids": [str(uuid4())]}, opened_at=NOW,
    )
    assert work_order.status == "OPEN"

    event = pq.AssetEvent(
        asset_event_id=uuid4(), hub_id="hub-1", work_order_id=work_order.work_order_id,
        event_type="INVERTER_REPLACED", old_inverter_serial="SN-OLD-1", new_inverter_serial="SN-NEW-1",
        occurred_at=NOW,
    )
    assert event.event_type == "INVERTER_REPLACED"
    assert event.from_state is None


def test_decision_type_gains_asset_health_values():
    from opengrid.core.models import engine

    row = engine.TraceRow(
        trace_id=uuid4(), decision_type="ASSET_STATE_TRANSITION", event_class="ASSET_STATE_TRANSITION",
        stream_id="s1", seq=0, payload={"hub_id": "hub-1", "to_state": "WATCH"}, hash="h",
    )
    assert row.decision_type == "ASSET_STATE_TRANSITION"

    calib_row = engine.TraceRow(
        trace_id=uuid4(), decision_type="CALIBRATION_ATTEMPT", event_class="CALIBRATION_ATTEMPT",
        stream_id="s1", seq=1, payload={"hub_id": "hub-1"}, hash="h2",
    )
    assert calib_row.decision_type == "CALIBRATION_ATTEMPT"
