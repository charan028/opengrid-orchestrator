"""AI copilot routes (issue #26): advisory answers over live state, and the agent's own status.

**Read-only and advisory.** Both routes depend on `require_viewer`; nothing here can command, approve or
release anything, and the copilot never reaches the database itself -- this router assembles the same
read-only snapshot the console's own GET routes serve, redacts it, and hands it over. The agent knows
tool results, not tables.

Every interaction is written to its own `ai` trace stream (issue #26 item 5) using the existing trace
API, so no migration is needed: the stream is new, the decision type is not.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from opengrid import ai_agent
from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.store import StoreProtocol
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api/ai", tags=["ai"])

#: How much of the board the copilot may look at in one answer. A cap, not a page size: an answer that
#: needs more than this is one the deterministic tier should not be attempting.
_MAX_OBLIGATIONS = 60


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: Which console screen the operator is on, for the answer's framing only. Never trusted for access.
    screen: str | None = Field(default=None, max_length=40)


class AskResponse(BaseModel):
    text: str
    tier: str
    citations: list[dict[str, str]]
    model: str | None = None
    confidence_label: str | None = None
    intent: str | None = None
    refusal_reason: str | None = None
    trace_id: str | None = None


async def _snapshot(store: StoreProtocol) -> dict[str, Any]:
    """The read-only view the copilot reasons over: the same data the health and dispatch GET routes
    serve, nothing more. Assembled here so the agent package never learns what a database is."""
    health: dict[str, Any] = {}
    obligations: list[dict[str, Any]] = []
    try:
        summary = await store.hub_health_summary()
        counts = dict(summary) if summary else {}
    except Exception as exc:
        logger.info("copilot snapshot: hub health unavailable (%s)", exc)
        counts = {}
    try:
        rows = await store.list_obligations(state=None)
        obligations = [o.model_dump(mode="json") for o in rows[:_MAX_OBLIGATIONS]]
    except Exception as exc:
        logger.info("copilot snapshot: obligations unavailable (%s)", exc)
    try:
        alerts = await store.list_alerts(open_only=True)
        health["alerts"] = [a.model_dump(mode="json") for a in alerts[:20]]
    except Exception as exc:
        logger.info("copilot snapshot: alerts unavailable (%s)", exc)
    return {"health": health, "hubs": {"counts": counts}, "obligations": obligations}


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_viewer)],
) -> AskResponse:
    """Answer one copilot question. Always returns 200: an unavailable or refused assistant is a normal
    answer the panel renders, never an error the console has to handle."""
    context = await _snapshot(store)
    answer = await ai_agent.service().ask(body.question, context)

    trace_id: str | None = None
    try:
        ref = await trace.append(
            f"ai:{identity.user}",
            "OPERATOR_ACTION",
            "AI_INTERACTION",
            {
                "question": body.question[:500],
                "screen": body.screen,
                "tier": answer.tier,
                "intent": answer.intent,
                "model": answer.model,
                "confidence": answer.confidence,
                "refusal_reason": answer.refusal_reason,
                "citations": [c.ref for c in answer.citations][:10],
                "user": identity.user,
            },
        )
        trace_id = str(getattr(ref, "trace_id", "") or "") or None
    except Exception as exc:
        logger.warning("copilot interaction could not be traced: %s", exc)

    return AskResponse(
        text=answer.text,
        tier=answer.tier,
        citations=[c.model_dump() for c in answer.citations],
        model=answer.model,
        confidence_label=answer.confidence_label,
        intent=answer.intent,
        refusal_reason=answer.refusal_reason,
        trace_id=trace_id,
    )


@router.get("/status")
async def status(_identity: Annotated[Identity, Depends(require_viewer)]) -> dict[str, Any]:
    """What works and how much budget is left (UI-DAT-05, shown on System Health)."""
    return ai_agent.service().status()
