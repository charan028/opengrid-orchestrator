"""Utility customer API v1 (D-33): a utility (e.g. Austin Energy, `AUSTIN_ENERGY`) issues and follows its
own toll calls automatically under `/og/api/customer/v1/utility/` (D-29: REGULATED_CAPACITY variant
TOLLING, 90 min product, held at 0 kW until called, discharge calls only).

Every endpoint is scoped to the caller's own utility_id (`utility_identity`). An obligation or call of
another utility answers 404 exactly like a missing one, and the attempt is traced as AUTHZ_DENY. Calls
go through `opengrid.calls` -- the same core function and the same checks as the operator's
`POST /og/api/dispatch/as-deployments` -- with origin UTILITY and the caller as principal.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from opengrid.api.call_errors import call_refused_http
from opengrid.api.deps import get_call_store, get_config, get_trace_store
from opengrid.calls import (
    CallLimits,
    CallOrigin,
    CallRecord,
    CallRefused,
    CallRequest,
    CallStore,
    call_status,
    cancel_call,
    issue_call,
    list_calls,
)
from opengrid.calls.models import MAX_CALL_MINUTES, MAX_TEXT_LEN, AwardView
from opengrid.calls.service import status_of
from opengrid.core.timeutil import MARKET_TZ
from opengrid.customer_api.utility_identity import (
    ACTION_CALL,
    ACTION_CANCEL,
    ACTION_READ,
    UtilityIdentity,
    require_utility_action,
)
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

API_VERSION = "v1"
router = APIRouter(prefix=f"/og/api/customer/{API_VERSION}/utility", tags=["utility"])

Reader = Annotated[UtilityIdentity, Depends(require_utility_action(ACTION_READ))]
Caller = Annotated[UtilityIdentity, Depends(require_utility_action(ACTION_CALL))]
Canceller = Annotated[UtilityIdentity, Depends(require_utility_action(ACTION_CANCEL))]
Store = Annotated[CallStore, Depends(get_call_store)]
Trace = Annotated[TraceStore, Depends(get_trace_store)]
Cfg = Annotated[Config, Depends(get_config)]

DEFAULT_OBLIGATION_DAYS = 2
DEFAULT_HISTORY_DAYS = 30
MAX_HISTORY_ROWS = 500


class UtilityCallSubmission(BaseModel):
    """A toll call. `kw` is signed (+charge/-discharge): a call is discharge only, so it is negative.
    `start_at` omitted = now. `obligation_id` omitted = the obligation whose reservation window covers
    the start. `idempotency_key` is required: resending the same key returns the original call."""

    kw: float
    duration_minutes: int = Field(ge=1, le=MAX_CALL_MINUTES)
    idempotency_key: str = Field(min_length=1, max_length=MAX_TEXT_LEN)
    start_at: datetime | None = None
    obligation_id: UUID | None = None
    reason: str = Field(default="utility toll call", min_length=1, max_length=MAX_TEXT_LEN)


class UtilityCancelSubmission(BaseModel):
    """Omit `end_at` (or give one not after now) to cancel; a later `end_at` shortens the call."""

    end_at: datetime | None = None


def _today_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Today's local (America/Chicago) calendar day, as UTC."""
    day = now.astimezone(MARKET_TZ).date()
    start = datetime.combine(day, time.min, tzinfo=MARKET_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _obligation_view(award: AwardView, active: CallRecord | None) -> dict[str, Any]:
    return {
        "obligation_id": str(award.obligation_id),
        "utility_id": award.utility_id,
        "service_type": award.service_type,
        "variant": award.variant,
        "state": award.state,
        "committed_kw": award.committed_kw,
        "max_call_minutes": award.duration_minutes,
        "window_start": award.window_start.isoformat() if award.window_start else None,
        "window_end": award.window_end.isoformat() if award.window_end else None,
        # D-29: held at 0 kW until called; while called, the call's target (signed, < 0).
        "held_kw": active.target_kw if active is not None else 0.0,
        "active_call_id": str(active.call_id) if active is not None else None,
    }


@router.get("/me")
async def me(who: Reader) -> dict[str, str]:
    return {"user": who.user, "utility_id": who.utility_id, "api_version": API_VERSION}


@router.get("/obligations")
async def my_obligations(
    who: Reader, store: Store, days: Annotated[int, Query(ge=1, le=14)] = DEFAULT_OBLIGATION_DAYS
) -> dict[str, Any]:
    """My tolling obligations from the start of today (local) for `days` days, and today's reservation
    window (the obligation whose window falls today, if any), each with its active call."""
    now = datetime.now(UTC)
    day_start, day_end = _today_bounds(now)
    awards = await store.toll_obligations(who.utility_id, day_start, day_start + timedelta(days=days))
    calls = await list_calls(store, utility_id=who.utility_id, since=day_start - timedelta(days=1))
    active = {
        c.obligation_id: c
        for c in calls
        if c.deployment_id is not None and c.cancelled_at is None and c.end_at > now
    }
    views = [_obligation_view(a, active.get(a.obligation_id)) for a in awards]
    today = [
        v
        for a, v in zip(awards, views, strict=True)
        if a.window_start is not None and day_start <= a.window_start < day_end
    ]
    return {"utility_id": who.utility_id, "obligations": views, "today": today[0] if today else None}


@router.post("/calls", status_code=status.HTTP_201_CREATED, response_model=None)
async def issue_toll_call(
    body: UtilityCallSubmission, who: Caller, store: Store, trace: Trace, cfg: Cfg
) -> dict[str, Any] | JSONResponse:
    """Issue a toll call (201), or return the original call for a replayed idempotency key (200).
    Refusals answer the core's status and `{"reason_code", "detail", "call_id"}`."""
    request = CallRequest(
        origin=CallOrigin.UTILITY,
        principal=who.user,
        reason=body.reason,
        duration_minutes=body.duration_minutes,
        obligation_id=body.obligation_id,
        utility_id=who.utility_id,
        requested_kw=body.kw,
        start_at=body.start_at,
        idempotency_key=body.idempotency_key,
    )
    limits = CallLimits.from_config(cfg)
    try:
        record = await issue_call(store, trace, request, limits=limits)
    except CallRefused as exc:
        raise call_refused_http(exc) from exc
    body_out = (await status_of(store, record, limits=limits, now=datetime.now(UTC))).public()
    if record.replayed:
        return JSONResponse(status_code=status.HTTP_200_OK, content=body_out)
    return body_out


@router.get("/calls")
async def my_calls(
    who: Reader,
    store: Store,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_ROWS)] = 100,
) -> dict[str, Any]:
    """My call history (accepted and refused), newest first; default the last 30 days."""
    since = since or datetime.now(UTC) - timedelta(days=DEFAULT_HISTORY_DAYS)
    rows = await list_calls(store, utility_id=who.utility_id, since=since, limit=limit)
    return {"calls": [r.public() for r in rows]}


