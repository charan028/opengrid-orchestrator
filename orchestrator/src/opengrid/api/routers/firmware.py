"""Firmware update campaigns from the console (R3.1; operator note 07-delivery/16-firmware-updates.md).

Operator role for every mutation, viewer for reads. Nothing here signs or publishes (K3): a campaign only
becomes `og.firmware_command` requests through og-engine's executor, and each is signed by og-guardian after
its own G-36 check.

    GET  /og/api/firmware/catalogue
    POST /og/api/firmware/campaigns                      propose (DRAFT; resolves the hubs once)
    POST /og/api/firmware/campaigns/{id}/confirm         step 2 (same operator ok): APPROVED, or PROPOSED
    POST /og/api/firmware/campaigns/{id}/approve         second operator (never the proposer): APPROVED
    POST /og/api/firmware/campaigns/{id}/pause|resume|abort
    POST /og/api/firmware/campaigns/{id}/retry-failed    two-step: returns a proposal ...
    POST /og/api/firmware/campaigns/{id}/retry-failed/{proposal_id}/confirm
    POST /og/api/firmware/campaigns/{id}/hubs/{hub_id}/rollback                        two-step, likewise
    POST /og/api/firmware/campaigns/{id}/hubs/{hub_id}/rollback/{proposal_id}/confirm
    GET  /og/api/firmware/campaigns, /{id} (per-hub jobs + counts), /{id}/events?after=<event_id>
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_proposals, get_trace_store
from opengrid.api.proposals import PROPOSAL_TTL_S, ProposalExpiredError, ProposalStore
from opengrid.firmware import campaigns as svc
from opengrid.firmware.catalogue import Catalogue
from opengrid.firmware.config import FirmwareConfig, load_firmware_config
from opengrid.firmware.model import Campaign, Job, JobEvent, WaveSpec
from opengrid.firmware.planner import job_counts
from opengrid.firmware.repo import FirmwareRepo, HubSelection, PgFirmwareRepo
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/firmware", tags=["firmware"])

_RETRY_KIND: Final = "firmware-retry-failed"
_ROLLBACK_KIND: Final = "firmware-hub-rollback"


# ---------------------------------------------------------------------------------------------- deps


def get_firmware_repo(request: Request) -> FirmwareRepo:
    """Postgres repo over the app's pool; tests override this dependency with an in-memory fake."""
    return PgFirmwareRepo(request.app.state.pool)


def get_firmware_config(cfg: Annotated[Config, Depends(get_config)]) -> FirmwareConfig:
    return load_firmware_config(cfg)


def _now() -> datetime:
    return datetime.now(UTC)


def _tracer(trace_store: TraceStore) -> svc.Tracer:
    async def trace(stream_id: str, event_class: str, payload: dict[str, Any]) -> UUID | None:
        ref = await trace_store.append(stream_id, svc.TRACE_DECISION_TYPE, event_class, payload)
        return ref.trace_id

    return trace


def _http(exc: svc.CampaignError) -> HTTPException:
    return HTTPException(exc.status, detail=exc.detail)


# ---------------------------------------------------------------------------------------------- bodies


class WavesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canary: int = Field(default=1, ge=1)
    canary_pct: float | None = Field(default=None, gt=0, le=100)
    sizes: list[int] = Field(default_factory=list)
    size_pct: float | None = Field(default=25.0, gt=0, le=100)


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hub_ids: list[str] = Field(default_factory=list)
    bank_ids: list[str] = Field(default_factory=list)
    zones: list[str] = Field(default_factory=list)
    feeder_ids: list[str] = Field(default_factory=list)
    hardware_revisions: list[str] = Field(default_factory=list)
    firmware_versions: list[str] = Field(default_factory=list)


class CampaignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    target_version: str = Field(min_length=1)
    selection: SelectionBody
    reason: str = Field(min_length=1)
    waves: WavesBody = Field(default_factory=WavesBody)
    bank_max_concurrent_pct: float | None = Field(default=None, gt=0, le=100)
    feeder_max_concurrent_pct: float | None = Field(default=None, gt=0, le=100)
    max_failures: int | None = Field(default=None, ge=1)
    max_failure_pct: float | None = Field(default=None, gt=0, le=100)
    window_start: datetime | None = None
    window_end: datetime | None = None
    allow_downgrade: bool = False
    override_committed: bool = False


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------------------------- views


