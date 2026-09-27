"""Fleet monitoring & control (02b S7.1, S8 screen 2): hub/bank read models, manual target
two-step confirmation (the engine ramps it, the guardian signs each step), and the sampled fleet SSE stream.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalExpiredError, ProposalStore
from opengrid.api.routers import charge_windows as _charge_windows
from opengrid.api.schemas import CommandProposalRequest, ProposalAccepted
from opengrid.api.sse import sse_response
from opengrid.api.store import StoreProtocol
from opengrid.core.manual_targets import (
    MANUAL_TARGET_EVENT,
    SIGN_CONVENTION,
    TargetState,
    TargetStatus,
    effective_targets,
)
from opengrid.core.reasons import R_BANK_UNAVAILABLE
from opengrid.market.availability import availability_fields, with_availability
from opengrid.platform.config import Config
from opengrid.trace.pg_backend import (
    PgTraceBackend,
    TraceJournalUnavailableError,
    TraceNotRecordedError,
)
from opengrid.trace.store import TraceRecordRef, TraceStore

logger = logging.getLogger(__name__)

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
) -> dict[str, Any]:
    """`{"items": [...]}`, matching `opengrid.ui.routes.fleet`/`control_room`'s own parsing
    (`raw.get("items", [])`)."""
    hubs = await store.list_hubs(zone=zone, bank_id=bank, health=health, limit=limit, offset=offset)
    return {"items": [with_availability(h) for h in hubs]}  # D-37 availability fields


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
    return {**with_availability(hub), "telemetry_sparkline": sparkline}


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
        **availability_fields(bank.availability, bank.availability_reason),
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


# -- manual target, two-step confirmation (02b S7.1/S7.3; engine-side ramp, engine/manual.py) ----------

#: How long a confirmed manual target holds when the operator gives no duration, and the longest allowed.
DEFAULT_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 240
_MAX_BANK_HUBS = 2000
#: Manual targets are traced on their own stream (`manual_target:<user>`) through a DB-or-nothing backend
#: (`journal_failed_writes=False`): a write that cannot reach og.trace raises instead of being journaled, so a
#: target the API answered 503 "not recorded" can never appear later through a journal replay or an engine
#: restart (workstation r3.4 review, LOW/R5). The engine and guardian read targets by event_class, not stream.
MANUAL_TARGET_STREAM_PREFIX = "manual_target:"
_NOT_RECORDED_DETAIL = "manual target not recorded (trace store unavailable); nothing will ramp -- retry"
_UNKNOWN_DETAIL = (
    "manual target outcome unknown (the trace store failed and could not be re-checked); it may be live -- "
    "check GET /og/api/fleet/manual-targets before retrying"
)


def get_manual_target_trace_store(
    request: Request, default: Annotated[TraceStore, Depends(get_trace_store)]
) -> TraceStore:
    """og-api's DB-or-nothing trace store for manual targets, built once on the app's pool. Without a pool
    (unit tests that inject a fake trace store) it is the injected store."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return default
    store: TraceStore | None = getattr(request.app.state, "manual_target_trace_store", None)
    if store is None:
        store = TraceStore(PgTraceBackend(pool, journal_failed_writes=False))
        request.app.state.manual_target_trace_store = store
    return store


@router.post("/command", status_code=status.HTTP_202_ACCEPTED)
async def propose_command(
    body: CommandProposalRequest,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> ProposalAccepted:
    """Step 1 of 2: validates shape/role only (02b S7.3); nothing is commanded until confirm."""
    if not body.bank_id and not body.hub_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="bank_id or hub_id is required")
    target = body.bank_id or body.hub_id
    summary = f"Set {target} to {body.p_kw_setpoint:.1f} kW ({body.reason})"
    proposal = proposals.create(_COMMAND_PROPOSAL_KIND, body, summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=60.0)


