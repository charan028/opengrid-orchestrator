"""Operator-side campaign lifecycle (R3.1): create (propose), confirm, second-operator approve, pause,
resume, abort, retry failed hubs and per-hub rollback. Each is a state change validated against
`CAMPAIGN_TRANSITIONS`, persisted with a timestamped event and traced (K10: the trace row is written first;
a failed trace refuses the action). The HTTP layer (`opengrid.api.routers.firmware`) stays thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from opengrid.firmware.catalogue import Catalogue, hub_target_problem
from opengrid.firmware.config import FirmwareConfig
from opengrid.firmware.model import (
    CAMPAIGN_TRANSITIONS,
    IN_FLIGHT_JOB_STATES,
    Campaign,
    CampaignState,
    Job,
    JobEvent,
    JobState,
    WaveSpec,
    is_downgrade,
)
from opengrid.firmware.planner import (
    assign_waves,
    build_waves,
    campaign_event,
    order_for_waves,
    request_rollback,
    reset_failed_for_retry,
    second_operator_reasons,
)
from opengrid.firmware.repo import FirmwareRepo, HubSelection

TRACE_EVENT_CLASS: Final = "FIRMWARE_CAMPAIGN"
TRACE_DECISION_TYPE: Final = "OPERATOR_ACTION"


class CampaignError(Exception):
    """A refused operator action. `status` is the HTTP status the API maps it to."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Tracer(Protocol):
    async def __call__(self, stream_id: str, event_class: str, payload: dict[str, Any]) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class CampaignRequest:
    name: str
    target_version: str
    selection: HubSelection
    reason: str
    waves: WaveSpec = field(default_factory=WaveSpec)
    bank_max_concurrent_pct: float | None = None
    feeder_max_concurrent_pct: float | None = None
    max_failures: int | None = None
    max_failure_pct: float | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    allow_downgrade: bool = False
    override_committed: bool = False


@dataclass(frozen=True, slots=True)
class CreatedCampaign:
    campaign: Campaign
    jobs: list[Job]
    committed_hub_ids: list[str]
    skipped: dict[str, str]


