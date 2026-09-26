"""K8 two-person stop RELEASE through the API (safety agent's spec): operator A requests, a DIFFERENT
operator B approves; the approval writes ONE og.operator_action SAFE_STOP_RELEASE (TIER2, confirmed,
operator_ref=A, approver_ref=B) for the guardian to verify and sign; the API then polls og.stop_event for
the RELEASE and answers 200 (released) or 202 (still pending with the guardian)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid.guardian.ports import EngagedStop, ReleaseRequest
from opengrid.guardian.stop_release import check_stop_release
from opengrid.platform.config import Config

ALICE = {"X-Remote-User": "alice"}
BOB = {"X-Remote-User": "bob"}


@pytest.fixture
def fake_config() -> Config:
    return Config({"api": {"sse_heartbeat_s": 15, "roles": {"operator": ["alice", "bob"], "viewer": []}}})


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr("opengrid.api.routers.safestop._RELEASE_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr("opengrid.api.routers.safestop._STOP_POLL_INTERVAL_S", 0.01)


def _request(client, scope: str = "bank", scope_id: str = "bank-01") -> str:
    resp = client.post(
        f"/og/api/safestop/{scope}/{scope_id}/release", headers=ALICE, json={"reason": "clear"}
    )
    assert resp.status_code == 202
    return resp.json()["proposal_id"]


def test_request_alone_writes_no_release_action(client, fake_store) -> None:
    _request(client)
    assert [a for a in fake_store.operator_actions if a["action_kind"] == "SAFE_STOP_RELEASE"] == []


def test_the_same_operator_cannot_approve_their_own_request(client, fake_store) -> None:
    proposal_id = _request(client)
    resp = client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=ALICE)
    assert resp.status_code == 403
    assert fake_store.operator_actions == []
    # the request is still there for a second operator
    assert client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB).status_code == 202


def test_a_second_operator_approval_writes_one_tier2_action_and_reports_pending(client, fake_store) -> None:
    proposal_id = _request(client)
    resp = client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB)

    assert resp.status_code == 202
    assert resp.json()["released"] is False
    (action,) = [a for a in fake_store.operator_actions if a["action_kind"] == "SAFE_STOP_RELEASE"]
    assert action["operator_ref"] == "alice"
    assert action["approver_ref"] == "bob"
    assert action["tier"] == "TIER2"
    assert action["target_ref"] == "BANK:bank-01"
    assert action["confirmed_at"] is not None


def test_approval_answers_200_once_the_release_event_lands(client, fake_store) -> None:
    proposal_id = _request(client)
    fake_store.stop_events[("BANK", "bank-01")] = ("RELEASE", datetime(2100, 1, 1, tzinfo=UTC))
    resp = client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB)
    assert resp.status_code == 200
    assert resp.json()["released"] is True


def test_fleet_scope_is_normalised(client, fake_store) -> None:
    proposal_id = _request(client, scope="fleet", scope_id="all")
    client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB)
    (action,) = [a for a in fake_store.operator_actions if a["action_kind"] == "SAFE_STOP_RELEASE"]
    assert action["target_ref"] == "FLEET:FLEET"


def test_the_recorded_release_passes_the_guardians_freshness_check(client, fake_store) -> None:
    """Regression (live 2026-09-26 10:18, bank-034): the guardian reads `requested_at` = the row's
    `created_at` and `approved_at` = `confirmed_at`. og-api stamped `confirmed_at` BEFORE the insert,
    so `created_at` (DB now()) was always later: every real release was refused APPROVAL_STALE.
    The row must carry the request time as `created_at` and the approval time as `confirmed_at`."""
    engaged_at = datetime.now(UTC) - timedelta(seconds=30)
    proposal_id = _request(client)
    client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB)
    (action,) = [a for a in fake_store.operator_actions if a["action_kind"] == "SAFE_STOP_RELEASE"]

    request = ReleaseRequest(
        operator_action_id=action["operator_action_id"],
        requested_by=action["operator_ref"],
        approved_by=action["approver_ref"],
        scope_kind="BANK",
        scope_ref="bank-01",
        reason="clear",
        requested_at=action["created_at"],
        approved_at=action["confirmed_at"],
        trace_id=action["trace_id"],
    )
    outcome = check_stop_release(
        request,
        now=datetime.now(UTC),
        engaged=[EngagedStop(stop_id=uuid4(), initiator_kind="SAFESTOP_AUTHORITY", engaged_at=engaged_at)],
        active_instruction_kinds=(),
        authorised_operators=("alice", "bob"),
        approval_max_age_s=60.0,
        max_clock_skew_s=5.0,
        request_traced=True,
    )
    assert outcome.ok, outcome.reason
    assert action["created_at"] <= action["confirmed_at"]


def test_approval_is_single_use(client) -> None:
    proposal_id = _request(client)
    assert client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB).status_code == 202
    assert client.post(f"/og/api/safestop/release/{proposal_id}/approve", headers=BOB).status_code == 404
