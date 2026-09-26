"""`opengrid.assets.calibration_ack.handle_calibration_ack` -- ES16 (corrected, re-admit) and ES17
(uncorrectable, work order) driven off a `CalibrationAck` message, plus the unknown/mismatched/rejected
guard rails (07-delivery/06 S6.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from opengrid.assets.calibration_ack import handle_calibration_ack
from opengrid.core.pq import CalibrationOutcome, OffsetVector

from .conftest import HUB_ID, make_asset_record

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
CALIBRATION_ID = UUID(int=42)


def _ack(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "calibration_id": str(CALIBRATION_ID),
        "hub_id": HUB_ID,
        "applied": True,
        "applied_at": "2026-09-26T12:00:00Z",
        "resulting_offsets": {"freq_hz": 0.0, "voltage_pct": 0.0, "phase_deg": 0.0},
        "status": "APPLIED",
    }
    base.update(overrides)
    return base


async def _seed_pending(fakes, *, hub_id: str = HUB_ID, measured_offset: OffsetVector) -> None:
    await fakes.calibration_attempts.record_attempt(
        calibration_id=CALIBRATION_ID,
        hub_id=hub_id,
        requested_at=NOW - timedelta(minutes=1),
        reference_phase_deg=0.0,
        reference_freq_hz=60.0,
        reference_amplitude_v=240.0,
        measured_offset=measured_offset,
        correction=OffsetVector(freq_hz=0.0, voltage_pct=0.0, phase_deg=0.0),
        command_batch_id=None,
    )


async def test_unknown_calibration_id_is_dropped(service, fakes):
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    outcome = await handle_calibration_ack(service, _ack(), now=NOW)
    assert outcome is None
    assert fakes.asset_health.records[HUB_ID].asset_state == "WATCH"  # untouched


async def test_hub_id_mismatch_is_dropped(service, fakes):
    await _seed_pending(fakes, hub_id="hub-other", measured_offset=OffsetVector(0.03, 0.5, 1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    outcome = await handle_calibration_ack(service, _ack(), now=NOW)
    assert outcome is None


async def test_corrected_ack_returns_hub_to_ok_es16(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)

    outcome = await handle_calibration_ack(service, _ack(), now=NOW)

    assert outcome == CalibrationOutcome.CORRECTED
    assert fakes.asset_health.records[HUB_ID].asset_state == "OK"
    assert HUB_ID not in fakes.work_orders.open_orders


async def test_no_change_ack_opens_work_order_es17(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)

    ack = _ack(resulting_offsets={"freq_hz": 0.03, "voltage_pct": 0.5, "phase_deg": 1.0})
    outcome = await handle_calibration_ack(service, ack, now=NOW)

    assert outcome == CalibrationOutcome.NO_CHANGE
    assert fakes.asset_health.records[HUB_ID].asset_state == "DEGRADED"
    assert HUB_ID in fakes.work_orders.open_orders
    assert fakes.work_orders.open_orders[HUB_ID].evidence["outcome"] == "NO_CHANGE"


async def test_worse_ack_quarantines_and_opens_work_order(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)

    ack = _ack(resulting_offsets={"freq_hz": 0.2, "voltage_pct": 2.0, "phase_deg": 5.0})
    outcome = await handle_calibration_ack(service, ack, now=NOW)

    assert outcome == CalibrationOutcome.WORSE_ROLLED_BACK
    assert fakes.asset_health.records[HUB_ID].asset_state == "QUARANTINED"
    assert HUB_ID in fakes.work_orders.open_orders


async def test_no_change_ack_does_not_duplicate_an_open_work_order(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    await fakes.work_orders.open(HUB_ID, severity="HIGH", evidence={}, opened_at=NOW)

    ack = _ack(resulting_offsets={"freq_hz": 0.03, "voltage_pct": 0.5, "phase_deg": 1.0})
    await handle_calibration_ack(service, ack, now=NOW)

    assert len(fakes.work_orders.open_orders) == 1


async def test_rejected_status_counts_as_no_change_es18_adjacent(service, fakes):
    """A REJECTED/EXPIRED ack (e.g. the guardian's G-25 refusal never even reached the hub, or the hub
    itself rejected an expired lease) never leaves an attempt un-resolved -- it counts toward escalation
    like any other failed attempt (S5.5.4)."""
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)

    ack = _ack(status="REJECTED", applied=False)
    outcome = await handle_calibration_ack(service, ack, now=NOW)

    assert outcome == CalibrationOutcome.NO_CHANGE
    assert fakes.asset_health.records[HUB_ID].asset_state == "DEGRADED"
    assert HUB_ID in fakes.work_orders.open_orders