@router.post("/command/{proposal_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_command(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_manual_target_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 2 of 2: records the confirmed setpoint as a MANUAL_TARGET (K10 trace event, the contract in
    `opengrid.engine.manual`) for the hub, or for every hub of the selected bank. The ENGINE then ramps
    each hub toward it within G-04 every cycle, each step signed by the guardian, until `expires_at`
    (live finding: a one-shot manual command was always vetoed by G-04's step bound). Returns 202
    `{"status": "RAMPING", "trace_id", "expires_at", "hub_ids"}`; cancel with
    `POST /manual-targets/{trace_id}/cancel`."""
    proposal = _pop_or_404(proposals, proposal_id, kind=_COMMAND_PROPOSAL_KIND)
    body: CommandProposalRequest = proposal.body
    hub_ids = await _resolve_hub_ids(store, body)
    return await issue_manual_target(
        trace_store,
        store,
        operator=identity.user,
        hub_ids=hub_ids,
        p_kw_target=body.p_kw_setpoint,
        reason=body.reason,
        duration_minutes=body.duration_minutes,
        target_ref=body.bank_id or hub_ids[0],
        extra={"proposal_id": str(proposal_id)},
    )


async def issue_manual_target(
    trace_store: TraceStore,
    store: StoreProtocol,
    *,
    operator: str,
    hub_ids: list[str],
    p_kw_target: float,
    reason: str,
    duration_minutes: int | None,
    target_ref: str,
    extra: dict[str, Any] | None = None,
    approver: str | None = None,
) -> dict[str, Any]:
    """The ONE write path for an operator's manual target (single and bulk confirm): the MANUAL_TARGET
    trace event first (K10), then the `og.operator_action` row. `p_kw_target` is +charge / -discharge and
    is written as `p_kw_command` with an explicit `sign_convention` (engine/manual.py's contract)."""
    minutes = duration_minutes or DEFAULT_TARGET_MINUTES
    if not 1 <= minutes <= MAX_TARGET_MINUTES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"duration_minutes must be 1..{MAX_TARGET_MINUTES}"
        )
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=minutes)
    trace_ref = await _append_manual_target(
        trace_store,
        store,
        operator,
        {
            "hub_ids": hub_ids,
            "p_kw_command": float(p_kw_target),
            "sign_convention": SIGN_CONVENTION,
            # issued_at is set HERE, at write time: the engine's late-record guard compares against it.
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "proposer": operator,
            "reason": reason,
            **(extra or {}),
        },
    )
    warnings = await _record_operator_action(
        store,
        operator_ref=operator,
        target_ref=target_ref,
        reason=reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=now,
        approver_ref=approver,
    )
    return {
        "status": "RAMPING",
        "trace_id": str(trace_ref.trace_id),
        "expires_at": expires_at.isoformat(),
        "hub_ids": hub_ids,
        "p_kw_target": float(p_kw_target),
        **({"warnings": warnings} if warnings else {}),
    }


async def _append_manual_target(
    trace_store: TraceStore, store: StoreProtocol, operator: str, payload: dict[str, Any]
) -> TraceRecordRef:
    """Append one MANUAL_TARGET row, DB-or-nothing, and return its ref -- or 503 "not recorded", which is
    then TRUE: the backend journals nothing, so no replay or restart can bring the row back later. The one
    ambiguous case (the insert committed but its acknowledgement was lost) is settled by looking the row up
    by this request's `request_id`: found -> success; absent -> 503 "not recorded" (true); the lookup itself
    failing -> 503 "outcome unknown" (never a false "not recorded")."""
    request_id = str(uuid4())
    try:
        ref = await trace_store.append(
            stream_id=f"{MANUAL_TARGET_STREAM_PREFIX}{operator}",
            decision_type="OPERATOR_ACTION",
            event_class=MANUAL_TARGET_EVENT,
            payload={**payload, "request_id": request_id},
            reason_codes=["MANUAL_OPERATOR"],
        )
    except (TraceNotRecordedError, TraceJournalUnavailableError) as exc:
        try:
            committed = await store.manual_target_trace_id(request_id)
        except Exception as lookup_exc:
            # Cannot tell whether the insert committed before its acknowledgement was lost: say so, never
            # claim "not recorded" when the target might be live.
            logger.error("manual target outcome unknown", extra={"operator": operator}, exc_info=True)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNKNOWN_DETAIL) from lookup_exc
        if committed is None:
            logger.error("manual target not recorded", extra={"operator": operator}, exc_info=True)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_NOT_RECORDED_DETAIL) from exc
        return TraceRecordRef(committed, "", -1, "")
    await _require_recorded(store, ref.trace_id)
    return ref


