"""Bulk manual targets (Gitea #19): propose one setpoint for many hubs, confirm, and -- when any
selected hub serves a committed obligation or is FAULT/critical -- confirm a second time.

On the final confirm the whole selection becomes ONE manual target through the single-hub path's own
writer, `opengrid.api.routers.fleet.issue_manual_target` (a K10 `MANUAL_TARGET` trace event plus the
`og.operator_action` row). The engine then ramps every hub toward it within G-04, each step signed by
og-guardian (engine/manual.py); this module never builds a batch, signs, or evaluates anything itself
(K3). Each step of the bulk flow is also traced as an operator action (`OPERATOR_BULK_COMMAND`).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from opengrid.api.auth import Identity, current_identity
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import PROPOSAL_TTL_S, ProposalExpiredError, ProposalStore
from opengrid.api.routers import fleet
from opengrid.api.store import StoreProtocol
from opengrid.api.views_ext import (
    ExtViewsProtocol,
    FleetMapService,
    get_ext_views,
    get_fleet_map_service,
)
from opengrid.authz.enforce import audit_deny, policy_engine_for
from opengrid.platform.config import Config
from opengrid.trace.store import TraceRecordRef, TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api/fleet/commands", tags=["fleet"])

BULK_PROPOSAL_KIND: Final = "fleet_bulk_command"
#: Mutating and audited in `config/authz.toml` (operator only; a deny is written to the trace store).
BULK_ACTION: Final = "api.write"
_STREAM_PREFIX: Final = "operator_action:"
_EVENT_CLASS: Final = "OPERATOR_BULK_COMMAND"
_DEFAULT_MAX_HUBS = 500

#: Why a bulk command needs a second confirmation (#19).
REASON_COMMITTED_OBLIGATION: Final = "SERVES_COMMITTED_OBLIGATION"
REASON_FAULT: Final = "HUB_FAULT"
REASON_CRITICAL_ALERT: Final = "CRITICAL_ALERT"
REASON_AT_RESERVE: Final = "AT_OR_BELOW_RESERVE"
#: Listed, but not on its own a reason for a second confirmation.
WARNING_OFFLINE: Final = "HUB_OFFLINE"
_COMMITTED_STATES: Final = frozenset({"COMMITTED", "DELIVERING"})


class BulkCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hub_ids: list[str] = Field(min_length=1)
    p_kw_setpoint: float  # the manual target, +charge / -discharge
    reason: str = Field(min_length=1)
    #: How long the manual target holds (default 15 min, max 4 h).
    duration_minutes: int | None = Field(default=None, ge=1, le=240)


@dataclass(slots=True)
class BulkProposal:
    request: BulkCommandRequest
    hub_ids: list[str]
    bank_by_hub: dict[str, str]
    requires_double_confirm: bool
    reasons: list[dict[str, Any]]
    confirmations: list[dict[str, str]] = field(default_factory=list)


def hub_risk(hub: dict[str, Any], critical_scopes: set[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(double-confirm reasons, warnings) for one fleet-map hub."""
    reasons: list[str] = []
    warnings: list[str] = []
    committed = [o for o in hub.get("serving_obligations", []) if o.get("state") in _COMMITTED_STATES]
    if committed:
        reasons.append(REASON_COMMITTED_OBLIGATION)
    if hub["activity"] == "FAULT":
        reasons.append(REASON_FAULT)
    if (
        ("hub", hub["hub_id"]) in critical_scopes
        or ("bank", hub["bank_id"]) in critical_scopes
        or (
            "zone",
            hub["zone"],
        )
        in critical_scopes
    ):
        reasons.append(REASON_CRITICAL_ALERT)
    soc, reserve = hub.get("soc_kwh"), hub.get("reserve_kwh")
    if soc is not None and reserve is not None and soc <= reserve:
        reasons.append(REASON_AT_RESERVE)
    if hub["activity"] == "OFFLINE":
        warnings.append(WARNING_OFFLINE)
    return reasons, warnings


