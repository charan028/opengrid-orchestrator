"""`AssetHealthService` orchestration: ES14-shaped drift detection, ES16 (calibration corrected),
ES17 (uncorrectable escalation), ES18 (calibration refused while a sensitive grant is active -- primary
check; the guardian's G-25 is the independent re-check, tested separately in `tests/unit/guardian/
test_pq_checks.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from opengrid.assets.calibration import CalibrationReference
from opengrid.assets.ports import DriftObservationWindow
from opengrid.assets.state_machine import InvalidAssetTransitionError
from opengrid.core.pq import CalibrationBounds, CalibrationOutcome, OffsetVector

from .conftest import HUB_ID, make_asset_record

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
ZERO_OFFSET = OffsetVector(freq_hz=0.0, voltage_pct=0.0, phase_deg=0.0)
REFERENCE = CalibrationReference(phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ptp")
BOUNDS = CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.0)


async def test_evaluate_drift_no_window_returns_none(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    assert await service.evaluate_drift(HUB_ID, now=NOW) is None


async def test_evaluate_drift_ok_stays_ok_when_not_persistent(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[False] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    assert await service.evaluate_drift(HUB_ID, now=NOW) == "OK"
    assert fakes.asset_events.state_transitions == []


async def test_evaluate_drift_ok_to_watch_on_persistent_drift(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW - timedelta(hours=1))
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    new_state = await service.evaluate_drift(HUB_ID, now=NOW)
    assert new_state == "WATCH"
    assert fakes.asset_health.records[HUB_ID].asset_state == "WATCH"
    assert fakes.asset_events.state_transitions[-1]["to_state"] == "WATCH"
    assert fakes.trace.appended[-1][0] == "ASSET_STATE_TRANSITION"


async def test_evaluate_drift_fleet_correlated_never_flagged_persistent(service, fakes):
    """S5.5.1: a deviation correlating with an already-explained fleet/bank-wide event never advances
    the lifecycle, even if every summary in the window individually exceeded the WATCH threshold."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10, correlates_with_fleet_event=True, latest_measured_offset=ZERO_OFFSET
    )
    assert await service.evaluate_drift(HUB_ID, now=NOW) == "OK"


