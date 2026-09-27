"""The one dispatch-call path (D-29, D-33): `issue_call`, `cancel_call`, `cancel_deployment`,
`call_status`, `list_calls`. The operator route (`POST /og/api/dispatch/as-deployments`), the utility
customer API, the grid link and the ERCOT deployment poller all call these; the checks live in
`opengrid.calls.rules` and nowhere else.

Every call is traced before it takes effect (K10) with its origin and principal, recorded in the call
ledger (`og.dispatch_call`, refusals included) and audited in `og.operator_action`. A call that did not
come from an operator raises an operator alert when it arrives, and every non-operator refusal raises
one too. An attempt on another utility's obligation answers NOT FOUND (never "forbidden": existence is
not leaked) and is traced as AUTHZ_DENY.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from opengrid.authz.enforce import audit_deny
from opengrid.authz.policy import Decision
from opengrid.calls import rules
from opengrid.calls.models import (
    SOURCE_FOR_ORIGIN,
    AwardView,
    CallKind,
    CallOrigin,
    CallOutcome,
    CallRecord,
    CallRefused,
    CallRequest,
    CallState,
    CallStatus,
)
from opengrid.calls.ports import CallStore, IdempotencyKeyTakenError, OverlapError
from opengrid.calls.rules import CallLimits, Refusal
from opengrid.health.model import AlertSeverity
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

TRACE_DECISION_TYPE = "OPERATOR_ACTION"
EVENT_CALL = "DISPATCH_CALL"
EVENT_CALL_REFUSED = "DISPATCH_CALL_REFUSED"
EVENT_CALL_END = "DISPATCH_CALL_END"
AUTHZ_ACTION_CALL = "dispatch.call"
ALERT_UTILITY_CALL = "ALR-UTILITY-CALL"
ALERT_UTILITY_CALL_REFUSED = "ALR-UTILITY-CALL-REFUSED"
ALERT_CALL = "ALR-DISPATCH-CALL"
ALERT_CALL_REFUSED = "ALR-DISPATCH-CALL-REFUSED"
#: A utility call's deployment reason starts with this marker (Dispatch UI, reports).
UTILITY_CALL_REASON_PREFIX = "utility call"
_UTILITY_ORIGINS = frozenset({CallOrigin.UTILITY, CallOrigin.GRID_LINK})
_ONE_HOUR = timedelta(hours=1)
_ONE_DAY = timedelta(days=1)


def _stream(principal: str) -> str:
    return f"dispatch_call:{principal}"


async def issue_call(
    store: CallStore,
    trace: TraceStore,
    request: CallRequest,
    *,
    limits: CallLimits,
    now: datetime | None = None,
) -> CallRecord:
    """Issue one call. Returns the ACCEPTED record (or, for an idempotent replay, the original record
    with `replayed=True`). Raises `CallRefused` on every refusal; a refusal is traced, recorded (except a
    rate-limit or idempotency conflict, which would let a caller flood the ledger) and alerted."""
    now = now or datetime.now(UTC)
    if request.idempotency_key is not None:
        replay = await _replay(store, request)
        if replay is not None:
            return replay
    start, end = rules.call_window(request, now)
    rate = rules.check_rate(
        await store.count_calls_since(request.principal, now - _ONE_HOUR),
        await store.count_calls_since(request.principal, now - _ONE_DAY),
        limits,
    )
    if rate is not None:
        await _trace_refusal(trace, request, rate, obligation_id=None)
        await _alert_refusal(store, request, rate)
        raise CallRefused(rate.reason_code, rate.detail, rate.http_status)
    award, refusal = await _resolve(store, trace, request, start, end, now)
    if refusal is not None or award is None:
        refusal = refusal or rules.not_found_refusal()
        return await _refuse(store, trace, request, refusal, start, end, award=award)
    return await _accept(store, trace, request, award, start, end)


async def _replay(store: CallStore, request: CallRequest) -> CallRecord | None:
    found = await store.find_by_key(request.principal, request.idempotency_key or "")
    if found is None:
        return None
    record, fingerprint = found
    if fingerprint != request.fingerprint():
        conflict = rules.idempotency_conflict_refusal()
        raise CallRefused(conflict.reason_code, conflict.detail, conflict.http_status, record)
    replayed = record.model_copy(update={"replayed": True})
    if replayed.outcome is CallOutcome.REFUSED:
        raise CallRefused(
            replayed.reason_code or rules.R_NOT_FOUND,
            replayed.detail or "",
            rules.status_for(replayed.reason_code),
            replayed,
        )
    return replayed


async def _resolve(
    store: CallStore, trace: TraceStore, request: CallRequest, start: datetime, end: datetime, now: datetime
) -> tuple[AwardView | None, Refusal | None]:
    """The called obligation, in the caller's scope, and the first failed check (if any)."""
    early = rules.check_request(request, start, end, now)
    if early is not None:
        return None, early
    obligation_id = request.obligation_id
    if obligation_id is None:
        if request.utility_id is None:
            return None, Refusal(rules.R_NO_OBLIGATION, "obligation_id is required", rules.HTTP_UNPROCESSABLE)
        obligation_id = await store.find_toll_obligation(request.utility_id, start)
        if obligation_id is None:
            return None, Refusal(
                rules.R_NOT_FOUND,
                "no deployable tolling obligation covers the call's start",
                rules.HTTP_NOT_FOUND,
            )
    award = await store.get_award(obligation_id)
    if award is None:
        return None, rules.not_found_refusal()
    if request.utility_id is not None and award.utility_id != request.utility_id:
        await _audit_out_of_scope(trace, request.principal, request.utility_id)
        return None, rules.not_found_refusal()
    return award, rules.check_award(award, request, start, end)


