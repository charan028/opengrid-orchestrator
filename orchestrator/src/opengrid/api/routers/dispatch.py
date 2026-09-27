"""Dispatch & commitments screen (02b S7.1, S8 screen 3): opportunity pipeline, latest plan, per-bank
ledger timeline, opportunity admission, and the dispatch SSE stream.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg_pool import AsyncConnectionPool
from pydantic import AwareDatetime, BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.call_errors import call_refused_http
from opengrid.api.deps import get_call_store, get_config, get_store, get_trace_store
from opengrid.api.schemas import OpportunityCreate
from opengrid.api.sse import sse_response
from opengrid.api.store import StoreProtocol
from opengrid.calls import (
    CallLimits,
    CallOrigin,
    CallRefused,
    CallRequest,
    CallStore,
    cancel_deployment,
    issue_call,
    list_calls,
)
from opengrid.calls.models import MAX_CALL_MINUTES
from opengrid.contracts import AdmissionError, admit
from opengrid.invariants import InvariantsSummary, read_summary
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api", tags=["dispatch"])

_UNAVAILABLE_INVARIANTS_SUMMARY = InvariantsSummary.unavailable()


@router.get("/dispatch/opportunities")
async def list_opportunities(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Kanban pipeline: offered -> committed -> delivering -> fulfilled (02b S8 screen 3). Returns
    `Obligation` rows, not `Opportunity` rows: `Opportunity.state` never reaches committed/delivering/
    fulfilled (02a S1.4 vs S1.5) -- see `StoreProtocol.list_obligations`."""
    obligations = await store.list_obligations(state=state)
    return [o.model_dump(mode="json") for o in obligations]


@router.post("/opportunities", status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    body: OpportunityCreate,
    _identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Admission (02a S2.1): creates an `OFFERED` opportunity via `opengrid.contracts.admit`, or `409`
    with the admission reason code if structurally infeasible."""
    try:
        opportunity = await admit(body.contract_id, body.window_start, body.window_end, body.requested_kw)
    except AdmissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": exc.reason_code}) from exc
    return opportunity.model_dump(mode="json")


class AsDeploymentCreate(BaseModel):
    """Operator-triggered deployment of ONE held award: an ERCOT_AS award (the demo's stand-in for an
    ERCOT deployment instruction) or, D-29, a utility's call on a tolling obligation. A fleet-wide
    deployment is refused: it would discharge every held award at once, which needs a two-person approval
    this route does not provide (review finding, 2026-09-26). `requested_kw` is signed (+charge/
    -discharge, so negative); omitted = the full committed kW. `start_at` omitted = now."""

    obligation_id: UUID | None = None
    scope: str | None = None
    duration_minutes: int = Field(default=15, ge=1, le=MAX_CALL_MINUTES)
    reason: str = Field(min_length=1, max_length=200)
    requested_kw: float | None = None
    start_at: AwareDatetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


@router.post("/dispatch/as-deployments", status_code=status.HTTP_201_CREATED)
async def create_as_deployment(
    body: AsDeploymentCreate,
    call_store: Annotated[CallStore, Depends(get_call_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    cfg: Annotated[Config, Depends(get_config)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Deploy one held award now (or at `start_at`) through the one call path, `opengrid.calls.issue_call`
    (D-33): the same checks as a utility's own call -- deployable state, the obligation's own product
    cap (ECRS 60, Non-Spin 240, TOLLING 90 min; refused when it has none), no overlap, discharge only,
    within the committed kW and the reservation window, idempotency and rate limits. Traced before it
    takes effect (K10), with origin OPERATOR."""
    if body.obligation_id is None and not body.scope:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="obligation_id is required")
    request = CallRequest(
        origin=CallOrigin.OPERATOR,
        principal=identity.user,
        reason=body.reason,
        duration_minutes=body.duration_minutes,
        obligation_id=body.obligation_id,
        requested_kw=body.requested_kw,
        start_at=body.start_at,
        idempotency_key=body.idempotency_key,
        fleet_wide=body.obligation_id is None,
    )
    try:
        record = await issue_call(call_store, trace_store, request, limits=CallLimits.from_config(cfg))
    except CallRefused as exc:
        raise call_refused_http(exc) from exc
    return {
        "call_id": str(record.call_id),
        "deployment_id": str(record.deployment_id),
        "obligation_id": str(record.obligation_id),
        "start_at": record.start_at.isoformat(),
        "end_at": record.end_at.isoformat(),
        "trace_id": str(record.trace_id),
        "kind": record.kind.value if record.kind else None,
        "origin": record.origin.value,
        "requested_kw": record.requested_kw,
        "replayed": record.replayed,
    }