async def test_evaluate_drift_recurrence_within_window_escalates_straight_to_degraded(service, fakes):
    """S5.5.4: a fresh persistent drift recurring within 14 days of a prior CORRECTED outcome escalates
    straight to DEGRADED instead of re-entering WATCH for another calibration attempt."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW - timedelta(days=1))
    fakes.calibration_attempts.last_corrected[HUB_ID] = NOW - timedelta(days=2)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    new_state = await service.evaluate_drift(HUB_ID, now=NOW)
    assert new_state == "DEGRADED"


async def test_evaluate_drift_watch_clears_back_to_ok(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="WATCH", since=NOW - timedelta(hours=1)
    )
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[False] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    assert await service.evaluate_drift(HUB_ID, now=NOW) == "OK"


async def test_request_calibration_refused_while_sensitive_grant_active_es18(service, fakes):
    """ES18/TS-18 (primary-check side): the ladder itself never even builds a calibration candidate for
    a hub that is the sole source of a committed PQ-sensitive obligation."""
    fakes.sensitive_grants.sensitive_hubs.add(HUB_ID)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    candidate = await service.request_calibration(
        HUB_ID, reference=REFERENCE, bounds=BOUNDS, now=NOW, lease_ttl_s=30.0
    )
    assert candidate is None


async def test_request_calibration_refused_within_rate_limit(service, fakes):
    fakes.calibration_attempts.last_attempt[HUB_ID] = NOW.timestamp() - 3600.0
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    candidate = await service.request_calibration(
        HUB_ID, reference=REFERENCE, bounds=BOUNDS, now=NOW, lease_ttl_s=30.0
    )
    assert candidate is None


async def test_request_calibration_builds_candidate_and_records_attempt(service, fakes):
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
    )
    candidate = await service.request_calibration(
        HUB_ID, reference=REFERENCE, bounds=BOUNDS, now=NOW, lease_ttl_s=30.0
    )
    assert candidate is not None
    assert candidate.hub_id == HUB_ID
    assert candidate.calibration_id in fakes.calibration_attempts.attempts
    assert fakes.trace.appended[-1][0] == "CALIBRATION_ATTEMPT"


async def test_request_calibration_without_epoch_seq_leaves_them_to_the_guardian(service, fakes):
    """#30: no placeholder (epoch, seq). The candidate carries none, the PENDING attempt row carries none,
    and the trace carries none -- the guardian assigns the real per-hub pair on og.calibration_command."""
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
    )
    candidate = await service.request_calibration(
        HUB_ID, reference=REFERENCE, bounds=BOUNDS, now=NOW, lease_ttl_s=30.0
    )
    assert candidate is not None
    assert candidate.epoch is None
    assert candidate.seq is None
    assert candidate.calibration_id in fakes.calibration_attempts.attempts
    payload = fakes.trace.appended[-1][1]
    assert "epoch" not in payload and "seq" not in payload


async def test_record_calibration_result_corrected_returns_to_ok_es16(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="WATCH", since=NOW - timedelta(minutes=20)
    )
    calibration_id = UUID(int=1)
    fakes.calibration_attempts.attempts[calibration_id] = {"hub_id": HUB_ID, "requested_at": NOW}
    outcome = await service.record_calibration_result(
        HUB_ID,
        calibration_id,
        pre=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
        post=OffsetVector(freq_hz=0.0, voltage_pct=0.0, phase_deg=0.0),
        corrected_tolerance=OffsetVector(freq_hz=0.01, voltage_pct=0.2, phase_deg=0.5),
        now=NOW,
    )
    assert outcome == CalibrationOutcome.CORRECTED
    assert fakes.asset_health.records[HUB_ID].asset_state == "OK"
    assert fakes.asset_health.records[HUB_ID].last_recalibration_at == NOW


async def test_record_calibration_result_no_change_moves_to_degraded_es17(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="WATCH", since=NOW - timedelta(minutes=20)
    )
    calibration_id = UUID(int=2)
    fakes.calibration_attempts.attempts[calibration_id] = {"hub_id": HUB_ID, "requested_at": NOW}
    outcome = await service.record_calibration_result(
        HUB_ID,
        calibration_id,
        pre=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
        post=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
        corrected_tolerance=OffsetVector(freq_hz=0.001, voltage_pct=0.01, phase_deg=0.01),
        now=NOW,
    )
    assert outcome == CalibrationOutcome.NO_CHANGE
    assert fakes.asset_health.records[HUB_ID].asset_state == "DEGRADED"


async def test_worse_rolled_back_quarantines(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="WATCH", since=NOW - timedelta(minutes=20)
    )
    calibration_id = UUID(int=3)
    fakes.calibration_attempts.attempts[calibration_id] = {"hub_id": HUB_ID, "requested_at": NOW}
    outcome = await service.record_calibration_result(
        HUB_ID,
        calibration_id,
        pre=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0),
        post=OffsetVector(freq_hz=0.2, voltage_pct=2.0, phase_deg=5.0),
        corrected_tolerance=OffsetVector(freq_hz=0.001, voltage_pct=0.01, phase_deg=0.01),
        now=NOW,
    )
    assert outcome == CalibrationOutcome.WORSE_ROLLED_BACK
    assert fakes.asset_health.records[HUB_ID].asset_state == "QUARANTINED"


async def test_full_replacement_and_recommissioning_flow_ts17a(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="DEGRADED", since=NOW - timedelta(days=1)
    )
    await fakes.work_orders.open(HUB_ID, severity="HIGH", evidence={"note": "drift"}, opened_at=NOW)

    state = await service.schedule_replacement(HUB_ID, now=NOW)
    assert state == "AWAITING_REPLACEMENT"

    state = await service.record_inverter_replaced(
        HUB_ID, old_serial="SN-OLD", new_serial="SN-NEW", old_firmware="1.0", new_firmware="1.1", now=NOW
    )
    assert state == "RECOMMISSIONING"
    assert fakes.work_orders.open_orders.get(HUB_ID) is None  # closed
    assert fakes.asset_events.replacements[-1]["new_serial"] == "SN-NEW"

    state = await service.verify_recommissioning(HUB_ID, passed=True, now=NOW + timedelta(hours=2))
    assert state == "OK"
    assert HUB_ID in fakes.asset_events.recommissioned
    assert fakes.asset_health.reset_at[HUB_ID] == NOW + timedelta(hours=2)


async def test_verify_recommissioning_failure_stays_recommissioning(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="RECOMMISSIONING", since=NOW - timedelta(hours=1)
    )
    state = await service.verify_recommissioning(HUB_ID, passed=False, now=NOW)
    assert state == "RECOMMISSIONING"
    assert HUB_ID not in fakes.asset_events.recommissioned


async def test_quarantine_escalates_degraded_hub(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(
        asset_state="DEGRADED", since=NOW - timedelta(days=1)
    )
    state = await service.quarantine(HUB_ID, now=NOW)
    assert state == "QUARANTINED"
    assert fakes.asset_health.records[HUB_ID].asset_state == "QUARANTINED"


async def test_quarantine_unknown_hub_returns_none(service):
    assert await service.quarantine("hub-unknown", now=NOW) is None


async def test_open_work_order_records_severity_and_evidence(service, fakes):
    work_order_id = await service.open_work_order(
        HUB_ID, severity="HIGH", evidence={"drift": "phase_angle"}, now=NOW
    )
    order = fakes.work_orders.open_orders[HUB_ID]
    assert order.work_order_id == work_order_id
    assert order.severity == "HIGH"
    assert order.evidence == {"drift": "phase_angle"}


async def test_schedule_replacement_unknown_hub_returns_none(service):
    assert await service.schedule_replacement("hub-unknown", now=NOW) is None


async def test_record_inverter_replaced_unknown_hub_returns_none(service):
    result = await service.record_inverter_replaced(
        "hub-unknown", old_serial=None, new_serial="SN-NEW", old_firmware=None, new_firmware="1.0", now=NOW
    )
    assert result is None


# --- R2 incident (2026-09-26): false-positive reset + the consecutive-exceedances proposal --------------


async def test_record_false_positive_reset_quarantined_to_ok_cancels_work_order(service, fakes):
    """The hub-01996/97/98 shape: QUARANTINED by the ordering-bug's wrong `WORSE_ROLLED_BACK` reading,
    with an open work order -- reset to OK, work order CANCELLED (not CLOSED), audited."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="QUARANTINED", since=NOW)
    work_order_id = await fakes.work_orders.open(HUB_ID, severity="HIGH", evidence={}, opened_at=NOW)

    state = await service.record_false_positive_reset(HUB_ID, reason="R2 ordering bug", now=NOW)

    assert state == "OK"
    assert fakes.asset_health.records[HUB_ID].asset_state == "OK"
    assert fakes.work_orders.open_orders.get(HUB_ID) is None
    assert fakes.work_orders.closed_status[work_order_id] == "CANCELLED"
    decision_types = [d for d, _ in fakes.trace.appended]
    assert "ASSET_STATE_TRANSITION" in decision_types
    assert "OPERATOR_ACTION" in decision_types
    operator_action = next(p for d, p in fakes.trace.appended if d == "OPERATOR_ACTION")
    assert operator_action["reason"] == "R2 ordering bug"
    assert operator_action["cancelled_work_order_id"] == str(work_order_id)