async def _audit_out_of_scope(trace: TraceStore, principal: str, utility_id: str) -> None:
    """AUTHZ_DENY trace for a reach into another utility's obligation or call (answered 404)."""
    await audit_deny(
        trace,
        actor=principal,
        action=AUTHZ_ACTION_CALL,
        decision=Decision(False, f"resource outside utility {utility_id!r}", audited=True),
        resource=None,
    )


def _record(
    request: CallRequest,
    start: datetime,
    end: datetime,
    *,
    outcome: CallOutcome,
    award: AwardView | None,
    refusal: Refusal | None = None,
) -> CallRecord:
    kind = rules.deployment_kind(award.service_type, award.variant) if award is not None else None
    return CallRecord(
        call_id=uuid4(),
        outcome=outcome,
        origin=request.origin,
        principal=request.principal,
        reason=request.reason,
        start_at=start,
        end_at=end,
        duration_minutes=rules.duration_minutes_of(start, end) if end > start else 1,
        reason_code=refusal.reason_code if refusal else None,
        detail=refusal.detail if refusal else None,
        obligation_id=award.obligation_id if award else None,
        utility_id=request.utility_id or (award.utility_id if award else None),
        kind=kind,
        requested_kw=request.requested_kw,
        committed_kw=award.committed_kw if award else None,
        idempotency_key=request.idempotency_key,
    )


async def _refuse(
    store: CallStore,
    trace: TraceStore,
    request: CallRequest,
    refusal: Refusal,
    start: datetime,
    end: datetime,
    *,
    award: AwardView | None,
) -> CallRecord:
    ref = await _trace_refusal(trace, request, refusal, obligation_id=award.obligation_id if award else None)
    record = _record(
        request, start, max(end, start), outcome=CallOutcome.REFUSED, award=award, refusal=refusal
    ).model_copy(update={"trace_id": ref})
    try:
        record = await store.insert_refused(record, fingerprint=request.fingerprint())
    except IdempotencyKeyTakenError:
        replay = await _replay(store, request)
        if replay is not None:
            return replay
    await _alert_refusal(store, request, refusal)
    raise CallRefused(refusal.reason_code, refusal.detail, refusal.http_status, record)


async def _trace_refusal(
    trace: TraceStore, request: CallRequest, refusal: Refusal, *, obligation_id: UUID | None
) -> UUID:
    ref = await trace.append(
        _stream(request.principal),
        TRACE_DECISION_TYPE,
        EVENT_CALL_REFUSED,
        {
            **_request_payload(request),
            "obligation_id": str(obligation_id) if obligation_id else None,
            "reason_code": refusal.reason_code,
            "detail": refusal.detail,
        },
        [refusal.reason_code],
    )
    return ref.trace_id


def _request_payload(request: CallRequest) -> dict[str, Any]:
    return {
        "origin": request.origin.value,
        "principal": request.principal,
        "utility_id": request.utility_id,
        "requested_kw": request.requested_kw,
        "start_at": request.start_at.isoformat() if request.start_at else None,
        "end_at": request.end_at.isoformat() if request.end_at else None,
        "duration_minutes": request.duration_minutes,
        "idempotency_key": request.idempotency_key,
        "reason": request.reason,
    }


def _deployment_reason(record: CallRecord) -> str:
    if record.kind is CallKind.UTILITY_CALL and not record.reason.startswith(UTILITY_CALL_REASON_PREFIX):
        return f"{UTILITY_CALL_REASON_PREFIX}: {record.reason}"
    return record.reason


