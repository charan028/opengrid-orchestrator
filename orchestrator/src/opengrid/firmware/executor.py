"""The engine's firmware executor (R3.1). Called once per engine cycle (`await executor.step(now)`):

1. APPROVED campaigns start (RUNNING, event CAMPAIGN_STARTED);
2. every job of an active campaign advances from its command row (the guardian's SIGNED/REFUSED verdict and
   the hub's timestamped status) and the hub's device-info firmware_version (`planner.advance_job`);
   transient failures retry with exponential backoff, terminal ones alert (ALR-FIRMWARE-HUB-FAILED);
3. a RUNNING campaign halts on its failure threshold (ALR-FIRMWARE-CAMPAIGN-HALTED, critical) or completes
   (ALR-FIRMWARE-CAMPAIGN-COMPLETED, info);
4. the next jobs are requested from the guardian (`og.firmware_command` REQUESTED rows) within the waves,
   the maintenance window and the bank/feeder caps.

`updating_hub_ids()` is what the allocator must exclude this cycle (hubs SENT/UPDATING/requested): their
committed kW moves to other hubs exactly as for a manual target (K13 substitution). The executor never
signs or publishes anything (K3): the guardian does, after its own G-36 check.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from opengrid.firmware.catalogue import Catalogue
from opengrid.firmware.config import FirmwareConfig
from opengrid.firmware.model import (
    IN_FLIGHT_JOB_STATES,
    Campaign,
    CampaignState,
    Job,
    JobEvent,
    JobState,
)
from opengrid.firmware.planner import (
    RetryPolicy,
    advance_job,
    campaign_event,
    halt_reason,
    is_complete,
    job_counts,
    plan_dispatch,
)
from opengrid.firmware.repo import CommandRequest, FirmwareRepo, HubSelection

logger = logging.getLogger(__name__)

ALR_CAMPAIGN_HALTED: Final = "ALR-FIRMWARE-CAMPAIGN-HALTED"
ALR_CAMPAIGN_COMPLETED: Final = "ALR-FIRMWARE-CAMPAIGN-COMPLETED"
ALR_CAMPAIGN_STARTED: Final = "ALR-FIRMWARE-CAMPAIGN-STARTED"
ALR_HUB_FAILED: Final = "ALR-FIRMWARE-HUB-FAILED"

#: `(rule, severity, summary, detail)` -> raised through health's single `og.alert` writer.
AlertSink = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
#: `(stream_id, event_class, payload)` -> trace append (K10). Best-effort for executor transitions.
TraceSink = Callable[[str, str, dict[str, Any]], Awaitable[object]]
CatalogueSource = Callable[[], Awaitable[Catalogue]]


@dataclass(slots=True)
class StepResult:
    requested: int = 0
    transitions: int = 0
    halted: int = 0
    completed: int = 0


class FirmwareExecutor:
    def __init__(
        self,
        repo: FirmwareRepo,
        cfg: FirmwareConfig,
        catalogue: CatalogueSource,
        *,
        alerts: AlertSink | None = None,
        trace: TraceSink | None = None,
    ) -> None:
        self._repo = repo
        self._cfg = cfg
        self._catalogue = catalogue
        self._alerts = alerts
        self._trace = trace
        self._policy = RetryPolicy(
            max_attempts=cfg.max_attempts,
            initial_s=cfg.retry_initial_s,
            max_s=cfg.retry_max_s,
            refusal_defer_s=cfg.refusal_defer_s,
            update_timeout_s=cfg.update_timeout_s,
        )
        self._excluded: frozenset[str] = frozenset()

    def updating_hub_ids(self) -> frozenset[str]:
        """Hubs the allocator must not dispatch this cycle (as of the last `step`)."""
        return self._excluded

    async def step(self, now: datetime) -> StepResult:
        result = StepResult()
        excluded: set[str] = set()
        catalogue: Catalogue | None = None
        for campaign in await self._repo.active_campaigns():
            try:
                if catalogue is None:
                    catalogue = await self._catalogue()
                await self._step_campaign(campaign, catalogue, now, result, excluded)
            except Exception:
                logger.exception(
                    "firmware campaign step failed", extra={"campaign_id": str(campaign.campaign_id)}
                )
        self._excluded = frozenset(excluded)
        return result

    async def _step_campaign(
        self, campaign: Campaign, catalogue: Catalogue, now: datetime, result: StepResult, excluded: set[str]
    ) -> None:
        if campaign.state == CampaignState.APPROVED:
            await self._set_state(campaign, CampaignState.RUNNING, "CAMPAIGN_STARTED", now)
            campaign.started_at = now
            await self._alert(
                ALR_CAMPAIGN_STARTED, "info", f"firmware campaign {campaign.name} started", campaign, {}
            )

        jobs = await self._repo.jobs(campaign.campaign_id)
        commands = await self._repo.commands(j.command_id for j in jobs if j.command_id is not None)
        versions = await self._repo.hub_versions(j.hub_id for j in jobs if j.state in IN_FLIGHT_JOB_STATES)
        changed: dict[Any, Job] = {}
        events: list[JobEvent] = []
        for job in jobs:
            command = commands.get(job.command_id) if job.command_id is not None else None
            produced = advance_job(
                job, command, observed_version=versions.get(job.hub_id), now=now, policy=self._policy
            )
            if produced:
                changed[job.job_id] = job
                events.extend(produced)
                for event in produced:
                    if event.event == "FAILED":
                        await self._alert(
                            ALR_HUB_FAILED,
                            "warning",
                            f"firmware update failed on {job.hub_id}: {event.reason}",
                            campaign,
                            {
                                "scope_kind": "hub",
                                "scope_ref": job.hub_id,
                                "hub_id": job.hub_id,
                                "reason": event.reason,
                                "attempt": event.attempt,
                            },
                        )

        if campaign.state == CampaignState.RUNNING:
            reason = halt_reason(campaign, jobs)
            if reason is not None:
                campaign.halt_reason = reason
                await self._set_state(campaign, CampaignState.HALTED, "CAMPAIGN_HALTED", now, reason=reason)
                result.halted += 1
                await self._alert(
                    ALR_CAMPAIGN_HALTED,
                    "critical",
                    f"firmware campaign {campaign.name} halted: {reason}",
                    campaign,
                    {"reason": reason, "counts": job_counts(jobs)},
                )

        if campaign.state == CampaignState.RUNNING and is_complete(jobs):
            campaign.finished_at = now
            await self._set_state(campaign, CampaignState.COMPLETED, "CAMPAIGN_COMPLETED", now)
            result.completed += 1
            await self._alert(
                ALR_CAMPAIGN_COMPLETED,
                "info",
                f"firmware campaign {campaign.name} completed",
                campaign,
                {"counts": job_counts(jobs)},
            )

        requests: list[CommandRequest] = []
        counts = await self._repo.fleet_counts()
        for job in plan_dispatch(campaign, jobs, counts, now):
            revision = await self._hub_revision(job)
            entry = catalogue.lookup(job.target_version, revision)
            if entry is None or revision is None:
                # Terminal: the catalogue no longer carries this image for the hub's hardware.
                before = job.state
                job.state, job.terminal_failure, job.finished_at = JobState.FAILED, True, now
                job.reason = "FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE"
                changed[job.job_id] = job
                events.append(
                    JobEvent(
                        campaign.campaign_id,
                        "FAILED",
                        now,
                        job.job_id,
                        job.hub_id,
                        before.value,
                        job.state.value,
                        job.reason,
                        job.attempts,
                    )
                )
                continue
            request = CommandRequest(
                job=job,
                sha256=entry.sha256,
                hardware_revision=revision,
                issued_at=now,
                expires_at=now + timedelta(seconds=self._cfg.command_lease_s),
            )
            requests.append(request)
            job.command_id = request.command_id
            events.append(
                JobEvent(
                    campaign.campaign_id,
                    "REQUESTED",
                    now,
                    job.job_id,
                    job.hub_id,
                    job.state.value,
                    job.state.value,
                    None,
                    job.attempts + 1,
                )
            )
        await self._repo.save_jobs(list(changed.values()), events)
        await self._repo.request_commands(requests)
        result.requested += len(requests)
        result.transitions += len(events)
        await self._trace_events(campaign, events)
        excluded.update(j.hub_id for j in jobs if j.state in IN_FLIGHT_JOB_STATES or j.command_id is not None)

    async def _hub_revision(self, job: Job) -> str | None:
        rows = await self._repo.resolve_hubs(HubSelection(hub_ids=(job.hub_id,)))
        return rows[0].hardware_revision if rows else None

    async def _set_state(
        self,
        campaign: Campaign,
        target: CampaignState,
        event: str,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        before = campaign.state
        campaign.state = target
        await self._repo.save_campaign(
            campaign,
            [
                campaign_event(
                    campaign.campaign_id,
                    event,
                    now,
                    from_state=before.value,
                    to_state=target.value,
                    reason=reason,
                )
            ],
        )
        if self._trace is not None:
            try:
                await self._trace(
                    f"firmware_campaign:{campaign.campaign_id}",
                    "FIRMWARE_CAMPAIGN",
                    {
                        "decision_ref": f"FIRMWARE_CAMPAIGN:{campaign.campaign_id}",
                        "event": event,
                        "from": before.value,
                        "to": target.value,
                        "reason": reason,
                    },
                )
            except Exception:
                logger.exception("could not trace a firmware campaign transition")

    async def _trace_events(self, campaign: Campaign, events: list[JobEvent]) -> None:
        if self._trace is None or not events:
            return
        try:
            await self._trace(
                f"firmware_campaign:{campaign.campaign_id}",
                "FIRMWARE_CAMPAIGN",
                {
                    "decision_ref": f"FIRMWARE_CAMPAIGN:{campaign.campaign_id}",
                    "events": [
                        {
                            "hub_id": e.hub_id,
                            "event": e.event,
                            "from": e.from_state,
                            "to": e.to_state,
                            "reason": e.reason,
                            "attempt": e.attempt,
                            "ts": e.ts.isoformat(),
                        }
                        for e in events
                    ],
                },
            )
        except Exception:
            logger.exception("could not trace firmware job transitions")

    async def _alert(
        self, rule: str, severity: str, summary: str, campaign: Campaign, detail: dict[str, Any]
    ) -> None:
        if self._alerts is None:
            return
        full = {"campaign_id": str(campaign.campaign_id), "campaign": campaign.name, **detail}
        full.setdefault("scope_kind", "fleet")
        full.setdefault("scope_ref", str(campaign.campaign_id))
        try:
            await self._alerts(rule, severity, summary, full)
        except Exception:
            logger.exception("could not raise firmware alert", extra={"rule": rule})


def pg_alert_sink(pool: Any) -> AlertSink:
    """Alerts through health's single `og.alert` writer (`opengrid.health.queries.raise_alert`), so they land
    in the console alerts panel with bulk ack. Each call raises one alert (every hub failure is its own)."""
    from opengrid.health.model import AlertFinding, AlertSeverity
    from opengrid.health.queries import raise_alert

    async def sink(rule: str, severity: str, summary: str, detail: dict[str, Any]) -> None:
        condition = f"{rule}:{detail.get('campaign_id')}:{detail.get('hub_id', '')}"
        finding = AlertFinding(
            rule=rule,
            # "info" is additive to health's Literal (requested from HEALTH); og.alert.severity is free text.
            severity=cast(AlertSeverity, severity),
            summary=summary,
            condition_key=condition,
            detail=detail,
        )
        await raise_alert(pool, finding, opened_at=datetime.now(UTC))

    return sink
