"""opengrid.assets.calibration_ack -- ingests `CalibrationAck` messages (interfaces/mqtt/
calibration_ack.schema.json, 07-delivery/06 S6.7) and turns each one into an `AssetHealthService.
record_calibration_result` call, opening a maintenance work order (S5.5.5) when the outcome is
uncorrectable.

This module owns the ack HALF of the calibration MQTT loop's orchestrator side; the guardian owns
signing and publishing the command. The engine-owned MQTT ingest loop routes `<root>/ack/cal/<hub_id>`
here, passing the topic's hub id and a `CalibrationAckGuard` (`opengrid.assets.repo.PgCalibrationAckGuard`).

An ack is acted on at most once, and only when it is bound to a command the guardian actually issued
(interfaces/crypto.md S2.5):

- the topic's hub, the ack's hub and the issued command's hub all match, and so does the calibration id;
- the ack echoes the issued command's `(epoch, seq)`;
- if the ack carries a hub signature (`key_id`/`signature`, additive), it verifies against that hub's
  registered key -- a signed ack from a hub with no registered key is refused;
- the command's `og.calibration_command.ack_consumed_at` is set atomically on first use, so a
  redelivered (QoS 1) or replayed ack is ignored.

A hub-side PROTOCOL rejection (`reject_reason` STALE_SEQ, BAD_SIGNATURE, UNKNOWN_HUB) says nothing about
the inverter: it is never counted as NO_CHANGE escalation. The attempt is closed `FAILED_NO_ACK` and
ALR-CALIBRATION-PROTOCOL is raised instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from opengrid.assets.service import AssetHealthService
from opengrid.core.crypto import verify_payload
from opengrid.core.models.pq import CalibrationAck
from opengrid.core.pq import CalibrationOutcome, OffsetVector
from opengrid.core.pq.constants import (
    DRIFT_FLOOR_FREQ_HZ,
    DRIFT_FLOOR_PHASE_DEG,
    DRIFT_FLOOR_VOLTAGE_PCT,
)

logger = logging.getLogger(__name__)

#: S5.5.4 verification tolerance: a post-calibration offset within these bounds on every axis is
#: `CORRECTED` (`opengrid.core.pq.classify_calibration_outcome`). Reuses the SAME floors S5.5.1 already
#: defines for "a deviation this small is not even WATCH-worthy" -- a residual offset below the floor
#: that would trip a fresh drift detection cannot reasonably be called "still drifting" (BUILD.md S1: no
#: second set of near-identical magic numbers).
CORRECTED_TOLERANCE = OffsetVector(
    freq_hz=DRIFT_FLOOR_FREQ_HZ, voltage_pct=DRIFT_FLOOR_VOLTAGE_PCT, phase_deg=DRIFT_FLOOR_PHASE_DEG
)

#: S5.5.5: severity for a work order opened directly off an uncorrectable/rolled-back calibration
#: outcome (as opposed to `runner.py`'s periodic-sweep work orders, which use the same default).
ESCALATION_WORK_ORDER_SEVERITY = "HIGH"

_ESCALATING_OUTCOMES = frozenset({CalibrationOutcome.NO_CHANGE, CalibrationOutcome.WORSE_ROLLED_BACK})

#: Hub rejections that are orchestrator/hub protocol disagreements, not evidence about the inverter.
PROTOCOL_REJECT_REASONS = frozenset({"STALE_SEQ", "BAD_SIGNATURE", "UNKNOWN_HUB"})
PROTOCOL_ALERT_RULE = "ALR-CALIBRATION-PROTOCOL"


@dataclass(frozen=True, slots=True)
class IssuedCalibration:
    """What the guardian actually issued for a calibration id (`og.calibration_command`)."""

    hub_id: str
    epoch: int
    seq: int
    ack_consumed: bool


class CalibrationAckGuard(Protocol):
    async def issued(self, calibration_id: UUID) -> IssuedCalibration | None:
        """The guardian-SIGNED command for this id, or None (never signed, refused, or unknown)."""
        ...

    async def consume(self, calibration_id: UUID) -> bool:
        """Atomically mark the command's ack consumed. True only for the first caller."""
        ...

    async def close_protocol_error(self, calibration_id: UUID, *, at: datetime) -> None:
        """Close the attempt as FAILED_NO_ACK without any asset-state change."""
        ...

    async def raise_alert(self, rule: str, summary: str, detail: dict[str, object]) -> None: ...

    def hub_public_key(self, hub_id: str) -> bytes | None: ...


def _ack_signature_ok(payload: dict[str, Any], guard: CalibrationAckGuard, hub_id: str) -> bool:
    signature = payload.get("signature")
    if signature is None:
        return True  # unsigned acks are accepted (bound by hub, id and (epoch, seq) above)
    key = guard.hub_public_key(hub_id)
    if key is None or not isinstance(signature, str):
        return False
    signed = {k: v for k, v in payload.items() if k not in ("key_id", "signature")}
    return verify_payload(key, signed, signature)


