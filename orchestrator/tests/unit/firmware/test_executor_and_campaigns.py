"""Campaign lifecycle (two-person rule, TTL, pause/abort, retry failed, rollback) and the engine executor
end to end over the in-memory repo: waves under the bank caps, the allocator exclusion, retries, halt with
its critical alert, completion with its info alert, and the timestamped event feed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from opengrid.firmware import campaigns as svc
from opengrid.firmware.catalogue import Catalogue
from opengrid.firmware.config import FirmwareConfig
from opengrid.firmware.executor import (
    ALR_CAMPAIGN_COMPLETED,
    ALR_CAMPAIGN_HALTED,
    ALR_HUB_FAILED,
    FirmwareExecutor,
)
from opengrid.firmware.model import CampaignState, JobState, WaveSpec
from opengrid.firmware.repo import HubSelection

from .fakes import CATALOGUE_CONFIG, FakeFirmwareRepo, fleet

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
CFG = FirmwareConfig(catalogue=CATALOGUE_CONFIG, retry_initial_s=60, max_attempts=3)
CATALOGUE = Catalogue.build(CATALOGUE_CONFIG)


def _request(**changes: Any) -> svc.CampaignRequest:
    base: dict[str, Any] = {
        "name": "R3.1 rollout",
        "target_version": "1.5.0",
        "selection": HubSelection(bank_ids=("bank-00", "bank-01")),
        "reason": "ramp fix",
        "waves": WaveSpec(canary=2, size_pct=50),
    }
    base.update(changes)
    return svc.CampaignRequest(**base)


async def _create(repo: FakeFirmwareRepo, **changes: Any) -> svc.CreatedCampaign:
    return await svc.create_campaign(
        repo, _request(**changes), proposer="op-a", catalogue=CATALOGUE, cfg=CFG, now=NOW
    )


# ------------------------------------------------------------------------------------------------ lifecycle


async def test_unknown_version_is_refused():
    with pytest.raises(svc.CampaignError) as err:
        await _create(FakeFirmwareRepo(), target_version="2.0.0")
    assert err.value.status == 422


async def test_small_campaign_confirms_to_approved_by_the_proposer():
    repo = FakeFirmwareRepo()
    created = await _create(repo)
    assert created.campaign.state == CampaignState.DRAFT and not created.campaign.requires_second_operator
    assert created.campaign.waves == [2, 10, 8]
    campaign = await svc.confirm_campaign(
        repo, created.campaign.campaign_id, operator="op-a", now=NOW, ttl_s=60
    )
    assert campaign.state == CampaignState.APPROVED and campaign.approved_by == "op-a"


async def test_large_campaign_needs_a_second_operator():
    repo = FakeFirmwareRepo(fleet(banks=6, per_bank=10))
    created = await _create(repo, selection=HubSelection(zones=("NORTH",)))
    assert created.campaign.requires_second_operator
    assert created.campaign.second_operator_reasons == ["MORE_THAN_THRESHOLD_HUBS"]
    cid = created.campaign.campaign_id
    campaign = await svc.confirm_campaign(repo, cid, operator="op-a", now=NOW, ttl_s=60)
    assert campaign.state == CampaignState.PROPOSED
    with pytest.raises(svc.CampaignError) as err:
        await svc.approve_campaign(repo, cid, operator="OP-A", now=NOW)
    assert err.value.status == 403
    campaign = await svc.approve_campaign(repo, cid, operator="op-b", now=NOW)
    assert campaign.state == CampaignState.APPROVED and campaign.approved_by == "op-b"


async def test_committed_override_needs_a_second_operator_and_the_confirmer_cannot_approve():
    repo = FakeFirmwareRepo(committed={"hub-00000"})
    created = await _create(repo, override_committed=True)
    assert "HUB_SERVES_COMMITTED_OBLIGATION" in created.campaign.second_operator_reasons
    assert created.committed_hub_ids == ["hub-00000"]
    cid = created.campaign.campaign_id
    await svc.confirm_campaign(repo, cid, operator="op-c", now=NOW, ttl_s=60)
    with pytest.raises(svc.CampaignError):
        await svc.approve_campaign(repo, cid, operator="op-c", now=NOW)


async def test_expired_draft_cannot_be_confirmed():
    repo = FakeFirmwareRepo()
    created = await _create(repo)
    with pytest.raises(svc.CampaignError) as err:
        await svc.confirm_campaign(
            repo, created.campaign.campaign_id, operator="op-a", now=NOW + timedelta(seconds=61), ttl_s=60
        )
    assert err.value.status == 409
    assert (await repo.get_campaign(created.campaign.campaign_id)).state == CampaignState.ABORTED  # type: ignore[union-attr]


async def test_hubs_that_cannot_take_the_image_are_skipped():
    hubs = fleet(banks=1, per_bank=4)
    hubs[0] = replace(hubs[0], hardware_revision="revA")
    hubs[1] = replace(hubs[1], firmware_version="1.5.0")
    repo = FakeFirmwareRepo(hubs)
    created = await _create(repo, selection=HubSelection(bank_ids=("bank-00",)))
    assert created.skipped == {
        "hub-00000": "FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE",
        "hub-00001": "FIRMWARE_ALREADY_AT_TARGET",
    }


# ------------------------------------------------------------------------------------------------ executor


class Alerts:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, rule: str, severity: str, summary: str, detail: dict[str, Any]) -> None:
        self.raised.append((rule, severity, detail))


async def _running(repo: FakeFirmwareRepo, **changes: Any) -> tuple[FirmwareExecutor, Alerts, Any]:
    created = await _create(repo, **changes)
    await svc.confirm_campaign(repo, created.campaign.campaign_id, operator="op-a", now=NOW, ttl_s=60)
    alerts = Alerts()

    async def catalogue() -> Catalogue:
        return CATALOGUE

    return FirmwareExecutor(repo, CFG, catalogue, alerts=alerts), alerts, created.campaign.campaign_id


def _sign_all(repo: FakeFirmwareRepo) -> list[Any]:
    """What og-guardian does for every REQUESTED row that passes G-36."""
    signed = []
    for cid, cmd in list(repo.command_rows.items()):
        if cmd.status == "REQUESTED":
            repo.set_command(cid, status="PUBLISHED")
            signed.append(cid)
    return signed


async def test_executor_runs_waves_under_bank_caps_and_completes():
    repo = FakeFirmwareRepo()
    executor, alerts, cid = await _running(repo)
    t = NOW
    result = await executor.step(t)
    campaign = await repo.get_campaign(cid)
    assert campaign is not None and campaign.state == CampaignState.RUNNING
    assert result.requested == 2  # canary: 2 hubs, one per bank (bank cap 10% of 10 = 1)
    assert executor.updating_hub_ids() == {r.job.hub_id for r in repo.command_requests.values()}
    for _ in range(40):
        t += timedelta(seconds=10)
        for command_id in _sign_all(repo):
            hub = repo.command_rows[command_id].hub_id
            repo.set_command(command_id, hub_state="DONE", hub_version="1.5.0")
            repo.set_version(hub, "1.5.0")
        await executor.step(t)
        in_flight = [j for j in repo.job_rows.values() if j.state in (JobState.SENT, JobState.UPDATING)]
        per_bank: dict[str, int] = {}
        for j in in_flight:
            per_bank[j.bank_id] = per_bank.get(j.bank_id, 0) + 1
        assert all(n <= 1 for n in per_bank.values())
    campaign = await repo.get_campaign(cid)
    assert campaign is not None and campaign.state == CampaignState.COMPLETED
    assert all(j.state == JobState.SUCCEEDED for j in repo.job_rows.values())
    assert (ALR_CAMPAIGN_COMPLETED, "info") in [(r, s) for r, s, _ in alerts.raised]
    events = [e.event for _, e in await repo.events(cid)]
    for expected in (
        "CAMPAIGN_CREATED",
        "CAMPAIGN_APPROVED",
        "CAMPAIGN_STARTED",
        "REQUESTED",
        "SENT",
        "ACKED",
        "UPDATING",
        "SUCCEEDED",
        "CAMPAIGN_COMPLETED",
    ):
        assert expected in events
    assert executor.updating_hub_ids() == frozenset()


async def test_executor_halts_on_terminal_failures_and_alerts():
    repo = FakeFirmwareRepo()
    executor, alerts, cid = await _running(repo, waves=WaveSpec(canary=2, size_pct=50), max_failures=2)
    await executor.step(NOW)
    for command_id in _sign_all(repo):
        repo.set_command(command_id, hub_state="FAILED", hub_reason="INSTALL_ERROR")
    await executor.step(NOW + timedelta(seconds=5))
    campaign = await repo.get_campaign(cid)
    assert campaign is not None and campaign.state == CampaignState.HALTED
    assert campaign.halt_reason == "FAILURE_THRESHOLD_REACHED"
    rules = [(r, s) for r, s, _ in alerts.raised]
    assert rules.count((ALR_HUB_FAILED, "warning")) == 2
    assert (ALR_CAMPAIGN_HALTED, "critical") in rules
    # Halted: nothing new is requested.
    before = len(repo.command_requests)
    await executor.step(NOW + timedelta(seconds=30))
    assert len(repo.command_requests) == before


async def test_transient_failure_retries_with_fresh_command_and_does_not_halt():
    repo = FakeFirmwareRepo(fleet(banks=1, per_bank=10))
    executor, alerts, cid = await _running(
        repo, selection=HubSelection(bank_ids=("bank-00",)), waves=WaveSpec(canary=1, size_pct=100)
    )
    await executor.step(NOW)
    [first] = _sign_all(repo)
    repo.set_command(first, hub_state="FAILED", hub_reason="DOWNLOAD_ERROR")
    await executor.step(NOW + timedelta(seconds=5))
    job = repo.job_for(repo.command_rows[first].hub_id)
    assert job.state == JobState.PENDING and job.attempts == 1
    assert job.next_attempt_at == NOW + timedelta(seconds=65)
    campaign = await repo.get_campaign(cid)
    assert campaign is not None and campaign.state == CampaignState.RUNNING
    await executor.step(NOW + timedelta(seconds=30))
    assert repo.job_for(job.hub_id).command_id is None  # still backing off
    await executor.step(NOW + timedelta(seconds=66))
    retried = repo.job_for(job.hub_id)
    assert retried.command_id is not None and retried.command_id != first
    assert repo.command_rows[retried.command_id].attempt == 2
    assert not [a for a in alerts.raised if a[0] == ALR_CAMPAIGN_HALTED]


async def test_pause_abort_retry_and_rollback():
    repo = FakeFirmwareRepo(fleet(banks=1, per_bank=10))
    executor, _alerts, cid = await _running(
        repo,
        selection=HubSelection(bank_ids=("bank-00",)),
        waves=WaveSpec(canary=1, size_pct=100),
        max_failures=1,
    )
    await executor.step(NOW)
    [first] = _sign_all(repo)
    hub = repo.command_rows[first].hub_id
    repo.set_command(first, hub_state="FAILED", hub_reason="HASH_MISMATCH")
    await executor.step(NOW + timedelta(seconds=5))
    assert (await repo.get_campaign(cid)).state == CampaignState.HALTED  # type: ignore[union-attr]

    campaign, count = await svc.retry_failed_hubs(repo, cid, operator="op-a", now=NOW + timedelta(seconds=10))
    assert count == 1 and campaign.state == CampaignState.PAUSED
    assert repo.job_for(hub).state == JobState.PENDING and repo.job_for(hub).attempts == 0
    campaign = await svc.resume_campaign(repo, cid, operator="op-a", now=NOW + timedelta(seconds=11))
    assert campaign.state == CampaignState.RUNNING

    await executor.step(NOW + timedelta(seconds=12))
    for command_id in _sign_all(repo):
        repo.set_command(command_id, hub_state="DONE", hub_version="1.5.0")
        repo.set_version(repo.command_rows[command_id].hub_id, "1.5.0")
    await executor.step(NOW + timedelta(seconds=13))
    assert repo.job_for(hub).state == JobState.SUCCEEDED

    await svc.pause_campaign(repo, cid, operator="op-a", now=NOW + timedelta(seconds=14))
    job = await svc.rollback_hub(repo, cid, hub, operator="op-a", now=NOW + timedelta(seconds=15))
    assert job.action == "ROLLBACK" and job.target_version == "1.4.2"
    await executor.step(NOW + timedelta(seconds=16))  # paused, but a rollback is still requested
    rollback_cmd = repo.job_for(hub).command_id
    assert rollback_cmd is not None and repo.command_requests[rollback_cmd].job.action == "ROLLBACK"
    _sign_all(repo)
    repo.set_command(rollback_cmd, hub_state="DONE", hub_version="1.4.2")
    repo.set_version(hub, "1.4.2")
    await executor.step(NOW + timedelta(seconds=17))
    assert repo.job_for(hub).state == JobState.ROLLED_BACK

    campaign = await svc.abort_campaign(repo, cid, operator="op-a", now=NOW + timedelta(seconds=18))
    assert campaign.state == CampaignState.ABORTED
    assert all(j.state != JobState.PENDING for j in repo.job_rows.values())
    with pytest.raises(svc.CampaignError):
        await svc.resume_campaign(repo, cid, operator="op-a", now=NOW + timedelta(seconds=19))