async def require_bulk_command(
    identity: Annotated[Identity, Depends(current_identity)],
    cfg: Annotated[Config, Depends(get_config)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
) -> Identity:
    """`BULK_ACTION` through `opengrid.authz`'s `PolicyEngine`, with its audited-deny write -- what
    `opengrid.authz.enforce.require_action(BULK_ACTION)` does. That factory cannot be used as a route
    dependency yet: its inner function annotates `Identity`, imported only under `TYPE_CHECKING`, so
    FastAPI cannot resolve the annotation and treats `identity` as a required body field (422 on every
    call). Switch to `require_action` once its owner fixes that (reported in the #19 build report)."""
    decision = policy_engine_for(cfg).decide(role=identity.role.value, action=BULK_ACTION)
    if decision.deny:
        await audit_deny(
            trace_store, actor=identity.user, action=BULK_ACTION, decision=decision, resource=None
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)
    return identity


async def _trace_step(
    trace_store: TraceStore, identity: Identity, step: str, payload: dict[str, Any]
) -> TraceRecordRef:
    return await trace_store.append(
        f"{_STREAM_PREFIX}{identity.user}",
        "OPERATOR_ACTION",
        _EVENT_CLASS,
        {"step": step, "operator": identity.user, **payload},
        ["MANUAL_OPERATOR"],
    )


@router.post("/bulk", status_code=status.HTTP_202_ACCEPTED)
async def propose_bulk(
    body: BulkCommandRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    fleet_map: Annotated[FleetMapService, Depends(get_fleet_map_service)],
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    cfg: Annotated[Config, Depends(get_config)],
    identity: Annotated[Identity, Depends(require_bulk_command)],
) -> dict[str, Any]:
    """Step 1: validates the selection and says whether a second confirmation will be needed, and why.
    Nothing is commanded until confirm."""
    hub_ids = list(dict.fromkeys(body.hub_ids))
    max_hubs = int(cfg.get("api.bulk_commands.max_hubs", _DEFAULT_MAX_HUBS))
    if len(hub_ids) > max_hubs:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"at most {max_hubs} hubs per bulk command"
        )
    snapshot = await fleet_map.snapshot()
    unknown = [h for h in hub_ids if h not in snapshot.by_id]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"message": "unknown hub_id(s)", "hub_ids": unknown}
        )
    critical_scopes = await views.critical_alert_scopes()
    reasons: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for hub_id in hub_ids:
        hub = snapshot.by_id[hub_id]
        hub_reasons, hub_warnings = hub_risk(hub, critical_scopes)
        if hub_reasons:
            reasons.append(
                {
                    "hub_id": hub_id,
                    "reasons": hub_reasons,
                    "obligations": [
                        o for o in hub.get("serving_obligations", []) if o.get("state") in _COMMITTED_STATES
                    ],
                }
            )
        if hub_warnings:
            warnings.append({"hub_id": hub_id, "warnings": hub_warnings})
    requires_double = bool(reasons)
    state = BulkProposal(
        request=body,
        hub_ids=hub_ids,
        bank_by_hub={h: snapshot.by_id[h]["bank_id"] for h in hub_ids},
        requires_double_confirm=requires_double,
        reasons=reasons,
    )
    summary = f"Set {len(hub_ids)} hub(s) to {body.p_kw_setpoint:.1f} kW ({body.reason})"
    proposal = proposals.create(BULK_PROPOSAL_KIND, state, summary, identity.user)
    reason_counts = Counter(r for entry in reasons for r in entry["reasons"])
    trace_ref = await _trace_step(
        trace_store,
        identity,
        "PROPOSE",
        {
            "proposal_id": str(proposal.proposal_id),
            "hub_ids": hub_ids,
            "p_kw_setpoint": body.p_kw_setpoint,
            "reason": body.reason,
            "requires_double_confirm": requires_double,
            "reason_counts": dict(reason_counts),
        },
    )
    return {
        "proposal_id": str(proposal.proposal_id),
        "summary": summary,
        "expires_in_s": PROPOSAL_TTL_S,
        "hub_count": len(hub_ids),
        "requires_double_confirm": requires_double,
        "confirmations_required": 2 if requires_double else 1,
        "reason_counts": dict(reason_counts),
        "double_confirm_reasons": reasons,
        "warnings": warnings,
        "trace_id": str(trace_ref.trace_id),
    }


