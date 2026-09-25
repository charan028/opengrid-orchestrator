"""Scoped safe stop, two-step confirmation (02b S7.1/S7.3), plus the single-step release. Engage goes
through `opengrid.safestop.engage`; release requires guardian co-signing per `opengrid.safestop`'s own
fail-closed contract (K8) -- `api` just forwards to it, it never re-implements the stop-only signing.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from opengrid.api.auth import Identity, require_operator
from opengrid.api.deps import get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalExpiredError, ProposalStore
from opengrid.api.schemas import (
    ProposalAccepted,
    SafestopConfirmResult,
    SafestopProposalRequest,
    SafestopReleaseRequest,
)
from opengrid.api.store import StoreProtocol
from opengrid.safestop import Scope as SafestopScope
from opengrid.safestop import engage, release
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/safestop", tags=["safestop"])

_SAFESTOP_PROPOSAL_KIND = "safestop"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def propose_safestop(
    body: SafestopProposalRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """Step 1 of 2 (02b S7.1): shape/role validation only."""
    scope_desc = f"{body.scope}" + (f"/{body.scope_id}" if body.scope_id else "")
    summary = f"Engage safe stop on {scope_desc} ({body.reason})"
    proposal = proposals.create(_SAFESTOP_PROPOSAL_KIND, body, summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=60.0)


@router.post("/{proposal_id}/confirm")
async def confirm_safestop(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> SafestopConfirmResult:
    """Step 2 of 2 (02b S7.1/S7.3): only now does `safestop` sign and broadcast the retained stop."""
    proposal = _pop_or_404(proposals, proposal_id, kind=_SAFESTOP_PROPOSAL_KIND)
    body: SafestopProposalRequest = proposal.body

    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="SAFE_STOP",
        event_class="SAFE_STOP_ENGAGE",
        payload={
            "decision_ref": f"{body.scope}:{body.scope_id or 'ALL'}",
            "scope": body.scope,
            "scope_id": body.scope_id,
            "reason": body.reason,
        },
    )
    await engage(SafestopScope(body.scope.upper()), body.scope_id or "ALL", body.reason, identity.user)
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="SAFE_STOP_ENGAGE",
        target_ref=f"{body.scope}:{body.scope_id or 'ALL'}",
        tier="ENGAGE",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    return SafestopConfirmResult(
        proposal_id=proposal_id,
        scope=body.scope,
        scope_id=body.scope_id,
        engaged=True,
        trace_id=trace_ref.trace_id,
    )


@router.post("/{scope}/{scope_id}/release")
async def release_safestop(
    scope: str,
    scope_id: str,
    body: SafestopReleaseRequest,
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Single step -- release is inherently reversible, engage is not (02b S7.1)."""
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="SAFE_STOP",
        event_class="SAFE_STOP_RELEASE",
        payload={
            "decision_ref": f"{scope}:{scope_id}",
            "scope": scope,
            "scope_id": scope_id,
            "reason": body.reason,
        },
    )
    await release(SafestopScope(scope.upper()), scope_id, identity.user)
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="SAFE_STOP_RELEASE",
        target_ref=f"{scope}:{scope_id}",
        tier="TIER2",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    return {"scope": scope, "scope_id": scope_id, "released": True, "trace_id": str(trace_ref.trace_id)}


def _pop_or_404(proposals: ProposalStore, proposal_id: UUID, *, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
