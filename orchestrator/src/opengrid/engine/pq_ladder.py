"""Executes the S5.4 corrective-action ladder on committed PQ-sensitive obligations (06-service-profiles-
and-power-quality.md S5.4/S5.5; K10, K13, K14).

`opengrid.allocator.pq_monitor` proposes the ladder from each cycle's measured PQ (fed through
`closed_loop_common.monitor_pq`); this module carries the per-obligation ladder state and acts on it:

- REBALANCE_PHASES / ADJUST_PF: internal. Phase balancing is the allocator's phase-balance weight, applied
  to every PQ-sensitive obligation each cycle; reactive (PF) setpoints are not in the command schema yet,
  so ADJUST_PF is recorded only.
- SUBSTITUTE_HUBS: the obligation's most-deviating hub (`pq_monitor.rank_hubs_by_deviation`) leaves its
  active set; the next cycle's `realize_obligation` moves its kW to eligible spares and records the swap
  with R-SUBSTITUTION. K13 unchanged: which hub delivers changes, never how much.
- RECALIBRATE: `assets.AssetHealthService.request_calibration` for a hub already taken off the delivery
  (S5.5.4: never on a live PQ-sensitive delivery; the service refuses otherwise). Once per hub per episode.
- EXCLUDE_HUB: the next most-deviating hub leaves the active set too.
- ESCALATE_AT_RISK: `contracts.set_obligation_at_risk` with R-PQ-DRIFT-AT-RISK (never a silent breach).

Recovery (the verdict back to NOMINAL) returns the hubs and clears the flag. Every step is traced before
the next cycle acts on it (K10); every resulting batch still passes the guardian's G-21..G-25. A missing
or stale measurement is recorded as missing, never as compliant.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opengrid.allocator.closed_loop_common import PQ_MEASUREMENT_MISSING, monitor_pq
from opengrid.allocator.pq_eligibility import HubPqCandidate
from opengrid.allocator.pq_monitor import (
    R_PQ_DRIFT_AT_RISK,
    CorrectionActionType,
    LadderState,
    rank_hubs_by_deviation,
)
from opengrid.core.pq import ComplianceState, PqEnvelopeLimits, PqMeasurement

logger = logging.getLogger(__name__)

R_PQ_DRIFT_RECOVERED = "R-PQ-DRIFT-RECOVERED"

_STATE_CHANGING = frozenset(
    {
        CorrectionActionType.ALERT,
        CorrectionActionType.SUBSTITUTE_HUBS,
        CorrectionActionType.RECALIBRATE,
        CorrectionActionType.EXCLUDE_HUB,
        CorrectionActionType.ESCALATE_AT_RISK,
    }
)


class TracePort(Protocol):
    async def append(
        self,
        stream_id: str,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None = None,
    ) -> object: ...


SetAtRisk = Callable[[UUID, bool, str, dict[str, object]], Awaitable[object]]
RequestCalibration = Callable[[str, datetime], Awaitable[bool]]
CandidateOf = Callable[[str], HubPqCandidate | None]


@dataclass
class _State:
    ladder: LadderState = field(default_factory=LadderState)
    excluded: set[str] = field(default_factory=set)
    calibration_requested: set[str] = field(default_factory=set)
    verdict: ComplianceState | None = None
    missing: bool = False
    at_risk: bool = False


def calibration_requester(service: Any, *, lease_ttl_s: float = 30.0) -> RequestCalibration:
    """S5.4 step 3 through the asset-health service (`AssetHealthService.request_calibration`), with the
    grid-synchronized nominal reference and the canonical firmware bounds -- the same inputs the asset
    drift sweep uses. The service itself refuses a hub still on a live PQ-sensitive delivery, inside the
    24 h rate limit, or without a fresh drift measurement (returns `None` -> False here)."""
    from opengrid.assets.calibration import CalibrationReference
    from opengrid.core.pq import DEFAULT_FIRMWARE_CALIBRATION_BOUNDS
    from opengrid.core.pq.constants import NOMINAL_FREQ_HZ
    from opengrid.pq_ingest.aggregation import NOMINAL_VOLTAGE_V

    async def _request(hub_id: str, now: datetime) -> bool:
        reference = CalibrationReference(
            phase_deg=0.0,
            freq_hz=NOMINAL_FREQ_HZ,
            amplitude_v=NOMINAL_VOLTAGE_V,
            sync_source="ntp_disciplined",
        )
        candidate = await service.request_calibration(
            hub_id,
            reference=reference,
            bounds=DEFAULT_FIRMWARE_CALIBRATION_BOUNDS,
            epoch=1,
            seq=int(now.timestamp()),
            now=now,
            lease_ttl_s=lease_ttl_s,
        )
        return candidate is not None

    return _request


class PqLadderExecutor:
    """One ladder state per obligation, carried across cycles (one instance per og-engine process)."""

    def __init__(
        self,
        trace: TracePort,
        *,
        set_at_risk: SetAtRisk,
        candidate_of: CandidateOf,
        request_calibration: RequestCalibration | None = None,
    ) -> None:
        self._trace = trace
        self._set_at_risk = set_at_risk
        self._candidate_of = candidate_of
        self._request_calibration = request_calibration
        self._states: dict[str, _State] = {}

    def excluded_by_obligation(self) -> dict[str, frozenset[str]]:
        return {oid: frozenset(s.excluded) for oid, s in self._states.items() if s.excluded}

    def forget(self, active_obligation_ids: set[str]) -> None:
        """Drop the state of obligations no longer monitored (window over, released)."""
        for oid in set(self._states) - active_obligation_ids:
            del self._states[oid]

    async def observe(
        self,
        obligation_id: str,
        measurement: PqMeasurement | None,
        limits: PqEnvelopeLimits,
        allocated_hub_ids: Sequence[str],
        *,
        spare_hubs: int,
        now: datetime,
    ) -> list[CorrectionActionType]:
        """Advance the ladder by one measurement and act on the proposed steps. Returns the steps acted on."""
        state = self._states.setdefault(obligation_id, _State())
        check = monitor_pq(
            obligation_id,
            measurement,
            limits,
            state.ladder,
            now.timestamp(),
            has_substitution_candidate=spare_hubs > 0,
            recalibration_eligible=bool(state.excluded - state.calibration_requested),
        )
        state.ladder = check.ladder_state
        if check.monitor is None:
            if PQ_MEASUREMENT_MISSING in check.flags and not state.missing:
                state.missing = True
                await self._record(obligation_id, "PQ_MEASUREMENT_MISSING", [PQ_MEASUREMENT_MISSING], {})
            return []
        state.missing = False
        verdict = check.monitor.verdict
        previous, state.verdict = state.verdict, verdict

        if verdict == ComplianceState.NOMINAL:
            if previous not in (None, ComplianceState.NOMINAL) or state.excluded or state.at_risk:
                await self._recover(obligation_id, state)
            return []

        acted: list[CorrectionActionType] = []
        worst_first = self._ranked(allocated_hub_ids, limits)
        for action in check.monitor.actions:
            kind = action.action
            if kind == CorrectionActionType.ALERT:
                if previous != ComplianceState.WARN:
                    acted.append(kind)
            elif kind in (CorrectionActionType.REBALANCE_PHASES, CorrectionActionType.ADJUST_PF):
                acted.append(kind)  # internal: the cycle's phase-balance weight; PF recorded only
            elif kind in (CorrectionActionType.SUBSTITUTE_HUBS, CorrectionActionType.EXCLUDE_HUB):
                target = next((h for h in worst_first if h not in state.excluded), None)
                if target is not None:
                    state.excluded.add(target)
                    acted.append(kind)
            elif kind == CorrectionActionType.RECALIBRATE:
                if await self._recalibrate(state, now):
                    acted.append(kind)
            elif kind == CorrectionActionType.ESCALATE_AT_RISK and not state.at_risk:
                state.at_risk = True
                await self._flag(obligation_id, True, R_PQ_DRIFT_AT_RISK, check.monitor.worst_dimension)
                acted.append(kind)
        # Traced when something changed (or on the first breach cycle), not every 2 s while a breach lasts.
        if acted and (_STATE_CHANGING & set(acted) or state.ladder.cycles_in_breach == 1):
            await self._record(
                obligation_id,
                "PQ_LADDER",
                sorted({a.reason_code for a in check.monitor.actions}),
                {
                    "verdict": verdict.value,
                    "worst_dimension": check.monitor.worst_dimension,
                    "cycles_in_breach": state.ladder.cycles_in_breach,
                    "actions": [a.value for a in acted],
                    "excluded_hub_ids": sorted(state.excluded),
                },
            )
        return acted

    def _ranked(self, hub_ids: Sequence[str], limits: PqEnvelopeLimits) -> tuple[str, ...]:
        candidates = [c for h in hub_ids if (c := self._candidate_of(h)) is not None]
        ranked = rank_hubs_by_deviation(candidates, limits)
        unranked = tuple(sorted(h for h in hub_ids if h not in set(ranked)))
        return ranked + unranked

    async def _recalibrate(self, state: _State, now: datetime) -> bool:
        if self._request_calibration is None:
            return False
        requested = False
        for hub_id in sorted(state.excluded - state.calibration_requested):
            state.calibration_requested.add(hub_id)
            try:
                requested = await self._request_calibration(hub_id, now) or requested
            except Exception:
                logger.exception("calibration request failed", extra={"hub_id": hub_id})
        return requested

    async def _recover(self, obligation_id: str, state: _State) -> None:
        returned = sorted(state.excluded)
        state.excluded.clear()
        state.calibration_requested.clear()
        if state.at_risk:
            state.at_risk = False
            await self._flag(obligation_id, False, R_PQ_DRIFT_RECOVERED, None)
        await self._record(
            obligation_id, "PQ_RECOVERED", [R_PQ_DRIFT_RECOVERED], {"returned_hub_ids": returned}
        )

    async def _flag(self, obligation_id: str, at_risk: bool, reason: str, dimension: str | None) -> None:
        try:
            await self._set_at_risk(
                UUID(obligation_id), at_risk, reason, {"cause": "pq_drift", "dimension": dimension}
            )
        except Exception:
            logger.exception("could not set PQ at-risk flag", extra={"obligation_id": obligation_id})

    async def _record(
        self, obligation_id: str, event_class: str, reason_codes: list[str], payload: dict[str, Any]
    ) -> None:
        try:
            await self._trace.append(
                f"pq-ladder-{obligation_id}",
                "ALERT",
                event_class,
                {"obligation_id": obligation_id, **payload},
                reason_codes=reason_codes,
            )
        except Exception:
            logger.exception("could not trace PQ ladder step", extra={"obligation_id": obligation_id})
