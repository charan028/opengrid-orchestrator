"""Dispatch & commitments screen (02b S7.1, S8 screen 3): opportunity pipeline, latest plan, per-bank
ledger timeline, opportunity admission, and the dispatch SSE stream.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import AsyncConnectionPool
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_store
from opengrid.api.schemas import OpportunityCreate
from opengrid.api.sse import sse_response
from opengrid.api.store import StoreProtocol
from opengrid.contracts import AdmissionError, admit
from opengrid.invariants import InvariantsSummary, read_summary
from opengrid.platform.config import Config

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
        }

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)