async def _accept(
    store: CallStore,
    trace: TraceStore,
    request: CallRequest,
    award: AwardView,
    start: datetime,
    end: datetime,
) -> CallRecord:
    draft = _record(request, start, end, outcome=CallOutcome.ACCEPTED, award=award)
    draft = draft.model_copy(update={"reason": _deployment_reason(draft)})
    ref = await trace.append(
        _stream(request.principal),
        TRACE_DECISION_TYPE,
        EVENT_CALL,
        {
            **_request_payload(request),
            "call_id": str(draft.call_id),
            "decision_ref": str(award.obligation_id),
            "kind": draft.kind.value if draft.kind else None,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "committed_kw": award.committed_kw,
        },
    )
    draft = draft.model_copy(update={"trace_id": ref.trace_id})
    source = SOURCE_FOR_ORIGIN[request.origin]
    try:
        record = await store.insert_accepted(draft, fingerprint=request.fingerprint(), source=source)
    except OverlapError:
        return await _refuse(store, trace, request, rules.overlap_refusal(), start, end, award=award)
    except IdempotencyKeyTakenError:
        replay = await _replay(store, request)
        if replay is None:
            raise
        return replay
    await _alert_accepted(store, record)
    return record


async def _safe_alert(
    store: CallStore, rule: str, severity: AlertSeverity, summary: str, detail: dict[str, Any]
) -> None:
    """An alert is a notification, never a precondition: a failed alert write is logged (degraded path
    visible in logs and health) and the call stands."""
    try:
        await store.raise_alert(rule, severity, summary, detail)
    except Exception:
        logger.exception("dispatch call alert could not be raised", extra={"rule": rule})


async def _alert_accepted(store: CallStore, record: CallRecord) -> None:
    if record.origin is CallOrigin.OPERATOR:
        return
    target = record.target_kw
    await _safe_alert(
        store,
        ALERT_UTILITY_CALL if record.kind is CallKind.UTILITY_CALL else ALERT_CALL,
        "info",
        f"{record.origin.value} call from {record.principal}: "
        f"{'' if target is None else f'{target:.0f} kW '}for {record.duration_minutes} min "
        f"from {record.start_at.isoformat(timespec='minutes')}",
        {
            "call_id": str(record.call_id),
            "obligation_id": str(record.obligation_id),
            "utility_id": record.utility_id,
            "origin": record.origin.value,
            "scope_kind": "dispatch_call",
            "scope_ref": str(record.call_id),
        },
    )


async def _alert_refusal(store: CallStore, request: CallRequest, refusal: Refusal) -> None:
    if request.origin is CallOrigin.OPERATOR:
        return
    rule = ALERT_UTILITY_CALL_REFUSED if request.origin in _UTILITY_ORIGINS else ALERT_CALL_REFUSED
    await _safe_alert(
        store,
        rule,
        "warning",
        f"{request.origin.value} call from {request.principal} refused: {refusal.reason_code}",
        {
            "reason_code": refusal.reason_code,
            "detail": refusal.detail,
            "origin": request.origin.value,
            "utility_id": request.utility_id,
            "scope_kind": "principal",
            "scope_ref": request.principal,
        },
    )


async def _own_call(
    store: CallStore, trace: TraceStore, call_id: UUID, principal: str, utility_id: str | None
) -> CallRecord:
    record = await store.get_call(call_id)
    if record is not None and utility_id is not None and record.utility_id != utility_id:
        await _audit_out_of_scope(trace, principal, utility_id)
        record = None
    if record is None:
        raise CallRefused(rules.R_NOT_FOUND, "no such call", rules.HTTP_NOT_FOUND)
    return record


async def cancel_call(
    store: CallStore,
    trace: TraceStore,
    call_id: UUID,
    *,
    origin: CallOrigin,
    principal: str,
    utility_id: str | None = None,
    end_at: datetime | None = None,
    now: datetime | None = None,
) -> CallRecord:
    """Cancel (no `end_at`, or one not after now) or shorten (a later `end_at`, never past the call's
    own end) an accepted call. `utility_id` scopes it to that utility's calls (anything else: NOT FOUND).
    Traced before it takes effect; the allocator returns the obligation to its 0 kW hold next cycle."""
    now = now or datetime.now(UTC)
    record = await _own_call(store, trace, call_id, principal, utility_id)
    if record.outcome is CallOutcome.REFUSED or record.deployment_id is None:
        raise CallRefused(
            rules.R_ALREADY_ENDED, "the call was refused; nothing to cancel", rules.HTTP_CONFLICT
        )
    target_end = max(end_at or now, now)
    if target_end > record.end_at:
        raise CallRefused(
            rules.R_CANNOT_EXTEND, "a call can be shortened, never extended", rules.HTTP_CONFLICT
        )
    await trace.append(
        _stream(principal),
        TRACE_DECISION_TYPE,
        EVENT_CALL_END,
        {
            "call_id": str(call_id),
            "decision_ref": str(record.deployment_id),
            "origin": origin.value,
            "principal": principal,
            "end_at": target_end.isoformat(),
        },
    )
    if not await store.end_deployment(record.deployment_id, end_at=target_end, now=now):
        raise CallRefused(rules.R_ALREADY_ENDED, "the call has already ended", rules.HTTP_CONFLICT)
    if origin is not CallOrigin.OPERATOR and record.kind is CallKind.UTILITY_CALL:
        await _safe_alert(
            store,
            ALERT_UTILITY_CALL,
            "info",
            f"{origin.value} {'cancelled' if target_end <= now else 'shortened'} call {call_id}",
            {"call_id": str(call_id), "scope_kind": "dispatch_call", "scope_ref": f"{call_id}:end"},
        )
    updated = await store.get_call(call_id)
    return updated or record