@router.get("/calls/{call_id}")
async def my_call(call_id: UUID, who: Reader, store: Store, trace: Trace, cfg: Cfg) -> dict[str, Any]:
    """Status: ACCEPTED, RAMPING / DELIVERING (window running; measured delivery below / at target), ACTIVE
    (running, not measured yet: `delivery_state` UNMEASURED), COMPLETED or REFUSED (with its reason), plus
    the MEASURED delivered kW (signed, < 0 = discharge) and kWh (`delivery_measured: true`, D-38)."""
    try:
        result = await call_status(
            store,
            trace,
            call_id,
            principal=who.user,
            utility_id=who.utility_id,
            limits=CallLimits.from_config(cfg),
        )
    except CallRefused as exc:
        raise call_refused_http(exc) from exc
    return result.public()


@router.post("/calls/{call_id}/cancel")
async def cancel_my_call(
    call_id: UUID, body: UtilityCancelSubmission, who: Canceller, store: Store, trace: Trace, cfg: Cfg
) -> dict[str, Any]:
    """Cancel an accepted call now, or shorten it to `end_at` (never extend it)."""
    try:
        record = await cancel_call(
            store,
            trace,
            call_id,
            origin=CallOrigin.UTILITY,
            principal=who.user,
            utility_id=who.utility_id,
            end_at=body.end_at,
        )
    except CallRefused as exc:
        raise call_refused_http(exc) from exc
    return (
        await status_of(store, record, limits=CallLimits.from_config(cfg), now=datetime.now(UTC))
    ).public()