@router.post("/bulk/{proposal_id}/confirm")
async def confirm_bulk(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    target_trace_store: Annotated[TraceStore, Depends(fleet.get_manual_target_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    identity: Annotated[Identity, Depends(require_bulk_command)],
    response: Response,
) -> dict[str, Any]:
    """Step 2 (and 3). With `requires_double_confirm`, the first confirm only records itself and returns
    `status=AWAITING_SECOND_CONFIRM`; the second executes: one MANUAL_TARGET for every selected hub
    (`status=RAMPING`, each hub `RAMPING`, with the target's `manual_target_trace_id` and `expires_at`;
    cancel with `POST /og/api/fleet/manual-targets/{manual_target_trace_id}/cancel`). Per-step guardian
    verdicts now happen cycle by cycle in the engine, not at confirm time."""
    try:
        proposal = proposals.peek(proposal_id, kind=BULK_PROPOSAL_KIND)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
    state: BulkProposal = proposal.body
    now = datetime.now(UTC)
    if state.requires_double_confirm and not state.confirmations:
        state.confirmations.append({"operator": identity.user, "at": now.isoformat()})
        trace_ref = await _trace_step(
            trace_store, identity, "CONFIRM_1", {"proposal_id": str(proposal_id), "reasons": state.reasons}
        )
        return {
            "proposal_id": str(proposal_id),
            "status": "AWAITING_SECOND_CONFIRM",
            "confirmations_required": 2,
            "confirmations_received": 1,
            "double_confirm_reasons": state.reasons,
            "trace_id": str(trace_ref.trace_id),
        }

    proposals.pop(proposal_id, kind=BULK_PROPOSAL_KIND)
    step = "CONFIRM_2" if state.requires_double_confirm else "CONFIRM"
    confirm_ref = await _trace_step(trace_store, identity, step, {"proposal_id": str(proposal_id)})
    response.status_code = status.HTTP_202_ACCEPTED  # executed: the engine is now ramping (single-hub parity)
    first = state.confirmations[0]["operator"] if state.confirmations else identity.user
    # The MANUAL_TARGET itself goes through the DB-or-nothing store (a 503 "not recorded" stays true).
    issued = await fleet.issue_manual_target(
        target_trace_store,
        store,
        operator=first,
        hub_ids=state.hub_ids,
        p_kw_target=state.request.p_kw_setpoint,
        reason=f"{state.request.reason} [bulk {proposal_id}]",
        duration_minutes=state.request.duration_minutes,
        target_ref=f"bulk:{proposal_id}",
        extra={"proposal_id": str(proposal_id), "bulk": True},
        approver=identity.user,
    )
    result_ref = await _trace_step(
        trace_store,
        identity,
        "RESULT",
        {
            "proposal_id": str(proposal_id),
            "manual_target_trace_id": issued["trace_id"],
            "confirm_trace_id": str(confirm_ref.trace_id),
        },
    )
    return {
        "proposal_id": str(proposal_id),
        "status": "RAMPING",
        "hub_count": len(state.hub_ids),
        "outcome_counts": {"RAMPING": len(state.hub_ids)},
        "results": [{"hub_id": h, "outcome": "RAMPING"} for h in state.hub_ids],
        "manual_target_trace_id": issued["trace_id"],
        "expires_at": issued["expires_at"],
        "trace_id": str(result_ref.trace_id),
        **({"warnings": issued["warnings"]} if issued.get("warnings") else {}),
    }
