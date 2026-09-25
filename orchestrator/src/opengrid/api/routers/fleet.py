"""Fleet monitoring & control (02b S7.1, S8 screen 2): hub/bank read models, manual command
two-step confirmation through the guardian, and the sampled fleet SSE stream.
"""

from __future__ import annotations

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
from opengrid.guardian import evaluate_and_sign
from opengrid.ledger import ledger_version
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/fleet", tags=["fleet"])

_COMMAND_PROPOSAL_KIND = "fleet_command"


@router.get("/hubs")
async def list_hubs(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    zone: str | None = None,
    bank: str | None = None,
    health: str | None = None,
    limit: int = Query(default=200, le=2000, gt=0),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    hubs = await store.list_hubs(zone=zone, bank_id=bank, health=health, limit=limit, offset=offset)
    return [h.model_dump(mode="json") for h in hubs]


@router.get("/hubs/{hub_id}")
async def get_hub(
    hub_id: str,
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    found = await store.get_hub(hub_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub not found")
    hub, state = found
    sparkline = await store.hub_telemetry_sparkline(hub_id, minutes=15)
    return {
        "hub": hub.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "telemetry_sparkline": [
            {"ts": row["ts"].isoformat(), "soc_kwh": row["soc_kwh"], "p_kw": row["p_kw"]} for row in sparkline
        ],
    }


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
                {"hub_id": h.hub_id, "health": h.health, "soc_kwh": h.soc_kwh, "p_kw": h.p_kw} for h in hubs
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
    """Step 2 of 2: the guardian actually runs its checks here (reserve, ramp, lease/epoch, G-19
    commitment lock) and either signs or vetoes (02b S7.3); a veto is returned as `409`, never
    silently retried."""
    proposal = _pop_or_404(proposals, proposal_id, kind=_COMMAND_PROPOSAL_KIND)
    body: CommandProposalRequest = proposal.body

    command_batch_id = uuid4()
    item_payload = {
        "bank_id": body.bank_id,
        "hub_id": body.hub_id,
        "p_kw_setpoint": body.p_kw_setpoint,
        "reason_code": "MANUAL_OPERATOR",
    }
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="MANUAL_COMMAND",
        payload={"decision_ref": str(command_batch_id), "command": item_payload, "reason": body.reason},
    )
    batch = CommandBatchRow(
        command_batch_id=command_batch_id,
        cycle_id="MANUAL",
        ledger_version=await ledger_version(),
        submission_id=str(proposal_id),
        command_count=1,
        merkle_root=sha256_hex_of_json(item_payload),
        trace_pre_image_id=trace_ref.trace_id,
    )
    verdict = await evaluate_and_sign(batch)
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="MANUAL_COMMAND",
        target_ref=body.bank_id or body.hub_id,
        tier="TIER1",
        reason=body.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    result = CommandConfirmResult(
        proposal_id=proposal_id,
        outcome=verdict.outcome,
        vetoed_rule_ids=verdict.vetoed_rule_ids,
        trace_id=trace_ref.trace_id,
    )
    if verdict.outcome in ("VETOED", "TIMEOUT"):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=result.model_dump(mode="json"))
    return result


def _pop_or_404(proposals: ProposalStore, proposal_id: UUID, *, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