def campaign_json(c: Campaign) -> dict[str, Any]:
    return {
        "campaign_id": str(c.campaign_id),
        "name": c.name,
        "target_version": c.target_version,
        "state": c.state.value,
        "hub_count": len(c.hub_ids),
        "waves": c.waves,
        "selection": c.selection,
        "bank_max_concurrent_pct": c.bank_max_concurrent_pct,
        "feeder_max_concurrent_pct": c.feeder_max_concurrent_pct,
        "max_failures": c.max_failures,
        "max_failure_pct": c.max_failure_pct,
        "window_start": c.window_start.isoformat() if c.window_start else None,
        "window_end": c.window_end.isoformat() if c.window_end else None,
        "allow_downgrade": c.allow_downgrade,
        "override_committed": c.override_committed,
        "requires_second_operator": c.requires_second_operator,
        "second_operator_reasons": c.second_operator_reasons,
        "reason": c.reason,
        "proposed_by": c.proposed_by,
        "confirmed_by": c.confirmed_by,
        "approved_by": c.approved_by,
        "halt_reason": c.halt_reason,
        "created_at": c.created_at.isoformat(),
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
        "trace_id": str(c.trace_id) if c.trace_id else None,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def job_json(j: Job) -> dict[str, Any]:
    return {
        "job_id": str(j.job_id),
        "hub_id": j.hub_id,
        "bank_id": j.bank_id,
        "feeder_id": j.feeder_id,
        "wave": j.wave,
        "action": j.action,
        "state": j.state.value,
        "from_version": j.from_version,
        "target_version": j.target_version,
        "attempts": j.attempts,
        "next_attempt_at": _iso(j.next_attempt_at),
        "reason": j.reason,
        "terminal_failure": j.terminal_failure,
        "sent_at": _iso(j.sent_at),
        "updating_at": _iso(j.updating_at),
        "finished_at": _iso(j.finished_at),
    }


def event_json(event_id: int, e: JobEvent) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "ts": e.ts.isoformat(),
        "event": e.event,
        "hub_id": e.hub_id,
        "job_id": str(e.job_id) if e.job_id else None,
        "from_state": e.from_state,
        "to_state": e.to_state,
        "reason": e.reason,
        "attempt": e.attempt,
        "detail": e.detail,
    }


async def _catalogue(repo: FirmwareRepo, cfg: FirmwareConfig) -> Catalogue:
    return Catalogue.build(cfg.catalogue, await repo.catalogue_rows())


# ---------------------------------------------------------------------------------------------- reads


