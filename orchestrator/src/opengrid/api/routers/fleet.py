"""Fleet monitoring & control (02b S7.1, S8 screen 2): hub/bank read models, manual command
two-step confirmation through the guardian, and the sampled fleet SSE stream.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalExpiredError, ProposalStore
from opengrid.api.schemas import CommandConfirmResult, CommandProposalRequest, ProposalAccepted
from opengrid.api.sse import sse_response
from opengrid.api.store import StoreProtocol
from opengrid.core.crypto import sha256_hex_of_json
from opengrid.core.models.engine import CommandBatchRow
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/fleet", tags=["fleet"])

_COMMAND_PROPOSAL_KIND = "fleet_command"
_COMMAND_VALIDITY_S = 30
_VERDICT_POLL_INTERVAL_S = 0.2
_VERDICT_POLL_TIMEOUT_S = 3.0


@router.get("/hubs")
async def list_hubs(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    zone: str | None = None,
    bank: str | None = None,
    health: str | None = None,
    limit: int = Query(default=200, le=2000, gt=0),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """`{"items": [...]}`, matching `opengrid.ui.routes.fleet`/`control_room`'s own parsing
    (`raw.get("items", [])`)."""
    hubs = await store.list_hubs(zone=zone, bank_id=bank, health=health, limit=limit, offset=offset)
    return {"items": hubs}


@router.get("/summary")
async def fleet_summary(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, int]:
    """`{"total", "online", "stale", "offline"}` counts over the whole registered fleet (task item
    A2: `GET /hubs` paginates at 200/page, so a smoke test counting "how many of ~2,000 hubs are
    online" needs either this O(1) summary or to page through every `/hubs` response -- this is the
    former; see `StoreProtocol.hub_health_summary`)."""
    return await store.hub_health_summary()


@router.get("/hubs/{hub_id}")
async def get_hub(
    hub_id: str,
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """A flat hub+state dict -- `opengrid.ui.templates._partials.hub_drilldown.html` reads
    `hub.hub_id`/`bank_id`/`zone`/`soc_kwh`/`p_kw`/`health`/`lease_epoch`/`lease_expires_at`/
    `last_command_id`/`last_seen_at` directly off the top-level object."""
    hub = await store.get_hub(hub_id)
    if hub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub not found")
    sparkline = await store.hub_telemetry_sparkline(hub_id, minutes=15)
    return {**hub, "telemetry_sparkline": sparkline}


@router.get("/banks/{bank_id}")
async def get_bank(
    bank_id: str,
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    bank = await store.get_bank(bank_id)
    if bank is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="bank not found")
    return {
        "bank_id": bank.bank_id,
        "zone": bank.zone,
        "kva_rating": bank.kva_rating,
        "reserve_kva": bank.reserve_kva,
        "feeder_id": bank.feeder_id,
        "member_hub_count": bank.member_hub_count,
        "load_kva_estimate": bank.load_kva_estimate,
    }


@router.get("/stream")
async def stream_fleet(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    zone: str | None = None,
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        hubs = await store.list_hubs(zone=zone, bank_id=None, health=None, limit=2000, offset=0)
        return {
            "hubs": [
                {"hub_id": h["hub_id"], "health": h["health"], "soc_kwh": h["soc_kwh"], "p_kw": h["p_kw"]}
                for h in hubs
            ]
        }

    return sse_response(request, interval_s=1.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


# -- manual command, two-step confirmation (02b S7.1/S7.3) -------------------------------------------


@router.post("/command", status_code=status.HTTP_202_ACCEPTED)
async def propose_command(
    body: CommandProposalRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """Step 1 of 2: validates shape/role only (02b S7.3) -- the guardian check runs at confirm time."""
    if not body.bank_id and not body.hub_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="bank_id or hub_id is required")
    target = body.bank_id or body.hub_id
    summary = f"Set {target} to {body.p_kw_setpoint:.1f} kW ({body.reason})"
    proposal = proposals.create(_COMMAND_PROPOSAL_KIND, body, summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=60.0)


@router.post("/command/{proposal_id}/confirm")
async def confirm_command(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> CommandConfirmResult:
    """Step 2 of 2 (02b S7.3): writes the K10 decision pre-image (`decision_type="RT_ALLOCATION"`,
    the shape `opengrid.guardian.repo.PgProposalPort` reads back) and a `command_batch` header row,
    then polls `og.verdict` for the independently-running `og-guardian` process's PASS/VETOED/TIMEOUT
    result -- `api` never signs or evaluates a batch itself (K3: sole signer stays in `og-guardian`).
    A veto is returned as `409`, never silently retried; no verdict within the poll window is a `503`
    (guardian not keeping up or not running), not a silent success.
    """
    proposal = _pop_or_404(proposals, proposal_id, kind=_COMMAND_PROPOSAL_KIND)
    body: CommandProposalRequest = proposal.body

    bank_id = await _resolve_bank_id(store, body)
    lease_epoch = await _resolve_lease_epoch(store, body)
    command_batch_id = uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=_COMMAND_VALIDITY_S)
    item_payload = {
        "hub_id": body.hub_id or bank_id,
        "p_kw_setpoint": body.p_kw_setpoint,
        "reason_code": "MANUAL_OPERATOR",
    }
    allocation_payload = {
        "command_batch_id": str(command_batch_id),
        "bank_id": bank_id,
        "cycle_id": "MANUAL",
        "epoch": lease_epoch,
        "seq": int(now.timestamp()),
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ledger_version": await store.current_ledger_version(),
        "items": [item_payload],
        "is_firm_event": False,
        "reason": body.reason,
        "proposer": identity.user,
    }
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="RT_ALLOCATION",
        event_class="RT_ALLOCATION",
        payload=allocation_payload,
        reason_codes=["MANUAL_OPERATOR"],
    )
    batch = CommandBatchRow(
        command_batch_id=command_batch_id,
        cycle_id="MANUAL",
        ledger_version=allocation_payload["ledger_version"],
        submission_id=str(proposal_id),
        command_count=1,
        merkle_root=sha256_hex_of_json(item_payload),
        trace_pre_image_id=trace_ref.trace_id,
    )
    await store.insert_command_batch(batch)
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="MANUAL_COMMAND",
        target_ref=bank_id,
        tier="TIER1",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=now,
    )

    verdict = await _poll_for_verdict(store, command_batch_id)
    if verdict is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no guardian verdict within the poll window -- og-guardian may not be running",
        )
    result = CommandConfirmResult(
        proposal_id=proposal_id,
        outcome=verdict.outcome,
        vetoed_rule_ids=verdict.vetoed_rule_ids,
        trace_id=trace_ref.trace_id,
    )
    if verdict.outcome != "PASS":
        raise HTTPException(status.HTTP_409_CONFLICT, detail=result.model_dump(mode="json"))
    return result


async def _resolve_bank_id(store: StoreProtocol, body: CommandProposalRequest) -> str:
    if body.bank_id:
        return body.bank_id
    hub = await store.get_hub(body.hub_id) if body.hub_id else None
    if hub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub not found")
    bank_id: str = hub["bank_id"]
    return bank_id


async def _resolve_lease_epoch(store: StoreProtocol, body: CommandProposalRequest) -> int:
    if not body.hub_id:
        return 0
    hub = await store.get_hub(body.hub_id)
    return int(hub["lease_epoch"]) if hub is not None else 0


async def _poll_for_verdict(store: StoreProtocol, command_batch_id: UUID) -> Any:
    elapsed = 0.0
    while elapsed < _VERDICT_POLL_TIMEOUT_S:
        verdict = await store.get_verdict(command_batch_id)
        if verdict is not None:
            return verdict
        await asyncio.sleep(_VERDICT_POLL_INTERVAL_S)
        elapsed += _VERDICT_POLL_INTERVAL_S
    return None


def _pop_or_404(proposals: ProposalStore, proposal_id: UUID, *, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
