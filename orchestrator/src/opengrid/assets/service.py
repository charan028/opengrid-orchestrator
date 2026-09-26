"""`AssetHealthService`: orchestrates the S5.5 asset-health/calibration workflow over the ports in
`opengrid.assets.ports`, the pure lifecycle in `opengrid.assets.state_machine` and the calibration
builder in `opengrid.assets.calibration`. Pure orchestration -- no direct Postgres/MQTT import here
(BUILD.md S5a); `opengrid.assets.repo` supplies real Postgres-backed ports, tests supply in-memory fakes.

Every transition is traced (K10/K11) before being treated as final, and the calibration ladder's own
"never on a live PQ-sensitive delivery" discipline (S5.4 step 2, S5.5.4) is enforced here as the PRIMARY
check (`SensitiveGrantPort`) -- the guardian's `check_g25_calibration_safety` (`opengrid.guardian.
pq_checks`) is the INDEPENDENT re-check that actually gates signing, per the K2/K14 shape. This service
never signs a `CalibrationCommand`; it only builds the candidate and asks the guardian to sign it (K3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from opengrid.assets.calibration import (
    CalibrationCandidate,
    CalibrationReference,
    build_calibration_candidate,
    calibration_due,
)
from opengrid.assets.ports import (
    AssetEventRepoPort,
    AssetHealthRepoPort,
    AssetTracePort,
    CalibrationAttemptRepoPort,
    DriftObservationPort,
    SensitiveGrantPort,
    WorkOrderRepoPort,
)
from opengrid.assets.state_machine import DriftEvent, next_asset_state
from opengrid.core.pq import (
    CalibrationBounds,
    CalibrationOutcome,
    OffsetVector,
    classify_calibration_outcome,
    is_persistent_drift,
)
from opengrid.core.pq.constants import CALIBRATION_RECURRENCE_WINDOW_DAYS_DEFAULT
from opengrid.guardian.pq_ports import AssetState


@dataclass(frozen=True, slots=True)
class AssetHealthPorts:
    """Bundles every port `AssetHealthService` needs (mirrors `guardian.ports.GuardianPorts`)."""

    drift: DriftObservationPort
    asset_health: AssetHealthRepoPort
    calibration_attempts: CalibrationAttemptRepoPort
    work_orders: WorkOrderRepoPort
    asset_events: AssetEventRepoPort
    trace: AssetTracePort
    sensitive_grants: SensitiveGrantPort


#: R2 incident proposal (2026-09-26, see `repo.py`'s "LIVE BUG FIX" and `runner.py`'s "SWEEP DISABLED"):
#: in addition to `core.pq.is_persistent_drift`'s existing ">= 90% of the window" rule, also require the
#: most recent `N` summaries (chronological tail, `DriftObservationWindow.exceeded_per_summary` is now
#: sorted ascending by `repo.PgDriftObservationRepo`) to ALL individually exceed the WATCH threshold --
#: closes exactly the failure class this incident exposed (a single wrong/stale reading, from an
#: ordering bug or any other future defect, driving an escalation on its own). 3 consecutive summaries at
#: the default 2-10s telemetry cadence is tens of seconds, not a new multi-minute detection delay, while
#: still being immune to any ONE bad reading. `evaluate_drift`'s `min_consecutive_exceedances` parameter
#: is OPT-IN (default `None`, today's exact pre-incident behaviour) -- this is a proposal for the owner
#: to review and enable when the sweep is re-enabled, not a silent behaviour change.
PROPOSED_MIN_CONSECUTIVE_EXCEEDANCES = 3


def _tail_consecutive_exceedances_ok(exceeded_per_summary: Sequence[bool], n: int) -> bool:
    """True iff the last `n` entries of `exceeded_per_summary` (chronological order, oldest first) are
    ALL `True`. `n <= 0` is treated as "no additional requirement" (always True) -- a caller passing a
    non-positive `n` has opted out, not asked for an impossible bar."""
    if n <= 0:
        return True
    tail = list(exceeded_per_summary)[-n:]
    return len(tail) == n and all(tail)


@dataclass
class AssetHealthService:
    ports: AssetHealthPorts
    calibration_min_interval_s: float = 86_400.0
    recurrence_window_days: float = CALIBRATION_RECURRENCE_WINDOW_DAYS_DEFAULT

    async def evaluate_drift(
        self, hub_id: str, *, now: datetime, min_consecutive_exceedances: int | None = None
    ) -> AssetState | None:
        """S5.5.1: advance `hub_id`'s asset state from its current rolling observation window. Returns
        `None` (no judgement made) when there is not yet enough telemetry -- never treated as "OK".

        `min_consecutive_exceedances` (opt-in, default `None` = off): additionally require the most
        recent `N` summaries to ALL individually exceed the WATCH threshold, on top of `core.pq.
        is_persistent_drift`'s existing window-fraction rule -- see `PROPOSED_MIN_CONSECUTIVE_
        EXCEEDANCES`'s module-level docstring (the R2 incident's proposed re-enable threshold)."""
        window = await self.ports.drift.observation_window(hub_id)
        if window is None:
            return None
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None

        persistent = (
            is_persistent_drift(window.exceeded_per_summary)
            and _tail_consecutive_exceedances_ok(
                window.exceeded_per_summary, min_consecutive_exceedances or 0
            )
            and not window.correlates_with_fleet_event
        )

        if record.asset_state == "OK":
            if not persistent:
                return "OK"
            new_state = await self._advance(hub_id, "OK", DriftEvent.PERSISTENT_DRIFT_DETECTED, now)
            last_corrected = await self.ports.calibration_attempts.last_corrected_at(hub_id)
            if last_corrected is not None and (now - last_corrected) <= timedelta(
                days=self.recurrence_window_days
            ):
                new_state = await self._advance(hub_id, new_state, DriftEvent.RECURRENCE_WITHIN_WINDOW, now)
            return new_state

        if record.asset_state == "WATCH" and not persistent:
            return await self._advance(hub_id, "WATCH", DriftEvent.WATCH_CLEARED, now)

        return record.asset_state

    async def request_calibration(
        self,
        hub_id: str,
        *,
        reference: CalibrationReference,
        bounds: CalibrationBounds,
        now: datetime,
        lease_ttl_s: float,
        epoch: int | None = None,
        seq: int | None = None,
    ) -> CalibrationCandidate | None:
        """S5.4 step 3 / S5.5.4: build the calibration candidate for the guardian to sign, or `None` if
        the ladder's own primary checks refuse it (a live PQ-sensitive delivery still on this hub, the
        24h rate limit, or no fresh drift measurement to correct) -- in every `None` case the drift
        proceeds toward escalation on its own timeline rather than being forced (S6.7).

        `epoch`/`seq` are optional and normally omitted: the guardian assigns the real per-hub (epoch, seq)
        on `og.calibration_command` when it signs, and the PENDING `og.calibration_attempt` row never
        carries a placeholder (#30)."""
        if await self.ports.sensitive_grants.has_active_non_default_envelope_grant(hub_id):
            return None
        last_attempt = await self.ports.calibration_attempts.last_attempt_epoch_s(hub_id)
        if not calibration_due(last_attempt, now.timestamp(), self.calibration_min_interval_s):
            return None
        window = await self.ports.drift.observation_window(hub_id)
        if window is None:
            return None

        candidate = build_calibration_candidate(
            hub_id=hub_id,
            measured_offset=window.latest_measured_offset,
            bounds=bounds,
            reference=reference,
            epoch=epoch,
            seq=seq,
            issued_at=now,
            lease_ttl_s=lease_ttl_s,
        )
        await self.ports.calibration_attempts.record_attempt(
            calibration_id=candidate.calibration_id,
            hub_id=hub_id,
            requested_at=now,
            reference_phase_deg=reference.phase_deg,
            reference_freq_hz=reference.freq_hz,
            reference_amplitude_v=reference.amplitude_v,
            measured_offset=window.latest_measured_offset,
            correction=candidate.correction,
            command_batch_id=None,
        )
        await self.ports.trace.append(
            "CALIBRATION_ATTEMPT",
            {"calibration_id": str(candidate.calibration_id), "hub_id": hub_id, "outcome": "PENDING"},
        )
        return candidate

    async def record_calibration_result(
        self,
        hub_id: str,
        calibration_id: UUID,
        *,
        pre: OffsetVector,
        post: OffsetVector,
        corrected_tolerance: OffsetVector,
        now: datetime,
    ) -> CalibrationOutcome:
        """S5.5.4: classify the verification capture (`opengrid.core.pq.classify_calibration_outcome`,
        never re-implemented) and advance the asset-state lifecycle accordingly -- `WORSE_ROLLED_BACK`
        moves straight to `QUARANTINED` (S5.5.2's own definition includes a failed/rolled-back attempt)."""
        outcome = classify_calibration_outcome(pre, post, corrected_tolerance=corrected_tolerance)
        await self.ports.calibration_attempts.record_outcome(calibration_id, outcome, verified_at=now)

        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return outcome

        event = {
            CalibrationOutcome.CORRECTED: DriftEvent.CALIBRATION_CORRECTED,
            CalibrationOutcome.IMPROVED: DriftEvent.CALIBRATION_IMPROVED,
            CalibrationOutcome.NO_CHANGE: DriftEvent.CALIBRATION_NO_CHANGE,
            CalibrationOutcome.WORSE_ROLLED_BACK: DriftEvent.CALIBRATION_WORSE_ROLLED_BACK,
        }[outcome]
        if outcome in (CalibrationOutcome.CORRECTED, CalibrationOutcome.IMPROVED):
            await self.ports.asset_health.record_recalibration(hub_id, at=now)
        await self._advance(hub_id, record.asset_state, event, now)
        return outcome

    async def record_calibration_rejected(
        self, hub_id: str, calibration_id: UUID, *, now: datetime
    ) -> AssetState | None:
        """A `CalibrationAck` with `status != 'APPLIED'` (`REJECTED`/`EXPIRED` -- bad signature, expired
        lease, or the hub itself refused the bounds): the hub never applied any correction, so there is
        no "post" offset to classify against a "pre" one. Treated as `NO_CHANGE` (S5.5.4's own default
        for "this attempt did not help") -- never silently discarded, since an attempt that never even
        reached the hub still counts toward the "at most one attempt per drift episode" escalation rule."""
        await self.ports.calibration_attempts.record_outcome(
            calibration_id, CalibrationOutcome.NO_CHANGE, verified_at=now
        )
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None
        return await self._advance(hub_id, record.asset_state, DriftEvent.CALIBRATION_NO_CHANGE, now)

    async def record_false_positive_reset(
        self, hub_id: str, *, reason: str, now: datetime
    ) -> AssetState | None:
        """An explicit, audited operator/owner override: undoes a `WATCH`/`DEGRADED`/`QUARANTINED`/
        `AWAITING_REPLACEMENT` escalation that turned out not to reflect a real drift (R2 incident,
        2026-09-26: see `repo.py`'s "LIVE BUG FIX" comment). Never available from `RECOMMISSIONING`
        (`opengrid.assets.state_machine`'s transition table has no entry for it there -- `next_asset_
        state` raises `InvalidAssetTransitionError`, which this method deliberately does NOT catch: a
        replaced unit is not a "false positive" to undo, and a caller trying it on one has a bug worth
        surfacing, not silencing).

        Closes the hub's open work order as `CANCELLED` (never `CLOSED` -- a cancelled order was never
        legitimately actionable, unlike one closed after a real replacement) and traces an
        `OPERATOR_ACTION` decision carrying `reason` and the cancelled work order id, IN ADDITION to the
        `ASSET_STATE_TRANSITION` `_advance` already writes -- two independent audit trail entries for a
        correction this consequential."""
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None

        work_order = await self.ports.work_orders.open_for_hub(hub_id)
        new_state = await self._advance(
            hub_id, record.asset_state, DriftEvent.OPERATOR_FALSE_POSITIVE_RESET, now
        )
        if work_order is not None:
            await self.ports.work_orders.close(
                work_order.work_order_id, closed_at=now, technician_notes=reason, status="CANCELLED"
            )
        await self.ports.trace.append(
            "OPERATOR_ACTION",
            {
                "hub_id": hub_id,
                "action": "ASSET_FALSE_POSITIVE_RESET",
                "reason": reason,
                "cancelled_work_order_id": str(work_order.work_order_id) if work_order else None,
                "from_state": record.asset_state,
                "to_state": new_state,
            },
        )
        return new_state

    async def quarantine(self, hub_id: str, *, now: datetime) -> AssetState | None:
        """S5.5.5: escalate a `DEGRADED` unit to `QUARANTINED` ahead of a scheduled replacement, when
        the drift's severity warrants pulling it from all grid-facing dispatch immediately."""
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None
        return await self._advance(hub_id, record.asset_state, DriftEvent.QUARANTINE_ESCALATED, now)

    async def open_work_order(
        self, hub_id: str, *, severity: str, evidence: dict[str, object], now: datetime
    ) -> UUID:
        """S5.5.5: open a `maintenance_work_order` carrying the drift/calibration evidence that
        justified it (waveform-summary ids, calibration_attempt ids -- caller assembles `evidence`)."""
        return await self.ports.work_orders.open(hub_id, severity=severity, evidence=evidence, opened_at=now)

    async def schedule_replacement(self, hub_id: str, *, now: datetime) -> AssetState | None:
        """S7.5's `REPLACE_INVERTER` control-plane action / a technician scheduling the physical swap:
        `DEGRADED`/`QUARANTINED` -> `AWAITING_REPLACEMENT`."""
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None
        return await self._advance(hub_id, record.asset_state, DriftEvent.REPLACEMENT_SCHEDULED, now)

    async def record_inverter_replaced(
        self,
        hub_id: str,
        *,
        old_serial: str | None,
        new_serial: str,
        old_firmware: str | None,
        new_firmware: str,
        now: datetime,
    ) -> AssetState | None:
        """S5.5.5: the physical hardware action -- `og.asset_event(event_type='INVERTER_REPLACED')`,
        never described as a dispatch "swap" (front matter item 7). Closes the hub's open work order and
        moves `AWAITING_REPLACEMENT` -> `RECOMMISSIONING`."""
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None
        work_order = await self.ports.work_orders.open_for_hub(hub_id)
        await self.ports.asset_events.record_inverter_replaced(
            hub_id,
            work_order_id=work_order.work_order_id if work_order else None,
            old_serial=old_serial,
            new_serial=new_serial,
            old_firmware=old_firmware,
            new_firmware=new_firmware,
            at=now,
        )
        if work_order is not None:
            await self.ports.work_orders.close(work_order.work_order_id, closed_at=now, technician_notes=None)
        return await self._advance(hub_id, record.asset_state, DriftEvent.INVERTER_REPLACED, now)

    async def verify_recommissioning(self, hub_id: str, *, passed: bool, now: datetime) -> AssetState | None:
        """S5.5.6: a passing verification capture returns the unit to `OK` and resets `last_estimated_
        at`; a failing one leaves it `RECOMMISSIONING` (K7: never a silent return to dispatch)."""
        record = await self.ports.asset_health.get(hub_id)
        if record is None:
            return None
        event = DriftEvent.RECOMMISSIONING_VERIFIED if passed else DriftEvent.RECOMMISSIONING_FAILED
        new_state = await self._advance(hub_id, record.asset_state, event, now)
        if passed:
            await self.ports.asset_health.reset_characterization(hub_id, last_estimated_at=now)
            await self.ports.asset_events.record_recommissioned(hub_id, at=now)
        return new_state

    async def _advance(
        self, hub_id: str, current: AssetState, event: DriftEvent, now: datetime
    ) -> AssetState:
        new_state = next_asset_state(current, event)
        record = await self.ports.asset_health.get(hub_id)
        consecutive = record.consecutive_correctable_drifts if record is not None else 0
        consecutive = consecutive + 1 if event == DriftEvent.CALIBRATION_CORRECTED else 0
        await self.ports.asset_health.set_state(
            hub_id, new_state, since=now, consecutive_correctable_drifts=consecutive
        )
        await self.ports.asset_events.record_state_transition(
            hub_id, from_state=current, to_state=new_state, reason_code=event.value, at=now
        )
        await self.ports.trace.append(
            "ASSET_STATE_TRANSITION",
            {
                "hub_id": hub_id,
                "from_state": current,
                "to_state": new_state,
                "reason_code": event.value,
            },
        )
        return new_state