async def cancel_deployment(
    store: CallStore,
    trace: TraceStore,
    deployment_id: UUID,
    *,
    origin: CallOrigin,
    principal: str,
    now: datetime | None = None,
) -> bool:
    """The operator's "stop deploy": ends a deployment now, whether or not it came through the call
    ledger (market/scenario rows written before migration 0047 have no call). False if none active."""
    record = await store.get_call_by_deployment(deployment_id)
    if record is not None:
        try:
            await cancel_call(store, trace, record.call_id, origin=origin, principal=principal, now=now)
        except CallRefused:
            return False
        return True
    now = now or datetime.now(UTC)
    await trace.append(
        _stream(principal),
        TRACE_DECISION_TYPE,
        EVENT_CALL_END,
        {"decision_ref": str(deployment_id), "origin": origin.value, "principal": principal},
    )
    return await store.end_deployment(deployment_id, end_at=now, now=now)


async def call_status(
    store: CallStore,
    trace: TraceStore,
    call_id: UUID,
    *,
    principal: str,
    utility_id: str | None = None,
    limits: CallLimits | None = None,
    now: datetime | None = None,
) -> CallStatus:
    """State and MEASURED delivery of a call (see `CallState`): delivered kW (signed, < 0 = discharge) and
    kWh from the call's `og.delivery_record` (`opengrid.delivery`, D-38), never the allocator's grants."""
    now = now or datetime.now(UTC)
    record = await _own_call(store, trace, call_id, principal, utility_id)
    return await status_of(store, record, limits=limits or CallLimits(), now=now)


def running_state(
    target_kw: float | None, delivered_kw: float | None, *, ramping_fraction: float
) -> CallState:
    """A running call's state from measured delivery: DELIVERING at or above `ramping_fraction` of the
    target (both signed, discharge < 0), RAMPING below it, ACTIVE when unmeasured or without a target."""
    if delivered_kw is None or target_kw is None or target_kw >= 0.0:
        return CallState.ACTIVE
    return CallState.DELIVERING if -delivered_kw >= ramping_fraction * -target_kw else CallState.RAMPING


async def status_of(store: CallStore, record: CallRecord, *, limits: CallLimits, now: datetime) -> CallStatus:
    """Status of a record, from its measured delivery (`CallStore.delivery`)."""
    if record.outcome is CallOutcome.REFUSED or record.obligation_id is None:
        return CallStatus(
            call=record, state=CallState.REFUSED, delivered_kw=None, delivered_kwh=None, as_of=now
        )
    effective_end = min(record.end_at, record.cancelled_at or record.end_at)
    if now < record.start_at:
        state = CallState.COMPLETED if record.cancelled_at is not None else CallState.ACCEPTED
        return CallStatus(
            call=record, state=state, delivered_kw=None, delivered_kwh=0.0, as_of=now, granted_kwh=0.0
        )
    measured = (
        await store.measured_delivery(record.deployment_id) if record.deployment_id is not None else None
    )
    granted = await store.granted(record.obligation_id, record.start_at, min(now, effective_end))
    delivered_kw = measured.delivered_kw if measured is not None else None
    if now >= effective_end:
        state = CallState.COMPLETED
    else:
        state = running_state(record.target_kw, delivered_kw, ramping_fraction=limits.ramping_fraction)
    return CallStatus(
        call=record,
        state=state,
        delivered_kw=delivered_kw,
        delivered_kwh=measured.discharged_kwh if measured is not None else 0.0,
        as_of=now,
        delivery=measured,
        granted_kw=-granted.last_kw if granted.last_kw is not None else None,
        granted_kwh=granted.kwh,
    )


async def list_calls(
    store: CallStore,
    *,
    utility_id: str | None = None,
    principal: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[CallRecord]:
    """Call history, newest first, scoped by utility and/or principal."""
    return await store.list_calls(utility_id=utility_id, principal=principal, since=since, limit=limit)


async def find_call_by_key(store: CallStore, principal: str, idempotency_key: str) -> CallRecord | None:
    """The call a principal made with this idempotency key (e.g. the grid link's EMS call id)."""
    found = await store.find_by_key(principal, idempotency_key)
    return found[0] if found is not None else None
