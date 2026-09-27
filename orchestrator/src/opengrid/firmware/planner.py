"""Pure firmware-campaign planning (R3.1): waves, dispatch under the bank/feeder concurrency caps and the
maintenance window, the job state machine driven by the guardian's verdict and the hub's status, retries
with exponential backoff, and the halt/complete decisions. No I/O -- `opengrid.firmware.executor` reads
the inputs and persists what these functions return.

Every function that moves a job appends timestamped `JobEvent`s (owner addition 1) and mutates only the
`Job` it was given.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from opengrid.firmware.model import (
    ALREADY_AT_TARGET,
    IN_FLIGHT_JOB_STATES,
    TERMINAL_JOB_STATES,
    TERMINAL_REFUSALS,
    TRANSIENT_FAILURES,
    Campaign,
    CampaignState,
    CommandRecord,
    Job,
    JobEvent,
    JobState,
    WaveSpec,
    concurrency_cap,
)

#: Why a campaign needs a second operator's approval (same two-person pattern as the safe-stop release).
REASON_LARGE_CAMPAIGN: Final = "MORE_THAN_THRESHOLD_HUBS"
REASON_COMMITTED_OBLIGATION: Final = "HUB_SERVES_COMMITTED_OBLIGATION"
REASON_DOWNGRADE: Final = "DOWNGRADE"

HALT_FAILURE_THRESHOLD: Final = "FAILURE_THRESHOLD_REACHED"
HALT_CANARY_FAILED: Final = "CANARY_WAVE_HAD_NO_SUCCESS"

_UPDATING_HUB_STATES: Final = frozenset({"DOWNLOADING", "INSTALLING", "REBOOTING"})


# ---------------------------------------------------------------------------------------------- waves


def build_waves(hub_count: int, spec: WaveSpec) -> list[int]:
    """Hub count per wave: the canary (N hubs or % of the campaign, at least 1), then waves of the given
    sizes (repeating the last) or of `size_pct`% each, until every hub is placed."""
    if hub_count <= 0:
        return []
    canary = spec.canary
    if spec.canary_pct is not None:
        canary = int(hub_count * spec.canary_pct / 100.0)
    waves = [min(hub_count, max(1, canary))]
    remaining = hub_count - waves[0]
    sizes = list(spec.sizes)
    step_pct = max(1, int(hub_count * (spec.size_pct or 100.0) / 100.0))
    index = 0
    while remaining > 0:
        size = sizes[min(index, len(sizes) - 1)] if sizes else step_pct
        size = max(1, min(size, remaining))
        waves.append(size)
        remaining -= size
        index += 1
    return waves


def order_for_waves(hubs: Sequence[tuple[str, str]]) -> list[str]:
    """Hub ids `(hub_id, bank_id)` interleaved round-robin across banks, so the canary and every wave are
    spread over as many banks as possible (a bad image never takes one bank down first)."""
    by_bank: dict[str, list[str]] = defaultdict(list)
    for hub_id, bank_id in sorted(hubs):
        by_bank[bank_id].append(hub_id)
    queues = [by_bank[b] for b in sorted(by_bank)]
    ordered: list[str] = []
    depth = 0
    while len(ordered) < len(hubs):
        for queue in queues:
            if depth < len(queue):
                ordered.append(queue[depth])
        depth += 1
    return ordered


def assign_waves(ordered_hub_ids: Sequence[str], waves: Sequence[int]) -> dict[str, int]:
    assignment: dict[str, int] = {}
    cursor = 0
    for wave_index, size in enumerate(waves):
        for hub_id in ordered_hub_ids[cursor : cursor + size]:
            assignment[hub_id] = wave_index
        cursor += size
    return assignment


def second_operator_reasons(
    *,
    hub_count: int,
    threshold: int,
    committed_hub_ids: Iterable[str],
    override_committed: bool,
    downgrade: bool,
) -> list[str]:
    """Why the campaign needs a second operator (empty = the proposer's own confirm suffices)."""
    reasons: list[str] = []
    if hub_count > threshold:
        reasons.append(REASON_LARGE_CAMPAIGN)
    if override_committed and any(True for _ in committed_hub_ids):
        reasons.append(REASON_COMMITTED_OBLIGATION)
    if downgrade:
        reasons.append(REASON_DOWNGRADE)
    return reasons


# ---------------------------------------------------------------------------------------------- retries


def retry_delay_s(attempt: int, *, initial_s: float, max_s: float) -> float:
    """Exponential backoff after failed attempt `attempt` (1-based): initial, 2x, 4x, ... capped at max."""
    return float(min(max_s, initial_s * (2 ** max(0, attempt - 1))))


def is_transient(reason: str | None) -> bool:
    return reason in TRANSIENT_FAILURES


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_s: float = 60.0
    max_s: float = 900.0
    refusal_defer_s: float = 60.0
    update_timeout_s: float = 600.0


def _event(
    job: Job, event: str, now: datetime, *, from_state: JobState | None, reason: str | None = None
) -> JobEvent:
    return JobEvent(
        campaign_id=job.campaign_id,
        event=event,
        ts=now,
        job_id=job.job_id,
        hub_id=job.hub_id,
        from_state=from_state.value if from_state else None,
        to_state=job.state.value,
        reason=reason,
        attempt=job.attempts,
    )


def fail_job(job: Job, reason: str, now: datetime, policy: RetryPolicy) -> list[JobEvent]:
    """A failed attempt: a transient reason with attempts left schedules a retry (PENDING + backoff,
    RETRY_SCHEDULED); anything else, or the last attempt, is terminal FAILED (counts toward the halt)."""
    before = job.state
    job.command_id = None
    job.reason = reason
    if is_transient(reason) and job.attempts < policy.max_attempts:
        delay = retry_delay_s(max(1, job.attempts), initial_s=policy.initial_s, max_s=policy.max_s)
        job.state = JobState.PENDING
        job.next_attempt_at = now + timedelta(seconds=delay)
        event = _event(job, "RETRY_SCHEDULED", now, from_state=before, reason=reason)
        return [replace(event, detail={"retry_at": job.next_attempt_at.isoformat(), "delay_s": delay})]
    job.state = JobState.FAILED
    job.terminal_failure = True
    job.finished_at = now
    job.next_attempt_at = None
    detail = {"max_attempts_reached": is_transient(reason)}
    event = _event(job, "FAILED", now, from_state=before, reason=reason)
    return [replace(event, detail=detail)]


# ---------------------------------------------------------------------------------------------- job step


def advance_job(
    job: Job,
    command: CommandRecord | None,
    *,
    observed_version: str | None,
    now: datetime,
    policy: RetryPolicy,
) -> list[JobEvent]:
    """Advance one job from its current attempt's command row (guardian verdict + hub status) and the
    hub's reported firmware version (device-info). Returns the events it produced."""
    if job.state in TERMINAL_JOB_STATES:
        return []
    done_state = JobState.ROLLED_BACK if job.action == "ROLLBACK" else JobState.SUCCEEDED
    events: list[JobEvent] = []
    before: JobState

    if job.state == JobState.PENDING:
        if command is None or job.command_id is None:
            return []
        if command.status == "REFUSED":
            reason = command.refuse_reason or "REFUSED"
            before = job.state
            job.command_id = None
            if reason == ALREADY_AT_TARGET:
                job.state, job.reason, job.finished_at = done_state, reason, now
                return [_event(job, done_state.value, now, from_state=before, reason=reason)]
            if reason in TERMINAL_REFUSALS:
                job.attempts = max(job.attempts, command.attempt)
                return fail_job(job, reason, now, RetryPolicy(max_attempts=0))
            job.reason = reason
            job.next_attempt_at = now + timedelta(seconds=policy.refusal_defer_s)
            return [_event(job, "DEFERRED", now, from_state=before, reason=reason)]
        if command.status in ("SIGNED", "PUBLISHED"):
            before = job.state
            job.state, job.sent_at, job.attempts, job.reason = JobState.SENT, now, command.attempt, None
            events.append(_event(job, "SENT", now, from_state=before))
        elif command.expires_at <= now:  # the guardian never decided within the lease: re-request later
            before = job.state
            job.command_id = None
            job.reason = "GUARDIAN_NO_VERDICT"
            job.next_attempt_at = now + timedelta(seconds=policy.refusal_defer_s)
            return [_event(job, "DEFERRED", now, from_state=before, reason=job.reason)]
        else:
            return []

    hub_state = command.hub_state if command is not None else None

    if hub_state in ("FAILED", "REJECTED"):
        if job.state == JobState.SENT and hub_state == "FAILED":
            events.extend(_mark_acked_updating(job, now))
        reason = (command.hub_reason if command else None) or "UNKNOWN"
        return events + fail_job(job, reason, now, policy)

    target_reached = observed_version == job.target_version or (
        hub_state == "DONE" and command is not None and command.hub_version == job.target_version
    )
    if target_reached and job.state in IN_FLIGHT_JOB_STATES:
        if job.state == JobState.SENT:
            events.extend(_mark_acked_updating(job, now))
        before = job.state
        job.state, job.finished_at, job.reason = done_state, now, None
        events.append(_event(job, done_state.value, now, from_state=before))
        return events
    if hub_state == "DONE":  # DONE but running another version: the image did not take
        return events + fail_job(job, "BOOT_FAILED", now, policy)

    if job.state == JobState.SENT:
        if hub_state == "ACCEPTED" or hub_state in _UPDATING_HUB_STATES:
            events.extend(_mark_acked_updating(job, now))
            return events
        if command is not None and command.expires_at <= now:
            return events + fail_job(job, "NO_ACK", now, policy)
        return events

    if (
        job.state == JobState.UPDATING
        and job.updating_at is not None
        and (now - job.updating_at).total_seconds() > policy.update_timeout_s
    ):
        return events + fail_job(job, "UPDATE_TIMEOUT", now, policy)
    return events


def _mark_acked_updating(job: Job, now: datetime) -> list[JobEvent]:
    before = job.state
    job.state, job.updating_at = JobState.UPDATING, now
    acked = _event(job, "ACKED", now, from_state=before)
    updating = _event(job, "UPDATING", now, from_state=JobState.SENT)
    return [acked, updating]


# ---------------------------------------------------------------------------------------------- dispatch


@dataclass(frozen=True, slots=True)
class FleetCounts:
    """Hub counts per bank/feeder (for the caps) and hubs in flight across ALL campaigns."""

    bank_hubs: Mapping[str, int]
    feeder_hubs: Mapping[str, int]
    bank_in_flight: Mapping[str, int] = field(default_factory=dict)
    feeder_in_flight: Mapping[str, int] = field(default_factory=dict)


def in_window(campaign: Campaign, now: datetime) -> bool:
    if campaign.window_start is not None and now < campaign.window_start:
        return False
    return not (campaign.window_end is not None and now >= campaign.window_end)


def current_wave(jobs: Iterable[Job]) -> int | None:
    """The lowest wave still holding a non-terminal UPDATE job (None = every wave finished)."""
    open_waves = [j.wave for j in jobs if j.action == "UPDATE" and j.state not in TERMINAL_JOB_STATES]
    return min(open_waves) if open_waves else None


def _awaiting_dispatch(job: Job, now: datetime) -> bool:
    return (
        job.state == JobState.PENDING
        and job.command_id is None
        and (job.next_attempt_at is None or job.next_attempt_at <= now)
    )


def plan_dispatch(campaign: Campaign, jobs: Sequence[Job], counts: FleetCounts, now: datetime) -> list[Job]:
    """Jobs to request a signed command for now. Operator ROLLBACKs go first and ignore waves/pause (never
    an aborted campaign); UPDATE jobs need a RUNNING campaign inside its maintenance window, come only from
    the current wave, and never exceed a bank's or feeder's concurrency cap (counting hubs in flight in
    every campaign). The guardian re-checks every one of these (G-36); this only avoids asking in vain."""
    if campaign.state == CampaignState.ABORTED:
        return []
    bank_used: Counter[str] = Counter(counts.bank_in_flight)
    feeder_used: Counter[str] = Counter(counts.feeder_in_flight)
    chosen: list[Job] = []

    def fits(job: Job) -> bool:
        bank_cap = concurrency_cap(counts.bank_hubs.get(job.bank_id, 1), campaign.bank_max_concurrent_pct)
        if bank_used[job.bank_id] + 1 > bank_cap:
            return False
        if job.feeder_id is not None:
            feeder_cap = concurrency_cap(
                counts.feeder_hubs.get(job.feeder_id, 1), campaign.feeder_max_concurrent_pct
            )
            if feeder_used[job.feeder_id] + 1 > feeder_cap:
                return False
        return True

    def take(job: Job) -> None:
        chosen.append(job)
        bank_used[job.bank_id] += 1
        if job.feeder_id is not None:
            feeder_used[job.feeder_id] += 1

    for job in sorted(jobs, key=lambda j: j.hub_id):
        if job.action == "ROLLBACK" and _awaiting_dispatch(job, now) and fits(job):
            take(job)

    if campaign.state != CampaignState.RUNNING or not in_window(campaign, now):
        return chosen
    wave = current_wave(jobs)
    if wave is None:
        return chosen
    for job in sorted(jobs, key=lambda j: j.hub_id):
        if job.action == "UPDATE" and job.wave == wave and _awaiting_dispatch(job, now) and fits(job):
            take(job)
    return chosen


# ---------------------------------------------------------------------------------------------- campaign


def job_counts(jobs: Iterable[Job]) -> dict[str, int]:
    counts = Counter(j.state.value for j in jobs)
    return {state.value: counts.get(state.value, 0) for state in JobState}


def halt_reason(campaign: Campaign, jobs: Sequence[Job]) -> str | None:
    """Why a RUNNING campaign must halt now, or None. Only TERMINAL failures count (a retried transient
    failure does not): `max_failures` of them, or `max_failure_pct`% of the campaign's hubs. A canary wave
    that finished without a single success halts too."""
    total = len(jobs)
    failures = sum(1 for j in jobs if j.terminal_failure and j.state == JobState.FAILED)
    if failures >= campaign.max_failures or (total and failures * 100.0 >= campaign.max_failure_pct * total):
        return HALT_FAILURE_THRESHOLD
    canary = [j for j in jobs if j.wave == 0 and j.action == "UPDATE"]
    canary_done = bool(canary) and all(j.state in TERMINAL_JOB_STATES for j in canary)
    if (
        canary_done
        and not any(j.state == JobState.SUCCEEDED for j in canary)
        and any(j.state == JobState.FAILED for j in canary)
    ):
        return HALT_CANARY_FAILED
    return None


def is_complete(jobs: Sequence[Job]) -> bool:
    return all(j.state in TERMINAL_JOB_STATES for j in jobs)


def campaign_event(
    campaign_id: UUID,
    event: str,
    now: datetime,
    *,
    from_state: str | None,
    to_state: str | None,
    reason: str | None = None,
    detail: dict[str, object] | None = None,
) -> JobEvent:
    return JobEvent(
        campaign_id=campaign_id,
        event=event,
        ts=now,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        detail=dict(detail) if detail else None,
    )


def reset_failed_for_retry(jobs: Iterable[Job], now: datetime) -> list[JobEvent]:
    """The operator's "retry failed hubs": every FAILED job back to PENDING with a fresh attempt budget
    (each attempt is still re-checked and re-signed by the guardian)."""
    events: list[JobEvent] = []
    for job in jobs:
        if job.state != JobState.FAILED:
            continue
        before = job.state
        job.state, job.attempts, job.terminal_failure = JobState.PENDING, 0, False
        job.next_attempt_at, job.command_id, job.finished_at = None, None, None
        job.reason = "MANUAL_RETRY"
        events.append(_event(job, "RETRY_SCHEDULED", now, from_state=before, reason="MANUAL_RETRY"))
    return events


def request_rollback(job: Job, now: datetime) -> list[JobEvent]:
    """An operator's per-hub rollback: the job becomes a ROLLBACK to the version the hub ran before the
    campaign. Refused (no events) when that version is unknown or the job is still in flight."""
    if job.from_version is None or job.state in IN_FLIGHT_JOB_STATES or job.action == "ROLLBACK":
        return []
    if job.state not in (JobState.SUCCEEDED, JobState.FAILED):
        return []
    before = job.state
    job.action, job.target_version = "ROLLBACK", job.from_version
    job.state, job.attempts, job.terminal_failure = JobState.PENDING, 0, False
    job.next_attempt_at, job.command_id, job.finished_at = None, None, None
    job.reason = "OPERATOR_ROLLBACK"
    return [_event(job, "ROLLBACK_REQUESTED", now, from_state=before, reason="OPERATOR_ROLLBACK")]