@router.get("/dispatch/as-deployments")
async def list_as_deployments(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> list[dict[str, Any]]:
    """Active deployments with their origin (`source`: OPERATOR, UTILITY, GRID_LINK, ERCOT, MARKET_SIM,
    SCENARIO) and who asked (`requested_by`), for the Dispatch screen."""
    rows = await store.list_active_as_deployments()
    return [{k: (str(v) if isinstance(v, UUID | datetime) else v) for k, v in row.items()} for row in rows]


@router.get("/dispatch/calls")
async def list_dispatch_calls(
    call_store: Annotated[CallStore, Depends(get_call_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    utility_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """The call ledger (every origin, refusals included), newest first."""
    return [r.public() for r in await list_calls(call_store, utility_id=utility_id, limit=limit)]


@router.delete("/dispatch/as-deployments/{deployment_id}")
async def end_as_deployment(
    deployment_id: UUID,
    call_store: Annotated[CallStore, Depends(get_call_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """End a deployment early (any origin): the award returns to a 0 kW capacity hold on the next cycle."""
    ended = await cancel_deployment(
        call_store, trace_store, deployment_id, origin=CallOrigin.OPERATOR, principal=identity.user
    )
    if not ended:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no active deployment with that id")
    return {"deployment_id": str(deployment_id), "ended": True}


@router.get("/dispatch/plan/latest")
async def latest_plan(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any] | None:
    """Latest selector plan and its rationale/value terms (02b S7.1)."""
    plan = await store.latest_plan()
    return plan.model_dump(mode="json") if plan is not None else None


@router.get("/ledger/{bank_id}/timeline")
async def ledger_timeline(
    bank_id: str,
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """Reservation timeline for a bank -- the Gantt-style ledger chart plus the grants/substitutions
    feed and commitment-lock events (02b S8 screen 3), matching
    `opengrid.ui.routes.dispatch.dispatch_page`'s `{"reservations", "grants", "commitments",
    "bank_capacity_kw"}` shape."""
    reservations = await store.ledger_timeline(bank_id)
    grants = await store.list_grants(bank_id)
    commitments = await store.list_commitments()
    bank = await store.get_bank(bank_id)
    return {
        "bank_id": bank_id,
        "reservations": [r.model_dump(mode="json") for r in reservations],
        "grants": [g.model_dump(mode="json") for g in grants],
        "commitments": [c.model_dump(mode="json") for c in commitments],
        "bank_capacity_kw": bank.kva_rating if bank is not None else 0.0,
    }


@router.get("/stream/dispatch")
async def stream_dispatch(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        obligations = await store.list_obligations(state=None)
        plan = await store.latest_plan()
        return {
            "opportunities": [o.model_dump(mode="json") for o in obligations[:50]],
            "latest_plan": plan.model_dump(mode="json") if plan is not None else None,
        }

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


def _get_optional_pool(request: Request) -> AsyncConnectionPool | None:
    """Like `opengrid.api.deps.get_pool`, but tolerant of `app.state.pool` never having been set: this
    router's own unit test suite overrides every OTHER dependency and never runs the real app lifespan
    (`opengrid.api.app._lifespan`) that would set it. A missing pool degrades this stream's invariant
    counters to "not yet measured" rather than failing the whole control-room stream over an unrelated
    dependency (K7: degrade, don't trip)."""
    return getattr(request.app.state, "pool", None)


async def _invariants_summary(pool: AsyncConnectionPool | None) -> InvariantsSummary:
    if pool is None:
        return _UNAVAILABLE_INVARIANTS_SUMMARY
    try:
        return await read_summary(pool)
    except Exception:
        logger.warning("opengrid.invariants summary read failed", exc_info=True)
        return _UNAVAILABLE_INVARIANTS_SUMMARY


@router.get("/stream/control-room")
async def stream_control_room(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    # Trailing + defaulted (unlike every other dependency here): `tests/unit/api/test_sse.py` calls this
    # coroutine directly (bypassing FastAPI's dependency injection) with only `store`/`cfg`/`_identity`
    # given by keyword, so `pool` needs a real Python default to remain callable that way.
    pool: Annotated[AsyncConnectionPool | None, Depends(_get_optional_pool)] = None,
) -> EventSourceResponse:
    """Price/load ticker, fleet MW/MWh, active commitment count, net margin, invariant counters, open
    alerts (02b S7.2, S8 screen 1). The invariant counters are a measured read of `og.invariant_check`
    (`opengrid.invariants.read_summary`, populated by that package's periodic K1/K2 checks), not a
    constant."""

    async def fetch() -> dict[str, Any]:
        active_commitment_count = await store.count_active_commitments()
        alerts = await store.list_alerts(open_only=True)
        hubs = await store.list_hubs(zone=None, bank_id=None, health=None, limit=2000, offset=0)
        fleet_mw = sum(h["p_kw"] for h in hubs) / 1000.0
        invariants = await _invariants_summary(pool)
        return {
            "active_commitment_count": active_commitment_count,
            "fleet_mw": fleet_mw,
            "open_alert_count": len(alerts),
            "reserve_breach_count": invariants.reserve_breaches,
            "double_sold_kwh": invariants.double_sold_kwh,
            "invariants_checked_at": invariants.as_of.isoformat() if invariants.as_of else None,
            # when these counts/sums were read: the tiles' age counts from here, not from page load
            "as_of": datetime.now(UTC).isoformat(),
        }

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)