async def _record_operator_action(
    store: StoreProtocol,
    *,
    operator_ref: str,
    target_ref: str,
    reason: str,
    trace_id: UUID,
    confirmed_at: datetime,
    approver_ref: str | None = None,
) -> list[str]:
    """The `og.operator_action` audit row for a target that is ALREADY durably traced (and so live). A
    failure here must not turn into a 500 while the engine ramps the target: the durable result is returned
    with a warning (the K10 trace row remains the authoritative record)."""
    try:
        await store.insert_operator_action(
            operator_ref=operator_ref,
            action_kind="MANUAL_COMMAND",
            target_ref=target_ref,
            tier="TIER1",
            reason=reason,
            trace_id=trace_id,
            confirmed_at=confirmed_at,
            approver_ref=approver_ref,
        )
    except Exception:
        logger.exception("operator_action row not written for a recorded manual target")
        return ["operator_action audit row not written; the trace record stands"]
    return []


async def _require_recorded(store: StoreProtocol, trace_id: UUID) -> None:
    """Success only once the MANUAL_TARGET row is DURABLY in og.trace: if the trace backend fell back to its
    local journal (database unreachable), the row reaches og.trace only on a later replay -- which the engine
    refuses as a late record -- so the operator must be told it was not recorded (503), not that it ramps."""
    if not await store.trace_recorded(trace_id):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="manual target not recorded (trace store unavailable); nothing will ramp -- retry",
        )


async def _target_states(store: StoreProtocol, now: datetime) -> dict[str, TargetState]:
    """Every hub's newest manual target and its status (`core.manual_targets.effective_targets`, the rule the
    engine dispatches by: operator/late-record cancels, safe stops, expiry)."""
    rows = await store.manual_target_rows()
    hub_ids = sorted({str(h) for _id, payload, _at in rows for h in (payload.get("hub_ids") or [])})
    topology = await store.hub_banks_zones(hub_ids)
    zone_by_bank = {bank: zone for bank, zone in topology.values()}
    return effective_targets(
        rows,
        await store.stop_event_rows(),
        now,
        bank_of_hub=lambda hub: topology[hub][0] if hub in topology else None,
        zone_of_bank=zone_by_bank.get,
    )


@router.get("/manual-targets")
async def list_manual_targets(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    include_expired: bool = False,
) -> dict[str, Any]:
    """Every hub's newest manual target with its status -- `ACTIVE` (the engine is ramping it),
    `CANCELLED_BY_OPERATOR`, `CANCELLED_BY_SAFE_STOP` (with `stop_event_id`), `CANCELLED_LATE_RECORD`, and
    with `include_expired=true` also `EXPIRED`. Only `ACTIVE` targets control a hub; a stop-cancelled target is
    never reported as active. `{"items": [{hub_id, status, p_kw_target, issued_at, expires_at, trace_id,
    proposer, reason, stop_event_id, cancelled_by}]}`. The ramp rate is the engine's, not listed."""
    rows = await store.manual_target_rows()
    reasons = {str(trace_id): str(payload.get("reason", "")) for trace_id, payload, _at in rows}
    states = await _target_states(store, datetime.now(UTC))
    items = [
        {
            "hub_id": hub_id,
            "status": state.status.value,
            "p_kw_target": state.target.p_kw_target,
            "issued_at": state.target.issued_at.isoformat(),
            "expires_at": state.target.expires_at.isoformat(),
            "trace_id": state.target.trace_id,
            "proposer": state.target.proposer,
            "reason": reasons.get(state.target.trace_id, ""),
            "stop_event_id": state.stop_event_id,
            "cancelled_by": state.cancelled_by,
        }
        for hub_id, state in sorted(states.items())
        if include_expired or state.status is not TargetStatus.EXPIRED
    ]
    return {"items": items}


