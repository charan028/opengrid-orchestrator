"""Scoped safe stop, two-step confirmation (02b S7.1/S7.3).

`opengrid.safestop` is its own process holding the stop-only Ed25519 key (K8: it must work even if
`og-engine`/`og-guardian` are down, and its blast radius on the broker/key material stays minimal) --
`api` never imports or configures it directly. Instead `api` is the "og-api (once built)" side of the
LISTEN/NOTIFY request intake `opengrid.safestop.pg_backend`/`opengrid.safestop.main` already document:
NOTIFY `REQUEST_CHANNEL` with `{"action": "PROPOSE"|"CONFIRM", "proposal_id", "scope", "scope_ref",
"reason", "initiator_ref"}`; `og-safestop`'s own `ConfirmationBroker` only calls `engage()` when a
CONFIRM lands within its `confirm_window_s` of the matching PROPOSE.

A RELEASE is never signed by `og-safestop`'s stop-only key (K8). It is the guardian's two-person path:
operator A requests (`request_release`), a different operator B approves (`approve_release`), which
writes one confirmed TIER2 `og.operator_action`; the guardian verifies it against
`[guardian].stop_release_authorised_operators` and signs, and `og-safestop` relays it to the hubs. `api`
never fabricates a signature or a release.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from opengrid.api.auth import Identity, require_operator
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalExpiredError, ProposalStore
from opengrid.api.schemas import (
    ProposalAccepted,
    SafestopConfirmResult,
    SafestopProposalRequest,
    SafestopReleaseRequest,
)
from opengrid.api.store import StoreProtocol
from opengrid.platform.config import Config
from opengrid.safestop.pg_backend import REQUEST_CHANNEL
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/safestop", tags=["safestop"])

_SAFESTOP_PROPOSAL_KIND = "safestop"
_RELEASE_PROPOSAL_KIND = "safestop-release"
_STOP_POLL_INTERVAL_S = 0.2
_STOP_POLL_TIMEOUT_S = 3.0
_RELEASE_POLL_TIMEOUT_S = 10.0  # guardian poll + sign + og-safestop relay
_SCOPE_KIND = {"fleet": "FLEET", "zone": "ZONE", "bank": "BANK"}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def propose_safestop(
    body: SafestopProposalRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """Step 1 of 2 (02b S7.1): arms `og-safestop`'s own `ConfirmationBroker` via a PROPOSE
    notification; nothing stops yet -- a single message can never engage the fleet (TS-10-03). The
    proposal expires here at og-safestop's own `[safestop].confirm_window_s` (30 s), not the generic
    60 s: a confirm the broker would already reject is refused with 409 instead of timing out."""
    scope_desc = body.scope + (f"/{body.scope_id}" if body.scope_id else "")
    summary = f"Engage safe stop on {scope_desc} ({body.reason})"
    window_s = safestop_confirm_window_s(cfg)
    proposal = proposals.create(_SAFESTOP_PROPOSAL_KIND, body, summary, identity.user, ttl_s=window_s)
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
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=window_s)


#: og-safestop's `ConfirmationBroker` default (`opengrid.safestop.main`: `[safestop].confirm_window_s`).
DEFAULT_SAFESTOP_CONFIRM_WINDOW_S = 30.0


def safestop_confirm_window_s(cfg: Config) -> float:
    """The safe-stop confirmation window og-safestop enforces -- the one config key both sides read."""
    return float(cfg.get("safestop.confirm_window_s", DEFAULT_SAFESTOP_CONFIRM_WINDOW_S))


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
    is not running -- reported as `503`, never assumed to have engaged. A proposal older than the
    broker's window is refused up front with `409` ("proposal expired, propose again")."""
    try:
        proposal = proposals.pop(proposal_id, kind=_SAFESTOP_PROPOSAL_KIND)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
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


@dataclass(frozen=True, slots=True)
class _ReleaseRequest:
    scope_kind: str
    scope_ref: str
    reason: str
    requested_by: str
    requested_at: datetime


