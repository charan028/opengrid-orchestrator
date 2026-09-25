"""Scoped safe stop, two-step confirmation (02b S7.1/S7.3).

`opengrid.safestop` is its own process holding the stop-only Ed25519 key (K8: it must work even if
`og-engine`/`og-guardian` are down, and its blast radius on the broker/key material stays minimal) --
`api` never imports or configures it directly. Instead `api` is the "og-api (once built)" side of the
LISTEN/NOTIFY request intake `opengrid.safestop.pg_backend`/`opengrid.safestop.main` already document:
NOTIFY `REQUEST_CHANNEL` with `{"action": "PROPOSE"|"CONFIRM", "proposal_id", "scope", "scope_ref",
"reason", "initiator_ref"}`; `og-safestop`'s own `ConfirmationBroker` only calls `engage()` when a
CONFIRM lands within its `confirm_window_s` of the matching PROPOSE.

`release()` always fails closed from `og-safestop` (`SafestopService.release`, K8: the stop-only key
can never sign a RELEASE) -- release requires the guardian's Tier-2 (two-person) co-signed path, which
no agent has built yet. `api` does not fabricate a signature to work around that; the release endpoint
below reports the gap honestly (`501`) rather than pretending to have released the stop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
from opengrid.safestop.pg_backend import REQUEST_CHANNEL
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/safestop", tags=["safestop"])

_SAFESTOP_PROPOSAL_KIND = "safestop"
_STOP_POLL_INTERVAL_S = 0.2
_STOP_POLL_TIMEOUT_S = 3.0
_SCOPE_KIND = {"fleet": "FLEET", "zone": "ZONE", "bank": "BANK"}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def propose_safestop(
    body: SafestopProposalRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """Step 1 of 2 (02b S7.1): arms `og-safestop`'s own `ConfirmationBroker` via a PROPOSE
    notification; nothing stops yet -- a single message can never engage the fleet (TS-10-03)."""
    scope_desc = body.scope + (f"/{body.scope_id}" if body.scope_id else "")
    summary = f"Engage safe stop on {scope_desc} ({body.reason})"
    proposal = proposals.create(_SAFESTOP_PROPOSAL_KIND, body, summary, identity.user)
    await store.notify(
        REQUEST_CHANNEL,
        {
            "action": "PROPOSE",
            "proposal_id": str(proposal.proposal_id),
            "scope": _SCOPE_KIND[body.scope],
            "scope_ref": body.scope_id or "",
            "reason": body.reason,
            "initiator_ref": identity.user,
        },
    )
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=60.0)


@router.post("/{proposal_id}/confirm")
async def confirm_safestop(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> SafestopConfirmResult:
    """Step 2 of 2 (02b S7.1/S7.3): NOTIFYs CONFIRM, then polls `og.stop_event` for the ENGAGE row
    `og-safestop` inserts on success. No matching event within the poll window means either the
    PROPOSE already expired in `og-safestop`'s own (shorter) `confirm_window_s`, or `og-safestop`
    is not running -- reported as `503`, never assumed to have engaged."""
    proposal = _pop_or_404(proposals, proposal_id, kind=_SAFESTOP_PROPOSAL_KIND)
    body: SafestopProposalRequest = proposal.body
    scope_kind = _SCOPE_KIND[body.scope]
    scope_ref = body.scope_id or "FLEET"
    since = datetime.now(UTC)

    await store.notify(REQUEST_CHANNEL, {"action": "CONFIRM", "proposal_id": str(proposal_id)})
    engaged = await _poll_for_engage(store, scope_kind, scope_ref, since=since)
    if not engaged:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no confirmed stop event within the poll window -- og-safestop may not be running "
            "or its own confirmation window elapsed first",
        )

    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="SAFE_STOP_ENGAGE",
        payload={
            "decision_ref": f"{scope_kind}:{scope_ref}",
            "scope": scope_kind,
            "scope_ref": scope_ref,
            "reason": body.reason,
        },
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="SAFE_STOP_ENGAGE",
        target_ref=f"{scope_kind}:{scope_ref}",
        tier="ENGAGE",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=since,
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
    """Single step in the API shape (02b S7.1: "release is inherently reversible, engage is not"), but
    `og-safestop`'s stop-only key always refuses to sign a RELEASE (K8) -- that requires the guardian's
    Tier-2 (two-person) co-signed path, not yet built by any agent. Records the attempt for audit and
    reports the gap as `501`, rather than fabricating a signature or silently no-op-ing."""
    scope_kind = _SCOPE_KIND.get(scope.lower(), scope.upper())
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="SAFE_STOP_RELEASE",
        payload={
            "decision_ref": f"{scope_kind}:{scope_id}",
            "scope": scope_kind,
            "scope_ref": scope_id,
            "reason": body.reason,
            "result": "REFUSED_NO_TIER2_PATH",
        },
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="SAFE_STOP_RELEASE",
        target_ref=f"{scope_kind}:{scope_id}",
        tier="TIER2",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="release requires the guardian's Tier-2 (two-person) co-signed path (02a S6.5); "
        "og-safestop's stop-only key cannot sign a RELEASE and that path is not yet built",
    )


async def _poll_for_engage(store: StoreProtocol, scope_kind: str, scope_ref: str, *, since: datetime) -> bool:
    elapsed = 0.0
    while elapsed < _STOP_POLL_TIMEOUT_S:
        latest = await store.latest_stop_event(scope_kind, scope_ref)
        if latest is not None:
            action, created_at = latest
            if action == "ENGAGE" and created_at >= since:
                return True
        await asyncio.sleep(_STOP_POLL_INTERVAL_S)
        elapsed += _STOP_POLL_INTERVAL_S
    return False


def _pop_or_404(proposals: ProposalStore, proposal_id: UUID, *, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
