"""Two-step confirmation (02b S7.1/S7.3): propose -> confirm, expiry, unknown/wrong-kind proposals,
a guardian veto surfacing as 409, and the "dependency process not running" 503 paths.

`fleet`'s confirm writes a `command_batch` + K10 trace pre-image and polls `og.verdict` for the
independently-running `og-guardian` process's result; `safestop`'s propose/confirm NOTIFY
`opengrid.safestop.pg_backend.REQUEST_CHANNEL` and poll `og.stop_event` for `og-safestop`'s response.
`FakeStore` (`fakes.py`) simulates both processes responding instantly so these tests never wait out
the real poll timeouts, and can be toggled off to exercise the 503 "not running" path.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from .conftest import OPERATOR_HEADERS


def _propose_command(client) -> str:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"bank_id": "bank-01", "p_kw_setpoint": 3.5, "reason": "demo"},
    )
    assert resp.status_code == 202
    return resp.json()["proposal_id"]


def test_command_confirm_success(client, fake_store) -> None:
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "PASS"
    assert body["trace_id"] is not None
    assert fake_store.operator_actions[-1]["action_kind"] == "MANUAL_COMMAND"
    assert fake_store.command_batches[-1].cycle_id == "MANUAL"


def test_command_confirm_veto_is_409(client, fake_store) -> None:
    fake_store.next_verdict_outcome = "VETOED"
    fake_store.next_vetoed_rule_ids = ["G-19"]
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["outcome"] == "VETOED"
    assert "G-19" in resp.json()["detail"]["vetoed_rule_ids"]


def test_command_confirm_no_verdict_is_503(client, fake_store, monkeypatch) -> None:
    """og-guardian never inserts a verdict (e.g. not running) -- polling must time out honestly."""
    monkeypatch.setattr("opengrid.api.routers.fleet._VERDICT_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr("opengrid.api.routers.fleet._VERDICT_POLL_INTERVAL_S", 0.01)

    async def _no_verdict(command_batch_id):
        return None

    monkeypatch.setattr(fake_store, "get_verdict", _no_verdict)
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 503


def test_command_confirm_unknown_proposal_404(client) -> None:
    resp = client.post(f"/og/api/fleet/command/{uuid4()}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


def test_command_confirm_is_single_use(client) -> None:
    proposal_id = _propose_command(client)
    first = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert first.status_code == 200
    second = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert second.status_code == 404


def test_command_confirm_expired_proposal_is_410(client, fake_proposals) -> None:
    proposal_id = _propose_command(client)
    # Force the 60 s TTL to have elapsed without waiting real time in the test.
    stored = fake_proposals._proposals[UUID(proposal_id)]
    stored.created_at -= 120.0
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 410


# -- safestop -----------------------------------------------------------------------------------------


def test_safestop_propose_confirm(client, fake_store) -> None:
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "bank", "scope_id": "bank-01", "reason": "overload drill"},
    )
    assert propose.status_code == 202
    proposal_id = propose.json()["proposal_id"]
    assert fake_store.notifications[0]["action"] == "PROPOSE"

    confirm = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["engaged"] is True
    assert fake_store.operator_actions[-1]["action_kind"] == "SAFE_STOP_ENGAGE"


def test_safestop_confirm_no_response_is_503(client, fake_store, monkeypatch) -> None:
    monkeypatch.setattr("opengrid.api.routers.safestop._STOP_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr("opengrid.api.routers.safestop._STOP_POLL_INTERVAL_S", 0.01)
    fake_store.safestop_responds = False
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "fleet", "scope_id": None, "reason": "drill"},
    )
    proposal_id = propose.json()["proposal_id"]
    resp = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 503


def test_safestop_release_request_is_step_one_of_two(client, fake_store) -> None:
    """K8: a release is the guardian's two-person path; the request alone releases nothing and writes
    no SAFE_STOP_RELEASE action (test_safestop_release.py covers the approval)."""
    resp = client.post(
        "/og/api/safestop/bank/bank-01/release", headers=OPERATOR_HEADERS, json={"reason": "clear"}
    )
    assert resp.status_code == 202
    assert fake_store.operator_actions == []


def test_safestop_confirm_expired_is_410(client, fake_proposals) -> None:
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "fleet", "scope_id": None, "reason": "drill"},
    )
    proposal_id = propose.json()["proposal_id"]
    stored = fake_proposals._proposals[UUID(proposal_id)]
    stored.created_at -= 120.0
    resp = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 410