@router.post("/manual-targets/{trace_id}/cancel")
async def cancel_manual_target(
    trace_id: UUID,
    trace_store: Annotated[TraceStore, Depends(get_manual_target_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """End a manual target now: appends a MANUAL_TARGET for the hubs it still controls with
    `expires_at = now` (`core.manual_targets.effective_targets`, shared with the engine: they return to
    the allocator next cycle). Hubs a NEWER target has since taken over are left alone. 404 when the
    target is unknown or no longer controls any hub."""
    now = datetime.now(UTC)
    states = await _target_states(store, now)
    mine = {h: s for h, s in states.items() if s.target.trace_id == str(trace_id)}
    if not mine:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no manual target with that trace id")
    live = {h: s.target for h, s in mine.items() if s.status is TargetStatus.ACTIVE}
    hub_ids = sorted(live)
    if not hub_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "target is not active",
                "status": sorted({s.status.value for s in mine.values()}),
            },
        )
    trace_ref = await _append_manual_target(
        trace_store,
        store,
        identity.user,
        {
            "hub_ids": hub_ids,
            "p_kw_command": live[hub_ids[0]].p_kw_target,
            "sign_convention": SIGN_CONVENTION,
            "issued_at": now.isoformat(),
            "expires_at": now.isoformat(),
            "proposer": identity.user,
            "reason": "cancel",
            "cancels": str(trace_id),
        },
    )
    warnings = await _record_operator_action(
        store,
        operator_ref=identity.user,
        target_ref=f"cancel:{trace_id}",
        reason="cancel manual target",
        trace_id=trace_ref.trace_id,
        confirmed_at=now,
    )
    return {
        "status": "CANCELLED",
        "trace_id": str(trace_ref.trace_id),
        "cancels": str(trace_id),
        "hub_ids": hub_ids,
        **({"warnings": warnings} if warnings else {}),
    }


async def _resolve_hub_ids(store: StoreProtocol, body: CommandProposalRequest) -> list[str]:
    """The hubs a proposal targets: the one hub (must exist), or every hub of the bank (at least one)."""
    if body.hub_id:
        hub = await store.get_hub(body.hub_id)
        if hub is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub not found")
        _refuse_if_unavailable([hub], body.p_kw_setpoint)
        return [body.hub_id]
    hubs = await store.list_hubs(zone=None, bank_id=body.bank_id, health=None, limit=_MAX_BANK_HUBS, offset=0)
    if not hubs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="bank not found or has no hubs")
    _refuse_if_unavailable(hubs, body.p_kw_setpoint)
    return [str(h["hub_id"]) for h in hubs]


def _refuse_if_unavailable(hubs: list[dict[str, Any]], p_kw_setpoint: float) -> None:
    """D-37: a non-zero manual target on an UNAVAILABLE hub/bank (regulated market, no contract) is refused
    up front with the owner's reason (409); the guardian would veto every step anyway (G-33,
    R-BANK-UNAVAILABLE-REGULATED-NO-CONTRACT). A 0 kW hold is allowed; safe stop is a separate path."""
    if abs(p_kw_setpoint) <= 1e-9:
        return
    for hub in hubs:
        fields = with_availability(hub)
        if fields["availability"] != "AVAILABLE":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": R_BANK_UNAVAILABLE,
                    "hub_id": str(hub.get("hub_id")),
                    "availability": fields["availability"],
                    "availability_reason": fields["availability_reason"],
                    "availability_badge": fields["availability_badge"],
                    "availability_text": fields["availability_text"],
                    "message": f"Refused: {fields['availability_badge']}. {fields['availability_text']}",
                },
            )


def _pop_or_404(proposals: ProposalStore, proposal_id: UUID, *, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc


# D-30: the owner charge-window endpoints live in their own module but under this router's prefix
# (`/og/api/fleet/charge-windows`), so they are served wherever the fleet router is mounted.
router.include_router(_charge_windows.router)
