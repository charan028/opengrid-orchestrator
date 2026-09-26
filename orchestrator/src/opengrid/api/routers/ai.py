"""AI copilot routes (issue #26): advisory answers over live state, and the agent's own status.

**Read-only and advisory.** Both routes depend on `require_viewer`; nothing here can command, approve or
release anything. The copilot's snapshot is assembled in-process through the console's own read-only GET
handlers (`GET /og/api/health`, `GET /og/api/dispatch/opportunities`) acting as the dedicated
`og-ai-agent` viewer identity, over a store view that exposes only the read methods those handlers use.
The agent itself never sees the store, the pool or a table: it gets the handlers' JSON.

Every interaction is written to its own `ai:<user>` trace stream using the existing trace API (no
migration: the stream is new, the decision type is not). If that write fails, the operator is told the
assistant is unavailable rather than shown an untraced answer.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from opengrid import ai_agent
from opengrid.api.auth import Identity, Role, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.routers import dispatch as dispatch_routes
from opengrid.api.routers import health as health_routes
from opengrid.api.store import StoreProtocol
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api/ai", tags=["ai"])

#: The service identity the snapshot reads run as: a viewer, so it can never pass `require_operator`.
AI_AGENT_IDENTITY = Identity("og-ai-agent", Role.VIEWER)

#: The only store methods the snapshot's GET handlers may reach. Every one is a SELECT.
READ_METHODS: frozenset[str] = frozenset(
    {
        "count_active_commitments",
        "feed_statuses",
        "health_snapshot",
        "hub_health_summary",
        "list_alerts",
        "list_hubs",
        "list_obligations",
        "profitability_summary",
    }
)

#: How much of the board the copilot may look at in one answer. A cap, not a page size.
_MAX_OBLIGATIONS = 60
_MAX_ALERTS = 20


class ReadOnlyStore:
    """A view of the API store that exposes only `READ_METHODS`. Anything else -- every write, ack or
    command method -- is an `AttributeError`, so a handler change that starts writing fails loudly here
    instead of writing as the copilot."""

    __slots__ = ("_store",)

    def __init__(self, store: StoreProtocol) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name not in READ_METHODS:
            raise AttributeError(f"the copilot's read-only store view has no {name!r}")
        return getattr(self._store, name)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: Which console screen the operator is on, for the answer's framing only. Never trusted for access.
    screen: str | None = Field(default=None, max_length=40)


class AskResponse(BaseModel):
    text: str
    tier: str
    citations: list[dict[str, str]]
    model: str | None = None
    provider: str | None = None
    confidence_label: str | None = None
    intent: str | None = None
    refusal_reason: str | None = None
    trace_id: str | None = None


def _optional_pool(request: Request) -> AsyncConnectionPool | None:
    """Handed only to `GET /og/api/health`'s own handler, whose pool reads are its invariant and
    degraded-mode SELECTs. None in unit tests that never run the app lifespan."""
    return getattr(request.app.state, "pool", None)


async def snapshot(store: StoreProtocol, pool: AsyncConnectionPool | None) -> dict[str, Any]:
    """The read-only view the copilot reasons over: exactly what the console's own GET handlers return
    to a viewer, nothing more.

    A read that fails is named in `unavailable` rather than left as an empty value, so the copilot says
    "can't verify right now" instead of "nothing wrong". The invariant counters count as unavailable
    whenever the health route could not measure them (`invariants_checked_at` is None: its own read
    failed or there is no pool), because the route then reports zeros that were never measured.
    `commitment_switches` is left out: the health route reports it as a constant, not a measurement.
    """
    reader = cast(StoreProtocol, ReadOnlyStore(store))
    unavailable: list[str] = []
    health: dict[str, Any] = {}
    obligations: list[dict[str, Any]] = []
    try:
        health = await health_routes.get_health(store=reader, pool=pool)
    except Exception as exc:
        logger.info("copilot snapshot: health unavailable (%s)", type(exc).__name__)
        unavailable.append("health")
    try:
        rows = await dispatch_routes.list_opportunities(store=reader, _identity=AI_AGENT_IDENTITY, state=None)
        obligations = rows[:_MAX_OBLIGATIONS]
    except Exception as exc:
        logger.info("copilot snapshot: obligations unavailable (%s)", type(exc).__name__)
        unavailable.append("obligations")
    health_view: dict[str, Any] = {}
    if "health" not in unavailable:
        if health.get("invariants_checked_at") is None:
            unavailable.append("invariants")
        else:
            health_view.update(
                {
                    key: health[key]
                    for key in ("reserve_breaches", "double_sold_kwh", "lock_violations")
                    if key in health
                }
            )
        health_view["alerts"] = list(health.get("alerts") or [])[:_MAX_ALERTS]
    view: dict[str, Any] = {
        "health": health_view,
        "hubs": {"counts": dict(health.get("hub_health_counts") or {})},
        "unavailable": unavailable,
    }
    if "obligations" not in unavailable:
        view["obligations"] = obligations
    return view


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_viewer)],
    pool: Annotated[AsyncConnectionPool | None, Depends(_optional_pool)],
) -> AskResponse:
    """Answer one copilot question. Always returns 200: an unavailable or refused assistant is a normal
    answer the panel renders, never an error the console has to handle."""
    context = await snapshot(store, pool)

    async def write_trace(payload: dict[str, Any]) -> str | None:
        ref = await trace.append(f"ai:{identity.user}", "OPERATOR_ACTION", "AI_INTERACTION", payload)
        return str(ref.trace_id)

    answer = await ai_agent.service().ask(
        body.question, context, trace=write_trace, screen=body.screen, user=identity.user
    )
    return AskResponse(
        text=answer.text,
        tier=answer.tier,
        citations=[c.model_dump() for c in answer.citations],
        model=answer.model,
        provider=answer.provider,
        confidence_label=answer.confidence_label,
        intent=answer.intent,
        refusal_reason=answer.refusal_reason,
        trace_id=answer.trace_id,
    )


@router.get("/status")
async def status(_identity: Annotated[Identity, Depends(require_viewer)]) -> dict[str, Any]:
    """What works and how much budget is left (UI-DAT-05, shown on System Health)."""
    return ai_agent.service().status()