async def create_campaign(
    repo: FirmwareRepo,
    request: CampaignRequest,
    *,
    proposer: str,
    catalogue: Catalogue,
    cfg: FirmwareConfig,
    now: datetime,
    tracer: Tracer | None = None,
) -> CreatedCampaign:
    """Step 1 (propose): validate the target against the catalogue (no arbitrary versions), resolve the
    selection to hub ids ONCE, split them into waves, flag hubs that cannot take the image (SKIPPED with the
    reason) and decide whether a second operator must approve. Persists a DRAFT."""
    if not catalogue.has_version(request.target_version):
        raise CampaignError(422, f"version {request.target_version!r} is not in the firmware catalogue")
    if request.selection.is_empty():
        raise CampaignError(422, "select hubs by explicit ids or a filter")
    if request.window_start and request.window_end and request.window_end <= request.window_start:
        raise CampaignError(422, "maintenance window end must be after its start")
    hubs = await repo.resolve_hubs(request.selection)
    if request.selection.hub_ids:
        missing = sorted(set(request.selection.hub_ids) - {h.hub_id for h in hubs})
        if missing:
            raise CampaignError(422, f"unknown hub ids: {', '.join(missing[:10])}")
    if not hubs:
        raise CampaignError(422, "the selection matches no hubs")

    campaign_id = uuid4()
    skipped: dict[str, str] = {}
    downgrade = False
    for hub in hubs:
        problem = hub_target_problem(
            catalogue,
            target_version=request.target_version,
            hardware_revision=hub.hardware_revision,
            current_version=hub.firmware_version,
            allow_downgrade=request.allow_downgrade,
        )
        if problem is not None:
            skipped[hub.hub_id] = problem
        elif is_downgrade(hub.firmware_version, request.target_version):
            downgrade = True
    eligible = [h for h in hubs if h.hub_id not in skipped]
    if not eligible:
        raise CampaignError(422, "no selected hub can take this version (all would be skipped)")

    committed = sorted(
        await repo.committed_hub_ids(
            [h.hub_id for h in eligible], until=now + timedelta(seconds=cfg.committed_lookahead_s)
        )
    )
    reasons = second_operator_reasons(
        hub_count=len(eligible),
        threshold=cfg.second_operator_hub_threshold,
        committed_hub_ids=committed,
        override_committed=request.override_committed,
        downgrade=downgrade,
    )
    waves = build_waves(len(eligible), request.waves)
    wave_of = assign_waves(order_for_waves([(h.hub_id, h.bank_id) for h in eligible]), waves)
    campaign = Campaign(
        campaign_id=campaign_id,
        name=request.name,
        target_version=request.target_version,
        state=CampaignState.DRAFT,
        hub_ids=[h.hub_id for h in hubs],
        waves=waves,
        reason=request.reason,
        proposed_by=proposer,
        created_at=now,
        selection=request.selection.as_json(),
        bank_max_concurrent_pct=request.bank_max_concurrent_pct or cfg.bank_max_concurrent_pct,
        feeder_max_concurrent_pct=request.feeder_max_concurrent_pct or cfg.feeder_max_concurrent_pct,
        max_failures=request.max_failures or cfg.max_failures,
        max_failure_pct=request.max_failure_pct or cfg.max_failure_pct,
        window_start=request.window_start,
        window_end=request.window_end,
        allow_downgrade=request.allow_downgrade,
        override_committed=request.override_committed,
        requires_second_operator=bool(reasons),
        second_operator_reasons=reasons,
    )
    jobs = [
        Job(
            job_id=uuid4(),
            campaign_id=campaign_id,
            hub_id=h.hub_id,
            bank_id=h.bank_id,
            feeder_id=h.feeder_id,
            wave=wave_of.get(h.hub_id, 0),
            target_version=request.target_version,
            state=JobState.SKIPPED if h.hub_id in skipped else JobState.PENDING,
            from_version=h.firmware_version,
            reason=skipped.get(h.hub_id),
            finished_at=now if h.hub_id in skipped else None,
        )
        for h in hubs
    ]
    campaign.trace_id = await _trace(
        tracer,
        campaign,
        "PROPOSED",
        proposer,
        {
            "hub_count": len(hubs),
            "skipped": len(skipped),
            "waves": waves,
            "committed_hub_ids": committed,
            "second_operator_reasons": reasons,
        },
    )
    events = [
        campaign_event(
            campaign_id,
            "CAMPAIGN_CREATED",
            now,
            from_state=None,
            to_state="DRAFT",
            detail={"by": proposer, "hubs": len(hubs), "skipped": len(skipped)},
        )
    ]
    events += [
        JobEvent(campaign_id, "SKIPPED", now, j.job_id, j.hub_id, None, "SKIPPED", j.reason, 0)
        for j in jobs
        if j.state == JobState.SKIPPED
    ]
    await repo.create_campaign(campaign, jobs, events)
    return CreatedCampaign(campaign, jobs, committed, skipped)


async def _load(repo: FirmwareRepo, campaign_id: UUID) -> Campaign:
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise CampaignError(404, "unknown campaign")
    return campaign


def _require_transition(campaign: Campaign, target: CampaignState) -> None:
    if target not in CAMPAIGN_TRANSITIONS[campaign.state]:
        raise CampaignError(409, f"campaign is {campaign.state.value}; cannot move to {target.value}")


async def _trace(
    tracer: Tracer | None, campaign: Campaign, action: str, operator: str, extra: dict[str, Any] | None = None
) -> UUID | None:
    if tracer is None:
        return campaign.trace_id
    payload = {
        "decision_ref": f"FIRMWARE_CAMPAIGN:{campaign.campaign_id}",
        "action": action,
        "campaign_id": str(campaign.campaign_id),
        "target_version": campaign.target_version,
        "operator": operator,
        **(extra or {}),
    }
    return await tracer(f"operator_action:{operator}", TRACE_EVENT_CLASS, payload)


