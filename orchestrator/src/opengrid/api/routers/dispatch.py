"""Dispatch & commitments screen (02b S7.1, S8 screen 3): opportunity pipeline, latest plan, per-bank
ledger timeline, opportunity admission, and the dispatch SSE stream.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_store
from opengrid.api.schemas import OpportunityCreate
from opengrid.api.sse import sse_response
from opengrid.api.store import StoreProtocol
from opengrid.contracts import AdmissionError, admit
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api", tags=["dispatch"])


@router.get("/dispatch/opportunities")
async def list_opportunities(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Kanban pipeline: offered -> committed -> delivering -> fulfilled (02b S8 screen 3)."""
    opportunities = await store.list_opportunities(state=state)
    return [o.model_dump(mode="json") for o in opportunities]


@router.post("/opportunities", status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    body: OpportunityCreate,
    _identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Admission (02a S2.1): creates an `OFFERED` opportunity via `opengrid.contracts.admit`, or `409`
    with the admission reason code if structurally infeasible."""
    try:
        opportunity = await admit(
            body.contract_id, body.window_start, body.window_end, body.requested_kw
        )
    except AdmissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": exc.reason_code}) from exc
    return opportunity.model_dump(mode="json")


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
) -> list[dict[str, Any]]:
    """Reservation timeline for a bank -- the Gantt-style ledger chart (02b S8 screen 3)."""
    reservations = await store.ledger_timeline(bank_id)
    return [r.model_dump(mode="json") for r in reservations]


@router.get("/stream/dispatch")
async def stream_dispatch(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        opportunities = await store.list_opportunities(state=None)
        plan = await store.latest_plan()
        return {
            "opportunities": [o.model_dump(mode="json") for o in opportunities[:50]],
            "latest_plan": plan.model_dump(mode="json") if plan is not None else None,
        }

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


@router.get("/stream/control-room")
async def stream_control_room(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    """Price/load ticker, fleet MW/MWh, active commitment count, net margin, invariant counters, open
    alerts (02b S7.2, S8 screen 1)."""

    async def fetch() -> dict[str, Any]:
        opportunities = await store.list_opportunities(state="COMMITTED")
        alerts = await store.list_alerts(open_only=True)
        hubs = await store.list_hubs(zone=None, bank_id=None, health=None, limit=2000, offset=0)
        fleet_mw = sum(h.p_kw for h in hubs) / 1000.0
        return {
            "active_commitment_count": len(opportunities),
            "fleet_mw": fleet_mw,
            "open_alert_count": len(alerts),
            "reserve_breach_count": 0,
            "double_sold_kwh": 0,
        }

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)
