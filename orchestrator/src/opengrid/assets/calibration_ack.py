"""opengrid.assets.calibration_ack -- ingests `CalibrationAck` messages (interfaces/mqtt/
calibration_ack.schema.json, 07-delivery/06 S6.7) and turns each one into an `AssetHealthService.
record_calibration_result` call, opening a maintenance work order (S5.5.5) when the outcome is
uncorrectable.

This module owns the ack HALF of the calibration MQTT loop's orchestrator side; the guardian owns
signing and publishing the command (see this package's README for the exact `evaluate_and_sign_
calibration` gap and the poll/publish wiring the live-path agent still needs to add). The engine-owned
MQTT ingest loop routes `<root>/ack/cal/<hub_id>` here -- see the README for the one wiring line.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opengrid.assets.service import AssetHealthService
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


async def handle_calibration_ack(
    service: AssetHealthService, payload: dict[str, Any], *, now: datetime | None = None
) -> CalibrationOutcome | None:
    """Validates `payload` as a `CalibrationAck` (the caller validates against `calibration_ack.
    schema.json` first via `opengrid.platform.mqtt.validate_payload`, per BUILD.md S5a -- this model
    validation is defense in depth, not a second copy of that check), looks up the pending attempt's
    pre-calibration measured offset (never trusts the ack's own claim of what was "before"), classifies
    the outcome, and opens a maintenance work order when it is `NO_CHANGE`/`WORSE_ROLLED_BACK` (S5.5.4:
    "recalibration is attempted at most once per drift episode before escalating"). Returns `None` (and
    logs) if the ack does not match any known pending attempt -- a stale/duplicate/forged ack must never
    silently mutate asset state."""
    ack = CalibrationAck.model_validate(payload)
    now = now or datetime.now(UTC)

    pending = await service.ports.calibration_attempts.get_pending(ack.calibration_id)
    if pending is None:
        logger.warning(
            "calibration ack for unknown calibration_id, dropped",
            extra={"calibration_id": str(ack.calibration_id), "hub_id": ack.hub_id},
        )
        return None
    if pending.hub_id != ack.hub_id:
        logger.warning(
            "calibration ack hub_id mismatch, dropped",
            extra={
                "calibration_id": str(ack.calibration_id),
                "ack_hub_id": ack.hub_id,
                "expected_hub_id": pending.hub_id,
            },
        )
        return None

    if ack.status != "APPLIED":
        # REJECTED (bad signature/expired/bounds) or EXPIRED: the hub never applied a correction --
        # `record_calibration_rejected` treats this as NO_CHANGE (S5.5.4's own default for "this
        # attempt did not help") rather than silently discarding it.
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