async def _transition(
    repo: FirmwareRepo,
    campaign: Campaign,
    target: CampaignState,
    *,
    event: str,
    operator: str,
    now: datetime,
    tracer: Tracer | None,
    reason: str | None = None,
) -> Campaign:
    _require_transition(campaign, target)
    before = campaign.state
    campaign.state = target
    campaign.trace_id = await _trace(tracer, campaign, event, operator, {"reason": reason})
    if target in (CampaignState.ABORTED, CampaignState.COMPLETED):
        campaign.finished_at = now
    await repo.save_campaign(
        campaign,
        [
            campaign_event(
                campaign.campaign_id,
                event,
                now,
                from_state=before.value,
                to_state=target.value,
                reason=reason,
                detail={"by": operator},
            )
        ],
    )
    return campaign


async def confirm_campaign(
    repo: FirmwareRepo,
    campaign_id: UUID,
    *,
    operator: str,
    now: datetime,
    ttl_s: float,
    tracer: Tracer | None = None,
) -> Campaign:
    """Step 2 (confirm, within the proposal TTL): DRAFT -> APPROVED, or -> PROPOSED when a second operator
    must approve. An expired draft is aborted (propose again)."""
    campaign = await _load(repo, campaign_id)
    if campaign.state != CampaignState.DRAFT:
        raise CampaignError(409, f"campaign is {campaign.state.value}, not DRAFT")
    if (now - campaign.created_at).total_seconds() > ttl_s:
        await _transition(
            repo,
            campaign,
            CampaignState.ABORTED,
            event="CAMPAIGN_ABORTED",
            operator=operator,
            now=now,
            tracer=tracer,
            reason="PROPOSAL_EXPIRED",
        )
        raise CampaignError(409, "proposal expired, propose again")
    campaign.confirmed_by, campaign.confirmed_at = operator, now
    if campaign.requires_second_operator:
        return await _transition(
            repo,
            campaign,
            CampaignState.PROPOSED,
            event="CAMPAIGN_CONFIRMED",
            operator=operator,
            now=now,
            tracer=tracer,
            reason="AWAITING_SECOND_OPERATOR",
        )
    campaign.approved_by, campaign.approved_at = operator, now
    return await _transition(
        repo,
        campaign,
        CampaignState.APPROVED,
        event="CAMPAIGN_APPROVED",
        operator=operator,
        now=now,
        tracer=tracer,
    )


async def approve_campaign(
    repo: FirmwareRepo, campaign_id: UUID, *, operator: str, now: datetime, tracer: Tracer | None = None
) -> Campaign:
    """Second-operator approval (two-person rule): PROPOSED -> APPROVED, never by the proposer or the
    confirmer."""
    campaign = await _load(repo, campaign_id)
    if campaign.state != CampaignState.PROPOSED:
        raise CampaignError(409, f"campaign is {campaign.state.value}, not awaiting a second operator")
    same = {campaign.proposed_by.casefold(), (campaign.confirmed_by or "").casefold()}
    if operator.casefold() in same:
        raise CampaignError(403, "this campaign needs a second operator; the proposer cannot approve it")
    campaign.approved_by, campaign.approved_at = operator, now
    return await _transition(
        repo,
        campaign,
        CampaignState.APPROVED,
        event="CAMPAIGN_APPROVED",
        operator=operator,
        now=now,
        tracer=tracer,
    )


async def pause_campaign(
    repo: FirmwareRepo, campaign_id: UUID, *, operator: str, now: datetime, tracer: Tracer | None = None
) -> Campaign:
    campaign = await _load(repo, campaign_id)
    return await _transition(
        repo,
        campaign,
        CampaignState.PAUSED,
        event="CAMPAIGN_PAUSED",
        operator=operator,
        now=now,
        tracer=tracer,
    )


async def resume_campaign(
    repo: FirmwareRepo, campaign_id: UUID, *, operator: str, now: datetime, tracer: Tracer | None = None
) -> Campaign:
    campaign = await _load(repo, campaign_id)
    if campaign.state != CampaignState.PAUSED:
        raise CampaignError(409, f"only a PAUSED campaign resumes (it is {campaign.state.value})")
    return await _transition(
        repo,
        campaign,
        CampaignState.RUNNING,
        event="CAMPAIGN_RESUMED",
        operator=operator,
        now=now,
        tracer=tracer,
    )