@router.post("/{scope}/{scope_id}/release", status_code=status.HTTP_202_ACCEPTED)
async def request_release(
    scope: str,
    scope_id: str,
    body: SafestopReleaseRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """K8 release, step 1 of 2 (operator A). Nothing is written or released yet: `og-safestop`'s
    stop-only key can never sign a RELEASE; the guardian signs one only for a request approved by a
    SECOND authorised operator (`approve_release`, `opengrid.guardian.stop_release`)."""
    scope_kind, scope_ref = _normalise_scope(scope, scope_id)
    request = _ReleaseRequest(scope_kind, scope_ref, body.reason, identity.user, datetime.now(UTC))
    summary = f"Release safe stop on {scope_kind}:{scope_ref} ({body.reason}); needs a second operator"
    proposal = proposals.create(_RELEASE_PROPOSAL_KIND, request, summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=60.0)


@router.post("/release/{proposal_id}/approve")
async def approve_release(
    proposal_id: UUID,
    response: Response,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """K8 release, step 2 of 2 (operator B, never the requester: `403`). Writes ONE `og.operator_action`
    SAFE_STOP_RELEASE (tier TIER2, confirmed, `operator_ref` = A, `approver_ref` = B) that the guardian
    verifies (allow-list, distinct operators, age, scope still engaged) and signs, and `og-safestop`
    relays to the hubs. Then polls `og.stop_event` for the RELEASE: `200` when it lands, `202` while the
    guardian has not released (it may refuse -- e.g. an empty allow-list -- which is never faked here)."""
    try:
        request: _ReleaseRequest = proposals.peek(proposal_id, kind=_RELEASE_PROPOSAL_KIND).body
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="release request expired, request again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown release request") from exc
    if identity.user.casefold() == request.requested_by.casefold():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="a release needs a second operator; the requester cannot approve",
        )
    _pop_or_404(proposals, proposal_id, kind=_RELEASE_PROPOSAL_KIND)

    since = datetime.now(UTC)
    target_ref = f"{request.scope_kind}:{request.scope_ref}"
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="SAFE_STOP_RELEASE",
        payload={
            "decision_ref": target_ref,
            "scope": request.scope_kind,
            "scope_ref": request.scope_ref,
            "reason": request.reason,
            "requested_by": request.requested_by,
            "approved_by": identity.user,
        },
    )
    await store.insert_operator_action(
        operator_ref=request.requested_by,
        action_kind="SAFE_STOP_RELEASE",
        target_ref=target_ref,
        tier="TIER2",
        reason=request.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=since,
        approver_ref=identity.user,
        # The guardian reads created_at as requested_at and refuses approved_at < requested_at: the
        # row's created_at is operator A's request time, never the (later) insert time.
        created_at=request.requested_at,
    )
    released = await _poll_for_stop_action(
        store,
        request.scope_kind,
        request.scope_ref,
        "RELEASE",
        since=since,
        timeout_s=_RELEASE_POLL_TIMEOUT_S,
    )
    if not released:
        response.status_code = status.HTTP_202_ACCEPTED
    return {
        "proposal_id": str(proposal_id),
        "scope": request.scope_kind,
        "scope_ref": request.scope_ref,
        "released": released,
        "trace_id": str(trace_ref.trace_id),
        "detail": None if released else "approved; waiting for the guardian's signed release",
    }


def _normalise_scope(scope: str, scope_id: str) -> tuple[str, str]:
    """`fleet` -> ("FLEET", "FLEET") whatever id was passed (the engage path's own fleet scope_ref);
    `zone`/`bank` keep their id."""
    scope_kind = _SCOPE_KIND.get(scope.lower())
    if scope_kind is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown scope {scope!r}")
    return scope_kind, ("FLEET" if scope_kind == "FLEET" else scope_id)


async def _poll_for_engage(store: StoreProtocol, scope_kind: str, scope_ref: str, *, since: datetime) -> bool:
    return await _poll_for_stop_action(
        store, scope_kind, scope_ref, "ENGAGE", since=since, timeout_s=_STOP_POLL_TIMEOUT_S
    )


async def _poll_for_stop_action(
    store: StoreProtocol, scope_kind: str, scope_ref: str, action: str, *, since: datetime, timeout_s: float
) -> bool:
    elapsed = 0.0
    while elapsed < timeout_s:
        latest = await store.latest_stop_event(scope_kind, scope_ref)
        if latest is not None:
            latest_action, created_at = latest
            if latest_action == action and created_at >= since:
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