@router.get("/catalogue")
async def get_catalogue(
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    cfg: Annotated[FirmwareConfig, Depends(get_firmware_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    catalogue = await _catalogue(repo, cfg)
    return {"entries": catalogue.as_json(), "versions": catalogue.versions()}


@router.get("/campaigns")
async def list_campaigns(
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {"campaigns": [campaign_json(c) for c in await repo.list_campaigns(limit=limit)]}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown campaign")
    jobs = await repo.jobs(campaign_id)
    return {
        "campaign": campaign_json(campaign),
        "counts": job_counts(jobs),
        "jobs": [job_json(j) for j in jobs],
    }


@router.get("/campaigns/{campaign_id}/events")
async def campaign_events(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    if await repo.get_campaign(campaign_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown campaign")
    rows = await repo.events(campaign_id, after_id=after, limit=limit)
    return {"events": [event_json(i, e) for i, e in rows], "next_after": rows[-1][0] if rows else after}


# ---------------------------------------------------------------------------------------------- writes


@router.post("/campaigns", status_code=status.HTTP_202_ACCEPTED)
async def propose_campaign(
    body: CampaignBody,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    cfg: Annotated[FirmwareConfig, Depends(get_firmware_config)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 1 of 2: nothing is sent yet. Confirm within the proposal TTL."""
    s = body.selection
    request = svc.CampaignRequest(
        name=body.name,
        target_version=body.target_version,
        selection=HubSelection(
            tuple(s.hub_ids),
            tuple(s.bank_ids),
            tuple(s.zones),
            tuple(s.feeder_ids),
            tuple(s.hardware_revisions),
            tuple(s.firmware_versions),
        ),
        reason=body.reason,
        waves=WaveSpec(
            body.waves.canary, body.waves.canary_pct, tuple(body.waves.sizes), body.waves.size_pct
        ),
        bank_max_concurrent_pct=body.bank_max_concurrent_pct,
        feeder_max_concurrent_pct=body.feeder_max_concurrent_pct,
        max_failures=body.max_failures,
        max_failure_pct=body.max_failure_pct,
        window_start=body.window_start,
        window_end=body.window_end,
        allow_downgrade=body.allow_downgrade,
        override_committed=body.override_committed,
    )
    try:
        created = await svc.create_campaign(
            repo,
            request,
            proposer=identity.user,
            catalogue=await _catalogue(repo, cfg),
            cfg=cfg,
            now=_now(),
            tracer=_tracer(trace_store),
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {
        "campaign": campaign_json(created.campaign),
        "counts": job_counts(created.jobs),
        "committed_hub_ids": created.committed_hub_ids,
        "skipped": created.skipped,
        "expires_in_s": PROPOSAL_TTL_S,
        "summary": (
            f"Update {len(created.jobs) - len(created.skipped)} hubs to {created.campaign.target_version} in "
            f"{len(created.campaign.waves)} waves"
            + ("; needs a second operator" if created.campaign.requires_second_operator else "")
        ),
    }


@router.post("/campaigns/{campaign_id}/confirm")
async def confirm_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    try:
        campaign = await svc.confirm_campaign(
            repo,
            campaign_id,
            operator=identity.user,
            now=_now(),
            ttl_s=PROPOSAL_TTL_S,
            tracer=_tracer(trace_store),
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {"campaign": campaign_json(campaign)}


@router.post("/campaigns/{campaign_id}/approve")
async def approve_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    try:
        campaign = await svc.approve_campaign(
            repo, campaign_id, operator=identity.user, now=_now(), tracer=_tracer(trace_store)
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {"campaign": campaign_json(campaign)}


async def _simple_action(
    handler: Any, campaign_id: UUID, repo: FirmwareRepo, trace_store: TraceStore, identity: Identity
) -> dict[str, Any]:
    try:
        campaign = await handler(
            repo, campaign_id, operator=identity.user, now=_now(), tracer=_tracer(trace_store)
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {"campaign": campaign_json(campaign)}


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Stop starting new hubs; hubs already updating finish."""
    return await _simple_action(svc.pause_campaign, campaign_id, repo, trace_store, identity)


@router.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    return await _simple_action(svc.resume_campaign, campaign_id, repo, trace_store, identity)


@router.post("/campaigns/{campaign_id}/abort")
async def abort_campaign(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    return await _simple_action(svc.abort_campaign, campaign_id, repo, trace_store, identity)


@router.post("/campaigns/{campaign_id}/retry-failed", status_code=status.HTTP_202_ACCEPTED)
async def propose_retry_failed(
    campaign_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 1 of 2 of "retry failed hubs"."""
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown campaign")
    failed = [j.hub_id for j in await repo.jobs(campaign_id) if j.state.value == "FAILED"]
    if not failed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no failed hubs to retry")
    summary = f"Retry {len(failed)} failed hubs of {campaign.name}"
    proposal = proposals.create(_RETRY_KIND, campaign_id, summary, identity.user)
    return {
        "proposal_id": str(proposal.proposal_id),
        "summary": summary,
        "hub_ids": failed,
        "expires_in_s": PROPOSAL_TTL_S,
    }


@router.post("/campaigns/{campaign_id}/retry-failed/{proposal_id}/confirm")
async def confirm_retry_failed(
    campaign_id: UUID,
    proposal_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    if _pop(proposals, proposal_id, _RETRY_KIND) != campaign_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="proposal is for another campaign")
    try:
        campaign, count = await svc.retry_failed_hubs(
            repo, campaign_id, operator=identity.user, now=_now(), tracer=_tracer(trace_store)
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {"campaign": campaign_json(campaign), "retried": count}


@router.post("/campaigns/{campaign_id}/hubs/{hub_id}/rollback", status_code=status.HTTP_202_ACCEPTED)
async def propose_rollback(
    campaign_id: UUID,
    hub_id: str,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    job = next((j for j in await repo.jobs(campaign_id) if j.hub_id == hub_id), None)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub is not in this campaign")
    if job.from_version is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the hub's previous version is unknown")
    summary = f"Roll {hub_id} back to {job.from_version}"
    proposal = proposals.create(_ROLLBACK_KIND, (campaign_id, hub_id), summary, identity.user)
    return {"proposal_id": str(proposal.proposal_id), "summary": summary, "expires_in_s": PROPOSAL_TTL_S}


@router.post("/campaigns/{campaign_id}/hubs/{hub_id}/rollback/{proposal_id}/confirm")
async def confirm_rollback(
    campaign_id: UUID,
    hub_id: str,
    proposal_id: UUID,
    repo: Annotated[FirmwareRepo, Depends(get_firmware_repo)],
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    if _pop(proposals, proposal_id, _ROLLBACK_KIND) != (campaign_id, hub_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="proposal is for another hub or campaign")
    try:
        job = await svc.rollback_hub(
            repo, campaign_id, hub_id, operator=identity.user, now=_now(), tracer=_tracer(trace_store)
        )
    except svc.CampaignError as exc:
        raise _http(exc) from exc
    return {"job": job_json(job)}


def _pop(proposals: ProposalStore, proposal_id: UUID, kind: str) -> Any:
    try:
        return proposals.pop(proposal_id, kind=kind).body
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