async def abort_campaign(
    repo: FirmwareRepo, campaign_id: UUID, *, operator: str, now: datetime, tracer: Tracer | None = None
) -> Campaign:
    """Abort: nothing new is requested or signed. Hubs already updating finish on their own (a hub
    mid-install cannot be recalled); pending jobs become SKIPPED."""
    campaign = await _load(repo, campaign_id)
    campaign = await _transition(
        repo,
        campaign,
        CampaignState.ABORTED,
        event="CAMPAIGN_ABORTED",
        operator=operator,
        now=now,
        tracer=tracer,
    )
    changed: list[Job] = []
    events: list[JobEvent] = []
    for job in await repo.jobs(campaign_id):
        if job.state == JobState.PENDING:
            job.state, job.reason, job.finished_at, job.command_id = (
                JobState.SKIPPED,
                "CAMPAIGN_ABORTED",
                now,
                None,
            )
            changed.append(job)
            events.append(
                JobEvent(
                    campaign_id,
                    "SKIPPED",
                    now,
                    job.job_id,
                    job.hub_id,
                    "PENDING",
                    "SKIPPED",
                    job.reason,
                    job.attempts,
                )
            )
    await repo.save_jobs(changed, events)
    return campaign


async def retry_failed_hubs(
    repo: FirmwareRepo, campaign_id: UUID, *, operator: str, now: datetime, tracer: Tracer | None = None
) -> tuple[Campaign, int]:
    """The operator's "retry failed hubs" (confirmed step of a two-step action): FAILED jobs back to PENDING
    with a fresh attempt budget. A HALTED campaign becomes PAUSED (the operator then resumes it); a COMPLETED
    one runs again."""
    campaign = await _load(repo, campaign_id)
    if campaign.state not in (
        CampaignState.RUNNING,
        CampaignState.PAUSED,
        CampaignState.HALTED,
        CampaignState.COMPLETED,
    ):
        raise CampaignError(409, f"campaign is {campaign.state.value}; nothing to retry")
    jobs = await repo.jobs(campaign_id)
    events = reset_failed_for_retry(jobs, now)
    if not events:
        raise CampaignError(409, "no failed hubs to retry")
    changed = [j for j in jobs if j.job_id in {e.job_id for e in events}]
    await repo.save_jobs(changed, events)
    target = {
        CampaignState.HALTED: CampaignState.PAUSED,
        CampaignState.COMPLETED: CampaignState.RUNNING,
    }.get(campaign.state)
    if target is not None:
        campaign.halt_reason = None
        campaign.finished_at = None
        campaign = await _transition(
            repo,
            campaign,
            target,
            event="CAMPAIGN_RETRY_FAILED",
            operator=operator,
            now=now,
            tracer=tracer,
            reason=f"{len(changed)} hubs",
        )
    else:
        campaign.trace_id = await _trace(
            tracer, campaign, "CAMPAIGN_RETRY_FAILED", operator, {"hubs": len(changed)}
        )
    return campaign, len(changed)


async def rollback_hub(
    repo: FirmwareRepo,
    campaign_id: UUID,
    hub_id: str,
    *,
    operator: str,
    now: datetime,
    tracer: Tracer | None = None,
) -> Job:
    """Per-hub rollback (confirmed step of a two-step action): the job becomes a ROLLBACK to the version the
    hub ran before the campaign; the executor requests it and the guardian signs it (G-36 allows a
    rollback in a paused, halted or completed campaign)."""
    campaign = await _load(repo, campaign_id)
    if campaign.state in (
        CampaignState.ABORTED,
        CampaignState.DRAFT,
        CampaignState.PROPOSED,
        CampaignState.APPROVED,
    ):
        raise CampaignError(409, f"campaign is {campaign.state.value}; nothing to roll back")
    job = next((j for j in await repo.jobs(campaign_id) if j.hub_id == hub_id), None)
    if job is None:
        raise CampaignError(404, "hub is not in this campaign")
    if job.state in IN_FLIGHT_JOB_STATES:
        raise CampaignError(409, "hub is updating; roll back once it finishes")
    events = request_rollback(job, now)
    if not events:
        raise CampaignError(409, "nothing to roll back (unknown previous version, or not updated)")
    await _trace(
        tracer, campaign, "HUB_ROLLBACK", operator, {"hub_id": hub_id, "to_version": job.target_version}
    )
    await repo.save_jobs([job], events)
    return job
