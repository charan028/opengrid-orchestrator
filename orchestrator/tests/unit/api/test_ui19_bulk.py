"""Gitea #19: `POST /og/api/fleet/commands/bulk` + `/bulk/{id}/confirm` -- the double-confirm rule,
reuse of the single-hub guardian path, per-hub outcomes, tracing, and authz."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS
from .ui19_fakes import FakeExtViews, FleetFakeStore, build_app, hub_row, make_client

STREAM = "operator_action:operator"


@pytest.fixture
def views() -> FakeExtViews:
    return FakeExtViews()


@pytest.fixture
def store(views) -> FleetFakeStore:
    return FleetFakeStore(views.rows)


@pytest.fixture
def bulk_config() -> Config:
    return Config(
        {
            "api": {
                "roles": {"operator": [], "viewer": []},
                "bulk_commands": {"max_hubs": 100, "concurrency": 4},
            }
        }
    )


@pytest.fixture
def client(views, store, fake_trace_store, fake_proposals, bulk_config) -> TestClient:
    return make_client(build_app(views, store, fake_trace_store, fake_proposals, bulk_config), PROXY_HEADERS)


def _propose(client: TestClient, hub_ids: list[str], **extra) -> dict:
    resp = client.post(
        "/og/api/fleet/commands/bulk",
        json={"hub_ids": hub_ids, "p_kw_setpoint": 0.0, "reason": "feeder work", **extra},
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _confirm(client: TestClient, proposal_id: str, headers=OPERATOR_HEADERS):
    return client.post(f"/og/api/fleet/commands/bulk/{proposal_id}/confirm", headers=headers)


def _steps(trace_store) -> list[str]:
    records = asyncio.run(trace_store._backend.fetch_range(STREAM, from_seq=0))
    return [r.payload["step"] for r in records if r.event_class == "OPERATOR_BULK_COMMAND"]


def test_single_confirm_executes_through_the_single_hub_path(client, store, fake_trace_store) -> None:
    hubs = [f"hub-{i:05d}" for i in (5, 6, 45, 46)]  # banks 5, 6: idle, nothing committed
    proposal = _propose(client, hubs)
    assert proposal["requires_double_confirm"] is False
    assert proposal["confirmations_required"] == 1 and proposal["hub_count"] == 4
    assert store.command_batches == []  # nothing commanded at propose time

    resp = _confirm(client, proposal["proposal_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "EXECUTED"
    assert body["outcome_counts"] == {"PASS": 4}
    assert [r["hub_id"] for r in body["results"]] == hubs
    # one command_batch + one MANUAL_COMMAND operator action per hub (the fleet.py path), plus the bulk row
    assert len(store.command_batches) == 4
    kinds = [a["target_ref"] for a in store.operator_actions]
    assert kinds.count(f"bulk:{proposal['proposal_id']}") == 1
    assert len(store.operator_actions) == 5
    assert _steps(fake_trace_store) == ["PROPOSE", "CONFIRM", "RESULT"]
    assert asyncio.run(fake_trace_store.verify(STREAM)).ok  # concurrent per-hub appends did not fork


def test_committed_obligation_and_fault_require_a_second_confirm(client, store, fake_trace_store) -> None:
    proposal = _propose(client, ["hub-00000", "hub-00003", "hub-00005"])
    assert proposal["requires_double_confirm"] is True
    assert proposal["confirmations_required"] == 2
    why = {r["hub_id"]: r["reasons"] for r in proposal["double_confirm_reasons"]}
    assert why == {"hub-00000": ["SERVES_COMMITTED_OBLIGATION"], "hub-00003": ["HUB_FAULT"]}
    assert proposal["reason_counts"] == {"SERVES_COMMITTED_OBLIGATION": 1, "HUB_FAULT": 1}
    assert proposal["double_confirm_reasons"][0]["obligations"][0]["state"] == "DELIVERING"

    first = _confirm(client, proposal["proposal_id"])
    assert first.status_code == 200
    assert first.json()["status"] == "AWAITING_SECOND_CONFIRM"
    assert store.command_batches == []

    second = _confirm(client, proposal["proposal_id"])
    assert second.status_code == 200
    assert second.json()["status"] == "EXECUTED" and second.json()["hub_count"] == 3
    assert _steps(fake_trace_store) == ["PROPOSE", "CONFIRM_1", "CONFIRM_2", "RESULT"]
    assert _confirm(client, proposal["proposal_id"]).status_code == 404  # consumed


def test_critical_alert_and_reserve_floor_require_a_second_confirm(views, client) -> None:
    views.critical = {("bank", "bank-007")}
    views.rows[8] = hub_row(8, kw=0.0, soc_kwh=5.0)  # below the 7.84 kWh reserve
    views.rows[9] = hub_row(9, health=None, kw=None)  # offline: a warning only
    proposal = _propose(client, ["hub-00007", "hub-00008", "hub-00009"])
    why = {r["hub_id"]: r["reasons"] for r in proposal["double_confirm_reasons"]}
    assert why == {"hub-00007": ["CRITICAL_ALERT"], "hub-00008": ["AT_OR_BELOW_RESERVE"]}
    assert proposal["warnings"] == [{"hub_id": "hub-00009", "warnings": ["HUB_OFFLINE"]}]


def test_guardian_veto_is_reported_per_hub(client, store) -> None:
    store.next_verdict_outcome = "VETOED"
    store.next_vetoed_rule_ids = ["G-04"]
    proposal = _propose(client, ["hub-00005", "hub-00006"])
    body = _confirm(client, proposal["proposal_id"]).json()
    assert body["outcome_counts"] == {"VETOED": 2}
    assert body["results"][0]["vetoed_rule_ids"] == ["G-04"]


def test_unknown_hub_and_too_many_hubs_are_422(client) -> None:
    resp = client.post(
        "/og/api/fleet/commands/bulk",
        json={"hub_ids": ["hub-00005", "hub-99999"], "p_kw_setpoint": 1.0, "reason": "x"},
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 422 and resp.json()["detail"]["hub_ids"] == ["hub-99999"]
    resp = client.post(
        "/og/api/fleet/commands/bulk",
        json={"hub_ids": [f"hub-{i:05d}" for i in range(101)], "p_kw_setpoint": 1.0, "reason": "x"},
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 422
    resp = client.post(
        "/og/api/fleet/commands/bulk",
        json={"hub_ids": [], "p_kw_setpoint": 1.0, "reason": "x"},
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 422


def test_unknown_proposal_is_404(client) -> None:
    assert _confirm(client, str(uuid4())).status_code == 404


def test_viewer_cannot_propose_or_confirm_and_the_deny_is_audited(client, fake_trace_store) -> None:
    resp = client.post(
        "/og/api/fleet/commands/bulk",
        json={"hub_ids": ["hub-00005"], "p_kw_setpoint": 1.0, "reason": "x"},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403
    records = asyncio.run(fake_trace_store._backend.fetch_range("authz_deny:viewer", from_seq=0))
    assert records and records[0].payload["action"] == "api.write"
    proposal = _propose(client, ["hub-00005"])
    assert _confirm(client, proposal["proposal_id"], headers=VIEWER_HEADERS).status_code == 403


def test_bulk_needs_an_identity(client) -> None:
    resp = client.post(
        "/og/api/fleet/commands/bulk", json={"hub_ids": ["hub-00005"], "p_kw_setpoint": 1.0, "reason": "x"}
    )
    assert resp.status_code == 401
