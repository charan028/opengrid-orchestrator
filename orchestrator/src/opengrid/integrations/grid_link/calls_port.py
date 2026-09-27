"""`TollCallPort` over the shared core call function `opengrid.calls` (grid-link.md S6.1; D-29, D-34).

The grid link owns NO call logic: validation against the tolling obligation, the 90-minute cap, overlap
and idempotency, the as_deployment row, the call ledger, the trace, the operator_action row and the
operator alert all happen inside `opengrid.calls.issue_call` -- the same function the operator route and
the utility customer API call. This adapter only translates:

- origin `GRID_LINK`, principal `grid_link:<utility_id>`, idempotency key `ems:<ems_call_id>`;
- the link's discharge MAGNITUDE into the core's signed kW (`-setpoint_kw`, -discharge);
- `CallState` / `CallRefused` into `CallOutcome` (ACCEPTED -> ACCEPTED, RAMPING/DELIVERING -> ACTIVE,
  COMPLETED -> ENDED, REFUSED -> REJECTED with the core's reason code).
"""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.calls import (
    CallLimits,
    CallOrigin,
    CallRecord,
    CallRefused,
    CallRequest,
    CallState,
    CallStore,
    cancel_call,
    find_call_by_key,
    issue_call,
    status_of,
)
from opengrid.integrations.grid_link.model import CallOutcome, CallPhase
from opengrid.trace.store import TraceStore

__all__ = ["CoreTollCallPort", "idempotency_key", "principal_of"]

_PHASE: dict[CallState, CallPhase] = {
    CallState.ACCEPTED: CallPhase.ACCEPTED,
    CallState.RAMPING: CallPhase.ACTIVE,
    CallState.DELIVERING: CallPhase.ACTIVE,
    CallState.COMPLETED: CallPhase.ENDED,
    CallState.REFUSED: CallPhase.REJECTED,
}


def principal_of(utility_id: str) -> str:
    return f"grid_link:{utility_id}"


def idempotency_key(ems_call_id: int) -> str:
    return f"ems:{ems_call_id}"


def _refused(exc: CallRefused) -> CallOutcome:
    return CallOutcome(CallPhase.REJECTED, exc.reason_code)


class CoreTollCallPort:
    """Calls `opengrid.calls` with the grid link's identity."""

    def __init__(self, store: CallStore, trace: TraceStore, limits: CallLimits) -> None:
        self._store = store
        self._trace = trace
        self._limits = limits

    async def issue(
        self, utility_id: str, ems_call_id: int, setpoint_kw: float, duration_min: int
    ) -> CallOutcome:
        request = CallRequest(
            origin=CallOrigin.GRID_LINK,
            principal=principal_of(utility_id),
            reason=f"grid link toll call {ems_call_id}",
            duration_minutes=duration_min,
            utility_id=utility_id,
            requested_kw=-abs(setpoint_kw),
            idempotency_key=idempotency_key(ems_call_id),
        )
        try:
            record = await issue_call(self._store, self._trace, request, limits=self._limits)
        except CallRefused as exc:
            return _refused(exc)
        return await self._status_of(record)

    async def cancel(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        record = await find_call_by_key(self._store, principal_of(utility_id), idempotency_key(ems_call_id))
        if record is None:
            return CallOutcome(CallPhase.REJECTED, "R-CALL-NOT-FOUND")
        try:
            await cancel_call(
                self._store,
                self._trace,
                record.call_id,
                origin=CallOrigin.GRID_LINK,
                principal=principal_of(utility_id),
                utility_id=utility_id,
            )
        except CallRefused as exc:
            if exc.reason_code != "R-CALL-STATE":
                return _refused(exc)
        return await self._status_of(record)

    async def status(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        record = await find_call_by_key(self._store, principal_of(utility_id), idempotency_key(ems_call_id))
        if record is None:
            return CallOutcome(CallPhase.REJECTED, "R-CALL-NOT-FOUND")
        return await self._status_of(record)

    async def _status_of(self, record: CallRecord) -> CallOutcome:
        status = await status_of(self._store, record, limits=self._limits, now=datetime.now(UTC))
        delivered = status.delivered_kw
        return CallOutcome(
            phase=_PHASE[status.state],
            reason_code=status.call.reason_code,
            delivered_kw=round(max(0.0, -delivered), 3) if delivered is not None else None,
        )
