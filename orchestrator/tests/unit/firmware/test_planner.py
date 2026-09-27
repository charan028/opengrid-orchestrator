"""Pure planning: waves, concurrency caps, maintenance window, the job state machine, retries with
exponential backoff (transient vs terminal), halt on terminal failures, and the catalogue."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid.firmware.catalogue import Catalogue, hub_target_problem
from opengrid.firmware.model import (
    Campaign,
    CampaignState,
    CommandRecord,
    Job,
    JobState,
    WaveSpec,
    is_downgrade,
)
from opengrid.firmware.planner import (
    HALT_CANARY_FAILED,
    HALT_FAILURE_THRESHOLD,
    FleetCounts,
    RetryPolicy,
    advance_job,
    assign_waves,
    build_waves,
    halt_reason,
    order_for_waves,
    plan_dispatch,
    request_rollback,
    reset_failed_for_retry,
    retry_delay_s,
    second_operator_reasons,
)

from .fakes import CATALOGUE_CONFIG, SHA_150

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
POLICY = RetryPolicy(max_attempts=3, initial_s=60, max_s=900, refusal_defer_s=60, update_timeout_s=600)


def _campaign(**changes) -> Campaign:
    base = Campaign(
        campaign_id=uuid4(),
        name="c",
        target_version="1.5.0",
        state=CampaignState.RUNNING,
        hub_ids=[],
        waves=[],
        reason="r",
        proposed_by="op-a",
        created_at=NOW,
    )
    for k, v in changes.items():
        setattr(base, k, v)
    return base


def _jobs(campaign: Campaign, spec: list[tuple[str, str, int]]) -> list[Job]:
    return [
        Job(
            uuid4(),
            campaign.campaign_id,
            hub,
            bank,
            wave,
            "1.5.0",
            feeder_id=f"f-{bank}",
            from_version="1.4.2",
        )
        for hub, bank, wave in spec
    ]


# ------------------------------------------------------------------------------------------------ waves


def test_build_waves_canary_then_percentage_steps():
    assert build_waves(100, WaveSpec(canary=2, size_pct=25)) == [2, 25, 25, 25, 23]
    assert build_waves(10, WaveSpec(canary=1, sizes=(3,), size_pct=None)) == [1, 3, 3, 3]
    assert build_waves(10, WaveSpec(canary_pct=10, sizes=(2, 7))) == [1, 2, 7]
    assert build_waves(1, WaveSpec(canary=5)) == [1]
    assert sum(build_waves(1234, WaveSpec())) == 1234


def test_waves_interleave_banks():
    hubs = [(f"a{i}", "bank-a") for i in range(3)] + [(f"b{i}", "bank-b") for i in range(3)]
    ordered = order_for_waves(hubs)
    assert ordered[:2] == ["a0", "b0"]
    assignment = assign_waves(ordered, [2, 4])
    assert {h for h, w in assignment.items() if w == 0} == {"a0", "b0"}


def test_second_operator_reasons():
    assert (
        second_operator_reasons(
            hub_count=50, threshold=50, committed_hub_ids=[], override_committed=False, downgrade=False
        )
        == []
    )
    assert second_operator_reasons(
        hub_count=51, threshold=50, committed_hub_ids=[], override_committed=False, downgrade=False
    ) == ["MORE_THAN_THRESHOLD_HUBS"]
    assert "HUB_SERVES_COMMITTED_OBLIGATION" in second_operator_reasons(
        hub_count=3, threshold=50, committed_hub_ids=["h"], override_committed=True, downgrade=False
    )
    assert "DOWNGRADE" in second_operator_reasons(
        hub_count=3, threshold=50, committed_hub_ids=[], override_committed=False, downgrade=True
    )


# ------------------------------------------------------------------------------------------------ dispatch


def test_dispatch_only_current_wave_and_bank_cap():
    campaign = _campaign(bank_max_concurrent_pct=10.0)
    jobs = _jobs(campaign, [(f"h{i:02d}", "b1", 0 if i < 5 else 1) for i in range(20)])
    counts = FleetCounts(bank_hubs={"b1": 20}, feeder_hubs={"f-b1": 20})
    chosen = plan_dispatch(campaign, jobs, counts, NOW)
    assert [j.hub_id for j in chosen] == ["h00", "h01"]  # 10% of 20 = 2, wave 0 only


def test_dispatch_counts_hubs_in_flight_in_other_campaigns():
    campaign = _campaign()
    jobs = _jobs(campaign, [("h1", "b1", 0), ("h2", "b1", 0)])
    counts = FleetCounts(bank_hubs={"b1": 10}, feeder_hubs={"f-b1": 10}, bank_in_flight={"b1": 1})
    assert plan_dispatch(campaign, jobs, counts, NOW) == []


def test_feeder_cap_applies_across_banks():
    campaign = _campaign(feeder_max_concurrent_pct=10.0)
    jobs = [Job(uuid4(), campaign.campaign_id, f"h{i}", f"b{i}", 0, "1.5.0", feeder_id="F") for i in range(4)]
    counts = FleetCounts(bank_hubs={f"b{i}": 10 for i in range(4)}, feeder_hubs={"F": 20})
    assert len(plan_dispatch(campaign, jobs, counts, NOW)) == 2  # 10% of 20


def test_next_wave_waits_for_the_previous_one():
    campaign = _campaign(bank_max_concurrent_pct=100.0)
    jobs = _jobs(campaign, [("h1", "b1", 0), ("h2", "b1", 1)])
    jobs[0].state = JobState.UPDATING
    counts = FleetCounts(bank_hubs={"b1": 2}, feeder_hubs={})
    assert plan_dispatch(campaign, jobs, counts, NOW) == []
    jobs[0].state = JobState.SUCCEEDED
    assert [j.hub_id for j in plan_dispatch(campaign, jobs, counts, NOW)] == ["h2"]


@pytest.mark.parametrize(
    ("state", "window"),
    [
        (CampaignState.PAUSED, (None, None)),
        (CampaignState.HALTED, (None, None)),
        (CampaignState.RUNNING, (NOW + timedelta(hours=1), None)),
        (CampaignState.RUNNING, (None, NOW)),
    ],
)
def test_no_update_dispatch_when_paused_halted_or_outside_window(state, window):
    campaign = _campaign(state=state, window_start=window[0], window_end=window[1])
    jobs = _jobs(campaign, [("h1", "b1", 0)])
    assert plan_dispatch(campaign, jobs, FleetCounts({"b1": 10}, {}), NOW) == []


def test_rollback_dispatches_even_in_a_halted_campaign():
    campaign = _campaign(state=CampaignState.HALTED)
    jobs = _jobs(campaign, [("h1", "b1", 0)])
    jobs[0].state = JobState.SUCCEEDED
    assert request_rollback(jobs[0], NOW)
    assert [j.action for j in plan_dispatch(campaign, jobs, FleetCounts({"b1": 10}, {}), NOW)] == ["ROLLBACK"]


def test_backoff_is_respected():
    campaign = _campaign()
    jobs = _jobs(campaign, [("h1", "b1", 0)])
    jobs[0].next_attempt_at = NOW + timedelta(seconds=30)
    assert plan_dispatch(campaign, jobs, FleetCounts({"b1": 10}, {}), NOW) == []
    assert plan_dispatch(campaign, jobs, FleetCounts({"b1": 10}, {}), NOW + timedelta(seconds=31))


# ------------------------------------------------------------------------------------------------ job machine


def _requested(job: Job, **fields) -> CommandRecord:
    job.command_id = uuid4()
    return CommandRecord(
        command_id=job.command_id,
        job_id=job.job_id,
        hub_id=job.hub_id,
        attempt=job.attempts + 1,
        status=fields.pop("status", "REQUESTED"),
        expires_at=fields.pop("expires_at", NOW + timedelta(seconds=120)),
        **fields,
    )


def _job() -> Job:
    return Job(uuid4(), uuid4(), "h1", "b1", 0, "1.5.0", from_version="1.4.2")


def test_happy_path_sent_acked_updating_succeeded_with_timestamps():
    job = _job()
    cmd = _requested(job, status="PUBLISHED")
    events = advance_job(job, cmd, observed_version="1.4.2", now=NOW, policy=POLICY)
    assert [e.event for e in events] == ["SENT"] and job.state == JobState.SENT and job.attempts == 1
    cmd = replace(cmd, hub_state="DOWNLOADING")
    events = advance_job(job, cmd, observed_version="1.4.2", now=NOW + timedelta(seconds=5), policy=POLICY)
    assert [e.event for e in events] == ["ACKED", "UPDATING"]
    events = advance_job(job, cmd, observed_version="1.5.0", now=NOW + timedelta(seconds=60), policy=POLICY)
    assert [e.event for e in events] == ["SUCCEEDED"]
    assert job.state == JobState.SUCCEEDED
    assert all(e.ts >= NOW for e in events) and events[0].from_state == "UPDATING"


def test_no_ack_before_lease_expiry_retries_with_backoff():
    job = _job()
    cmd = _requested(job, status="PUBLISHED", expires_at=NOW + timedelta(seconds=10))
    advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)
    events = advance_job(job, cmd, observed_version=None, now=NOW + timedelta(seconds=11), policy=POLICY)
    assert [e.event for e in events] == ["RETRY_SCHEDULED"]
    assert events[0].reason == "NO_ACK"
    assert job.state == JobState.PENDING and job.command_id is None
    assert job.next_attempt_at == NOW + timedelta(seconds=11 + 60)


@pytest.mark.parametrize("reason", ["DOWNLOAD_ERROR", "VERIFY_ERROR"])
def test_transient_hub_failures_retry_until_max_attempts(reason):
    job = _job()
    delays = []
    for attempt in range(1, 4):
        cmd = _requested(job, status="PUBLISHED")
        advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)
        assert job.attempts == attempt
        events = advance_job(
            job,
            replace(cmd, hub_state="FAILED", hub_reason=reason),
            observed_version=None,
            now=NOW,
            policy=POLICY,
        )
        if attempt < 3:
            assert events[-1].event == "RETRY_SCHEDULED"
            delays.append((job.next_attempt_at - NOW).total_seconds())
        else:
            assert events[-1].event == "FAILED"
            assert job.state == JobState.FAILED and job.terminal_failure
            assert events[-1].detail == {"max_attempts_reached": True}
    assert delays == [60.0, 120.0]


@pytest.mark.parametrize("reason", ["HASH_MISMATCH", "HARDWARE_INCOMPATIBLE", "INSTALL_ERROR"])
def test_terminal_hub_failures_never_retry(reason):
    job = _job()
    cmd = _requested(job, status="PUBLISHED")
    advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)
    events = advance_job(
        job,
        replace(cmd, hub_state="FAILED", hub_reason=reason),
        observed_version=None,
        now=NOW,
        policy=POLICY,
    )
    assert events[-1].event == "FAILED" and job.state == JobState.FAILED and job.terminal_failure
    assert job.attempts == 1


def test_update_timeout_is_transient():
    job = _job()
    cmd = _requested(job, status="PUBLISHED")
    advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)
    advance_job(job, replace(cmd, hub_state="INSTALLING"), observed_version=None, now=NOW, policy=POLICY)
    events = advance_job(
        job,
        replace(cmd, hub_state="INSTALLING"),
        observed_version=None,
        now=NOW + timedelta(seconds=601),
        policy=POLICY,
    )
    assert events[-1].event == "RETRY_SCHEDULED" and events[-1].reason == "UPDATE_TIMEOUT"


def test_guardian_refusal_defers_without_consuming_an_attempt():
    job = _job()
    cmd = _requested(job, status="REFUSED", refuse_reason="FIRMWARE_SOC_BELOW_RESERVE_MARGIN")
    events = advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)
    assert [e.event for e in events] == ["DEFERRED"]
    assert job.state == JobState.PENDING and job.attempts == 0
    assert job.next_attempt_at == NOW + timedelta(seconds=60)


def test_guardian_catalogue_refusal_is_terminal_and_already_at_target_succeeds():
    job = _job()
    cmd = _requested(job, status="REFUSED", refuse_reason="FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE")
    assert advance_job(job, cmd, observed_version=None, now=NOW, policy=POLICY)[-1].event == "FAILED"
    job2 = _job()
    cmd2 = _requested(job2, status="REFUSED", refuse_reason="FIRMWARE_ALREADY_AT_TARGET")
    assert advance_job(job2, cmd2, observed_version=None, now=NOW, policy=POLICY)[-1].event == "SUCCEEDED"


def test_retry_delay_caps():
    assert [retry_delay_s(a, initial_s=60, max_s=900) for a in range(1, 7)] == [60, 120, 240, 480, 900, 900]


# ------------------------------------------------------------------------------------------------ halt


def test_halt_counts_only_terminal_failures():
    campaign = _campaign(max_failures=2, max_failure_pct=50.0)
    jobs = _jobs(campaign, [(f"h{i}", "b1", 1) for i in range(10)])
    jobs[0].state, jobs[0].terminal_failure = JobState.FAILED, True
    jobs[1].state, jobs[1].reason = JobState.PENDING, "NO_ACK"  # a retried transient failure
    assert halt_reason(campaign, jobs) is None
    jobs[2].state, jobs[2].terminal_failure = JobState.FAILED, True
    assert halt_reason(campaign, jobs) == HALT_FAILURE_THRESHOLD


def test_halt_on_failure_percentage():
    campaign = _campaign(max_failures=100, max_failure_pct=5.0)
    jobs = _jobs(campaign, [(f"h{i}", "b1", 1) for i in range(40)])
    jobs[0].state, jobs[0].terminal_failure = JobState.FAILED, True
    assert halt_reason(campaign, jobs) is None  # 2.5%
    jobs[1].state, jobs[1].terminal_failure = JobState.FAILED, True
    assert halt_reason(campaign, jobs) == HALT_FAILURE_THRESHOLD  # 5%


def test_canary_without_success_halts():
    campaign = _campaign(max_failures=10, max_failure_pct=100.0)
    jobs = _jobs(campaign, [("h1", "b1", 0), ("h2", "b1", 1)])
    jobs[0].state, jobs[0].terminal_failure = JobState.FAILED, True
    assert halt_reason(campaign, jobs) == HALT_CANARY_FAILED


def test_manual_retry_resets_failed_jobs():
    campaign = _campaign()
    jobs = _jobs(campaign, [("h1", "b1", 0), ("h2", "b1", 0)])
    jobs[0].state, jobs[0].terminal_failure, jobs[0].attempts = JobState.FAILED, True, 3
    events = reset_failed_for_retry(jobs, NOW)
    assert [e.event for e in events] == ["RETRY_SCHEDULED"]
    assert jobs[0].state == JobState.PENDING and jobs[0].attempts == 0 and not jobs[0].terminal_failure


# ------------------------------------------------------------------------------------------------ catalogue


def test_catalogue_drops_invalid_entries_and_table_wins():
    catalogue = Catalogue.build(
        [
            *CATALOGUE_CONFIG,
            {"version": "latest", "hardware_revision": "revB", "sha256": SHA_150, "release_note": "x"},
        ],
        [{"version": "1.5.0", "hardware_revision": "revB", "sha256": "3" * 64, "release_note": "reissued"}],
    )
    assert catalogue.versions() == ["1.4.2", "1.5.0"]
    entry = catalogue.lookup("1.5.0", "revB")
    assert entry is not None and entry.sha256 == "3" * 64 and entry.source == "table"
    assert (
        hub_target_problem(
            catalogue,
            target_version="1.5.0",
            hardware_revision=None,
            current_version=None,
            allow_downgrade=False,
        )
        == "FIRMWARE_HARDWARE_REVISION_UNKNOWN"
    )


def test_version_ordering():
    assert is_downgrade("1.10.0", "1.9.9")
    assert not is_downgrade("1.9.9", "1.10.0")
    assert is_downgrade("1.5.0", "1.5.0-rc1")
    assert not is_downgrade(None, "1.0.0")