async def _protocol_error(
    guard: CalibrationAckGuard, reason: str, ack: CalibrationAck, *, extra: dict[str, object] | None = None
) -> None:
    logger.warning(
        "calibration ack protocol error, dropped",
        extra={"reason": reason, "calibration_id": str(ack.calibration_id), "hub_id": ack.hub_id},
    )
    try:
        await guard.raise_alert(
            PROTOCOL_ALERT_RULE,
            f"calibration ack protocol error: {reason}",
            {
                "reason": reason,
                "calibration_id": str(ack.calibration_id),
                "hub_id": ack.hub_id,
                **(extra or {}),
            },
        )
    except Exception:
        logger.exception("failed to raise %s", PROTOCOL_ALERT_RULE)


async def handle_calibration_ack(
    service: AssetHealthService,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    topic_hub_id: str | None = None,
    guard: CalibrationAckGuard | None = None,
) -> CalibrationOutcome | None:
    """Validates `payload` as a `CalibrationAck` (the caller validates against `calibration_ack.
    schema.json` first), binds it to the command the guardian issued (see the module docstring), consumes
    it exactly once, then classifies the outcome from the pending attempt's own pre-calibration offset
    (never the ack's claim of "before") and opens a maintenance work order when it is `NO_CHANGE`/
    `WORSE_ROLLED_BACK` (S5.5.4). Returns `None` for anything not acted on: no guard (fail closed), a
    binding failure, a duplicate, an unknown attempt, or a protocol rejection."""
    ack = CalibrationAck.model_validate(payload)
    now = now or datetime.now(UTC)
    if guard is None:
        logger.error("calibration ack dropped: no CalibrationAckGuard wired (acks are consumed only once)")
        return None
    if topic_hub_id is not None and topic_hub_id != ack.hub_id:
        await _protocol_error(guard, "TOPIC_HUB_MISMATCH", ack, extra={"topic_hub_id": topic_hub_id})
        return None

    issued = await guard.issued(ack.calibration_id)
    if issued is None:
        await _protocol_error(guard, "NOT_ISSUED", ack)
        return None
    if issued.hub_id != ack.hub_id:
        await _protocol_error(guard, "HUB_MISMATCH", ack, extra={"issued_hub_id": issued.hub_id})
        return None
    if (ack.epoch, ack.seq) != (issued.epoch, issued.seq):
        await _protocol_error(guard, "SEQUENCE_MISMATCH", ack, extra={"epoch": ack.epoch, "seq": ack.seq})
        return None
    if not _ack_signature_ok(payload, guard, ack.hub_id):
        await _protocol_error(guard, "BAD_ACK_SIGNATURE", ack)
        return None
    if issued.ack_consumed or not await guard.consume(ack.calibration_id):
        logger.info("duplicate calibration ack ignored", extra={"calibration_id": str(ack.calibration_id)})
        return None

    pending = await service.ports.calibration_attempts.get_pending(ack.calibration_id)
    if pending is None or pending.hub_id != ack.hub_id:
        logger.warning(
            "calibration ack for an attempt that is not pending on this hub, dropped",
            extra={"calibration_id": str(ack.calibration_id), "hub_id": ack.hub_id},
        )
        return None

    if ack.status == "REJECTED" and ack.reject_reason in PROTOCOL_REJECT_REASONS:
        await guard.close_protocol_error(ack.calibration_id, at=now)
        await _protocol_error(guard, f"HUB_REJECTED_{ack.reject_reason}", ack)
        return None

    if ack.status != "APPLIED":
        # REJECTED for a non-protocol reason (bounds, rate limit) or EXPIRED: the hub never applied a
        # correction -- `record_calibration_rejected` treats this as NO_CHANGE (S5.5.4's own default for
        # "this attempt did not help") rather than silently discarding it.
        outcome = CalibrationOutcome.NO_CHANGE
        await service.record_calibration_rejected(ack.hub_id, ack.calibration_id, now=now)
    else:
        post = OffsetVector(**ack.resulting_offsets.model_dump())
        outcome = await service.record_calibration_result(
            ack.hub_id,
            ack.calibration_id,
            pre=pending.measured_offset,
            post=post,
            corrected_tolerance=CORRECTED_TOLERANCE,
            now=now,
        )

    if outcome in _ESCALATING_OUTCOMES:
        await _open_escalation_work_order(service, ack.hub_id, ack.calibration_id, outcome, now=now)
    return outcome


async def _open_escalation_work_order(
    service: AssetHealthService,
    hub_id: str,
    calibration_id: UUID,
    outcome: CalibrationOutcome,
    *,
    now: datetime,
) -> None:
    existing = await service.ports.work_orders.open_for_hub(hub_id)
    if existing is not None:
        return
    await service.open_work_order(
        hub_id,
        severity=ESCALATION_WORK_ORDER_SEVERITY,
        evidence={"calibration_id": str(calibration_id), "outcome": outcome.value},
        now=now,
    )