async def test_record_false_positive_reset_degraded_to_ok_with_no_work_order(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="DEGRADED", since=NOW)
    state = await service.record_false_positive_reset(HUB_ID, reason="false positive", now=NOW)
    assert state == "OK"
    assert fakes.work_orders.closed == []  # nothing to cancel


async def test_record_false_positive_reset_unknown_hub_returns_none(service):
    assert await service.record_false_positive_reset("hub-unknown", reason="n/a", now=NOW) is None


async def test_record_false_positive_reset_refuses_from_recommissioning(service, fakes):
    """A unit already RECOMMISSIONING had a real physical replacement -- never a "false positive" to
    undo; the state machine has no transition for it, and this must surface, not be silenced."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="RECOMMISSIONING", since=NOW)
    with pytest.raises(InvalidAssetTransitionError):
        await service.record_false_positive_reset(HUB_ID, reason="n/a", now=NOW)


async def test_evaluate_drift_min_consecutive_exceedances_blocks_single_bad_reading(service, fakes):
    """The R2 proposal: even when `is_persistent_drift`'s 90%-of-window rule is satisfied, requiring the
    tail N readings to ALL exceed threshold blocks a window where the most recent readings have already
    recovered (a single earlier bad/stale reading must not still be driving an escalation)."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    # 9 of 10 exceed (>= 90%), but the tail 3 (chronological, oldest..newest) do NOT all exceed.
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 9 + [False],
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    state = await service.evaluate_drift(HUB_ID, now=NOW, min_consecutive_exceedances=3)
    assert state == "OK"


async def test_evaluate_drift_min_consecutive_exceedances_allows_genuine_recent_drift(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 10,
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    state = await service.evaluate_drift(HUB_ID, now=NOW, min_consecutive_exceedances=3)
    assert state == "WATCH"


async def test_evaluate_drift_default_min_consecutive_exceedances_is_off(service, fakes):
    """Opt-in only: passing nothing preserves today's exact (pre-incident) persistence rule."""
    fakes.asset_health.records[HUB_ID] = make_asset_record(since=NOW)
    fakes.drift.windows[HUB_ID] = DriftObservationWindow(
        exceeded_per_summary=[True] * 9 + [False],
        correlates_with_fleet_event=False,
        latest_measured_offset=ZERO_OFFSET,
    )
    state = await service.evaluate_drift(HUB_ID, now=NOW)
    assert state == "WATCH"
