"""`opengrid.assets.calibration_ack.handle_calibration_ack` -- ES16 (corrected, re-admit) and ES17
(uncorrectable, work order) driven off a `CalibrationAck` message, plus the unknown/mismatched/rejected
guard rails (07-delivery/06 S6.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from opengrid.assets.calibration_ack import IssuedCalibration
from opengrid.assets.calibration_ack import handle_calibration_ack as _handle
from opengrid.core.crypto import generate_keypair, sign_payload
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
        "epoch": 1,
        "seq": 5,
    }
    base.update(overrides)
    return base


class FakeAckGuard:
    """`CalibrationAckGuard` over an in-memory og.calibration_command: the command the guardian issued."""

    def __init__(self, *, hub_id: str = HUB_ID, epoch: int = 1, seq: int = 5, signed: bool = True) -> None:
        self.issued_command = (
            IssuedCalibration(hub_id=hub_id, epoch=epoch, seq=seq, ack_consumed=False) if signed else None
        )
        self.consumed = 0
        self.protocol_closed: list[UUID] = []
        self.alerts: list[str] = []
        self.hub_keys: dict[str, bytes] = {}

    async def issued(self, calibration_id: UUID) -> IssuedCalibration | None:
        return self.issued_command if calibration_id == CALIBRATION_ID else None

    async def consume(self, calibration_id: UUID) -> bool:
        if self.consumed:
            return False
        self.consumed += 1
        return True

    async def close_protocol_error(self, calibration_id: UUID, *, at: datetime) -> None:
        self.protocol_closed.append(calibration_id)

    async def raise_alert(self, rule: str, summary: str, detail: dict[str, object]) -> None:
        self.alerts.append(str(detail["reason"]))

    def hub_public_key(self, hub_id: str) -> bytes | None:
        return self.hub_keys.get(hub_id)


async def handle_calibration_ack(service, payload, *, now, guard=None, topic_hub_id=HUB_ID):
    return await _handle(service, payload, now=now, guard=guard or FakeAckGuard(), topic_hub_id=topic_hub_id)


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


# --- #11 consume-once and binding; #15 protocol rejections --------------------------------------------------


async def test_a_redelivered_ack_is_consumed_only_once(service, fakes):
    """Regression (#11): the same ack (QoS 1 redelivery or replay) was processed on every delivery."""
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    guard = FakeAckGuard()
    ack = _ack(resulting_offsets={"freq_hz": 0.03, "voltage_pct": 0.5, "phase_deg": 1.0})

    first = await handle_calibration_ack(service, ack, now=NOW, guard=guard)
    second = await handle_calibration_ack(service, ack, now=NOW, guard=guard)

    assert first == CalibrationOutcome.NO_CHANGE and second is None and guard.consumed == 1
    assert len(fakes.work_orders.open_orders) == 1


@pytest.mark.parametrize(
    ("guard_kwargs", "ack_overrides", "topic_hub", "reason"),
    [
        ({}, {}, "hub-other", "TOPIC_HUB_MISMATCH"),
        ({"signed": False}, {}, HUB_ID, "NOT_ISSUED"),
        ({"hub_id": "hub-other"}, {}, HUB_ID, "HUB_MISMATCH"),
        ({}, {"seq": 4}, HUB_ID, "SEQUENCE_MISMATCH"),
        ({}, {"epoch": None, "seq": None}, HUB_ID, "SEQUENCE_MISMATCH"),
    ],
)
async def test_an_ack_not_bound_to_the_issued_command_is_dropped_with_an_alert(
    service, fakes, guard_kwargs, ack_overrides, topic_hub, reason
):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    guard = FakeAckGuard(**guard_kwargs)

    ack = {k: v for k, v in _ack(**ack_overrides).items() if v is not None}
    outcome = await handle_calibration_ack(service, ack, now=NOW, guard=guard, topic_hub_id=topic_hub)

    assert outcome is None and guard.consumed == 0 and guard.alerts == [reason]
    assert fakes.asset_health.records[HUB_ID].asset_state == "WATCH"


@pytest.mark.parametrize("reject_reason", ["STALE_SEQ", "BAD_SIGNATURE", "UNKNOWN_HUB"])
async def test_a_protocol_rejection_is_not_counted_as_no_change(service, fakes, reject_reason):
    """Regression (#15): a hub rejecting a stale/badly signed command said nothing about the inverter,
    yet it escalated the asset like a failed calibration. It is now a protocol error with an alert."""
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    guard = FakeAckGuard()

    ack = _ack(status="REJECTED", applied=False, reject_reason=reject_reason)
    outcome = await handle_calibration_ack(service, ack, now=NOW, guard=guard)

    assert outcome is None
    assert fakes.asset_health.records[HUB_ID].asset_state == "WATCH"
    assert HUB_ID not in fakes.work_orders.open_orders
    assert guard.protocol_closed == [CALIBRATION_ID] and guard.alerts == [f"HUB_REJECTED_{reject_reason}"]


async def test_a_hub_signed_ack_is_enforced(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    seed, public = generate_keypair()
    unsigned = _ack()
    signed = {**unsigned, "key_id": "hub-key", "signature": sign_payload(seed, unsigned)}

    no_key = FakeAckGuard()
    assert await handle_calibration_ack(service, signed, now=NOW, guard=no_key) is None
    assert no_key.alerts == ["BAD_ACK_SIGNATURE"]

    wrong = FakeAckGuard()
    wrong.hub_keys[HUB_ID] = generate_keypair()[1]
    assert await handle_calibration_ack(service, signed, now=NOW, guard=wrong) is None

    right = FakeAckGuard()
    right.hub_keys[HUB_ID] = public
    assert await handle_calibration_ack(service, signed, now=NOW, guard=right) == CalibrationOutcome.CORRECTED


async def test_without_a_guard_no_ack_is_acted_on(service, fakes):
    await _seed_pending(fakes, measured_offset=OffsetVector(freq_hz=0.03, voltage_pct=0.5, phase_deg=1.0))
    fakes.asset_health.records[HUB_ID] = make_asset_record(asset_state="WATCH", since=NOW)
    assert await _handle(service, _ack(), now=NOW) is None
    assert fakes.asset_health.records[HUB_ID].asset_state == "WATCH"
