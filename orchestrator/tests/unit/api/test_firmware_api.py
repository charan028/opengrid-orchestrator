"""`/og/api/firmware/*` (R3.1): operator-only mutations, viewer read-only, the two-step propose/confirm,
the second-operator approval, the two-step "retry failed hubs" and per-hub rollback, and the events feed.

The UI fixtures under `fixtures/firmware/` are kept in sync with the live responses here: with
`OG_UPDATE_FIXTURES=1` this test rewrites them; otherwise it asserts their shape (keys) still matches.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.deps import get_config, get_proposals, get_trace_store
from opengrid.api.proposals import ProposalStore
from opengrid.api.routers import firmware as firmware_router
from opengrid.firmware.catalogue import Catalogue
from opengrid.firmware.config import load_firmware_config
from opengrid.firmware.executor import FirmwareExecutor
from opengrid.firmware.model import CampaignState, JobState
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

from ..firmware.fakes import CATALOGUE_CONFIG, FakeFirmwareRepo, fleet
from .conftest import PROXY_HEADERS

FIXTURES = Path(__file__).parent / "fixtures" / "firmware"
OP_A = {"X-Remote-User": "op-a"}
OP_B = {"X-Remote-User": "op-b"}
VIEWER = {"X-Remote-User": "viewer"}


@pytest.fixture
def fake_config() -> Config:
    return Config(
        {
            "api": {"sse_heartbeat_s": 15, "roles": {"operator": ["op-a", "op-b"], "viewer": []}},
            "firmware": {
                "catalogue": [dict(e) for e in CATALOGUE_CONFIG],
                "second_operator_hub_threshold": 50,
            },
        }
    )


@pytest.fixture
def repo() -> FakeFirmwareRepo:
    return FakeFirmwareRepo(fleet(banks=6, per_bank=10))


@pytest.fixture
def fw(repo: FakeFirmwareRepo, fake_config: Config, fake_trace_store: TraceStore) -> TestClient:
    """A bare app with only this router (independent of which other routers app.py mounts today)."""
    app = FastAPI()
    app.include_router(firmware_router.router)
    proposals = ProposalStore()
    app.dependency_overrides[firmware_router.get_firmware_repo] = lambda: repo
    app.dependency_overrides[get_config] = lambda: fake_config
    app.dependency_overrides[get_trace_store] = lambda: fake_trace_store
    app.dependency_overrides[get_proposals] = lambda: proposals
    return TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)


def _fixture(name: str, body: dict[str, Any]) -> None:
    path = FIXTURES / f"{name}.json"
    if os.environ.get("OG_UPDATE_FIXTURES"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert _shape(stored) == _shape(body), f"fixtures/firmware/{name}.json is stale (OG_UPDATE_FIXTURES=1)"


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items()) if k not in ("detail", "selection", "skipped")}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return "scalar"


def _propose(fw: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {
        "name": "R3.1 ramp fix",
        "target_version": "1.5.0",
        "selection": {"bank_ids": ["bank-00", "bank-01"]},
        "reason": "ramp fix",
        "waves": {"canary": 2, "size_pct": 50},
        **body,
    }
    response = fw.post("/og/api/firmware/campaigns", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    result: dict[str, Any] = response.json()
    return result


def test_catalogue_is_readable_by_viewer(fw):
    response = fw.get("/og/api/firmware/catalogue", headers=VIEWER)
    assert response.status_code == 200
    body = response.json()
    assert body["versions"] == ["1.4.2", "1.5.0"]
    _fixture("catalogue", body)


def test_viewer_cannot_mutate(fw):
    response = fw.post(
        "/og/api/firmware/campaigns",
        json={"name": "x", "target_version": "1.5.0", "selection": {"hub_ids": ["hub-00000"]}, "reason": "r"},
        headers=VIEWER,
    )
    assert response.status_code == 403


def test_unknown_version_and_empty_selection_are_422(fw):
    bad = fw.post(
        "/og/api/firmware/campaigns",
        json={"name": "x", "target_version": "7.0.0", "selection": {"hub_ids": ["hub-00000"]}, "reason": "r"},
        headers=OP_A,
    )
    assert bad.status_code == 422
    empty = fw.post(
        "/og/api/firmware/campaigns",
        json={"name": "x", "target_version": "1.5.0", "selection": {}, "reason": "r"},
        headers=OP_A,
    )
    assert empty.status_code == 422


def test_small_campaign_propose_confirm_and_detail(fw, repo):
    proposed = _propose(fw, OP_A)
    _fixture("campaign_proposed", proposed)
    campaign_id = proposed["campaign"]["campaign_id"]
    assert proposed["campaign"]["state"] == "DRAFT" and not proposed["campaign"]["requires_second_operator"]
    confirmed = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/confirm", headers=OP_A)
    assert confirmed.status_code == 200 and confirmed.json()["campaign"]["state"] == "APPROVED"
    detail = fw.get(f"/og/api/firmware/campaigns/{campaign_id}", headers=VIEWER).json()
    assert detail["counts"]["PENDING"] == 20 and len(detail["jobs"]) == 20
    listing = fw.get("/og/api/firmware/campaigns", headers=VIEWER).json()
    assert [c["campaign_id"] for c in listing["campaigns"]] == [campaign_id]
    _fixture("campaign_list", listing)


def test_large_campaign_two_person_rule(fw):
    proposed = _propose(fw, OP_A, selection={"zones": ["NORTH"]})
    campaign_id = proposed["campaign"]["campaign_id"]
    assert proposed["campaign"]["second_operator_reasons"] == ["MORE_THAN_THRESHOLD_HUBS"]
    confirmed = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/confirm", headers=OP_A)
    assert confirmed.json()["campaign"]["state"] == "PROPOSED"
    same = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/approve", headers=OP_A)
    assert same.status_code == 403
    approved = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/approve", headers=OP_B)
    assert approved.status_code == 200
    assert approved.json()["campaign"]["approved_by"] == "op-b"
    assert approved.json()["campaign"]["state"] == "APPROVED"


async def test_running_campaign_events_retry_and_rollback(fw, repo, fake_config):
    proposed = _propose(fw, OP_A, max_failures=1)
    campaign_id = proposed["campaign"]["campaign_id"]
    fw.post(f"/og/api/firmware/campaigns/{campaign_id}/confirm", headers=OP_A)

    async def catalogue() -> Catalogue:
        return Catalogue.build(CATALOGUE_CONFIG)

    executor = FirmwareExecutor(repo, load_firmware_config(fake_config), catalogue)
    now = datetime.now(UTC)
    await executor.step(now)
    commands = sorted(repo.command_rows)
    ok_cmd, bad_cmd = commands[0], commands[1]
    for cid in commands:
        repo.set_command(cid, status="PUBLISHED")
    ok_hub = repo.command_rows[ok_cmd].hub_id
    repo.set_command(ok_cmd, hub_state="DONE", hub_version="1.5.0")
    repo.set_version(ok_hub, "1.5.0")
    repo.set_command(bad_cmd, hub_state="FAILED", hub_reason="INSTALL_ERROR")
    await executor.step(now + timedelta(seconds=5))
    await executor.step(now + timedelta(seconds=6))

    detail = fw.get(f"/og/api/firmware/campaigns/{campaign_id}", headers=VIEWER).json()
    assert detail["campaign"]["state"] == "HALTED"
    assert detail["counts"]["FAILED"] == 1 and detail["counts"]["SUCCEEDED"] == 1
    _fixture("campaign_detail_halted", detail)

    events = fw.get(f"/og/api/firmware/campaigns/{campaign_id}/events", headers=VIEWER).json()
    names = [e["event"] for e in events["events"]]
    assert {"CAMPAIGN_CREATED", "CAMPAIGN_STARTED", "SENT", "SUCCEEDED", "FAILED", "CAMPAIGN_HALTED"} <= set(
        names
    )
    assert all(e["ts"] for e in events["events"])
    later = fw.get(
        f"/og/api/firmware/campaigns/{campaign_id}/events?after={events['next_after']}", headers=VIEWER
    )
    assert later.json()["events"] == []
    _fixture("campaign_events", events)

    # Resume is refused from HALTED; "retry failed hubs" is two-step and puts the campaign in PAUSED.
    assert fw.post(f"/og/api/firmware/campaigns/{campaign_id}/resume", headers=OP_A).status_code == 409
    retry = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/retry-failed", headers=OP_A)
    assert retry.status_code == 202 and len(retry.json()["hub_ids"]) == 1
    _fixture("retry_failed_proposal", retry.json())
    done = fw.post(
        f"/og/api/firmware/campaigns/{campaign_id}/retry-failed/{retry.json()['proposal_id']}/confirm",
        headers=OP_A,
    )
    assert done.status_code == 200 and done.json()["retried"] == 1
    assert done.json()["campaign"]["state"] == "PAUSED"

    # Per-hub rollback, two-step.
    rb = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/hubs/{ok_hub}/rollback", headers=OP_A)
    assert rb.status_code == 202
    confirmed = fw.post(
        f"/og/api/firmware/campaigns/{campaign_id}/hubs/{ok_hub}/rollback/{rb.json()['proposal_id']}/confirm",
        headers=OP_A,
    )
    assert confirmed.status_code == 200
    assert (
        confirmed.json()["job"]["action"] == "ROLLBACK"
        and confirmed.json()["job"]["target_version"] == "1.4.2"
    )
    assert repo.job_for(ok_hub).state == JobState.PENDING

    aborted = fw.post(f"/og/api/firmware/campaigns/{campaign_id}/abort", headers=OP_A)
    assert aborted.status_code == 200
    assert aborted.json()["campaign"]["state"] == CampaignState.ABORTED.value


def test_unknown_campaign_is_404(fw):
    missing = "00000000-0000-0000-0000-000000000000"
    assert fw.get(f"/og/api/firmware/campaigns/{missing}", headers=VIEWER).status_code == 404
    assert fw.post(f"/og/api/firmware/campaigns/{missing}/pause", headers=OP_A).status_code == 404
