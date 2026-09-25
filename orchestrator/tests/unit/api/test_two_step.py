"""Two-step confirmation (02b S7.1/S7.3): propose -> confirm, expiry, unknown/wrong-kind proposals,
and a guardian veto surfacing as 409."""

from __future__ import annotations

from uuid import uuid4

import pytest

from opengrid.core.models.engine import Verdict

from .conftest import OPERATOR_HEADERS


class _StubVerdict:
    def __init__(self, outcome: str, vetoed_rule_ids: list[str] | None = None) -> None:
        self.outcome = outcome
        self.vetoed_rule_ids = vetoed_rule_ids or []


@pytest.fixture
def _passing_guardian(monkeypatch) -> None:
    async def _fake_evaluate_and_sign(batch):
        return Verdict(
            verdict_id=uuid4(),
            command_batch_id=batch.command_batch_id,
            outcome="PASS",
            latency_ms=5,
            inputs_hash="deadbeef",
        )

    async def _fake_ledger_version() -> int:
        return 1

    monkeypatch.setattr("opengrid.api.routers.fleet.evaluate_and_sign", _fake_evaluate_and_sign)
    monkeypatch.setattr("opengrid.api.routers.fleet.ledger_version", _fake_ledger_version)


@pytest.fixture
def _vetoing_guardian(monkeypatch) -> None:
    async def _fake_evaluate_and_sign(batch):
        return Verdict(
            verdict_id=uuid4(),
            command_batch_id=batch.command_batch_id,
            outcome="VETOED",
            vetoed_rule_ids=["G-19"],
            latency_ms=5,
            inputs_hash="deadbeef",
        )

    async def _fake_ledger_version() -> int:
        return 1

    monkeypatch.setattr("opengrid.api.routers.fleet.evaluate_and_sign", _fake_evaluate_and_sign)
    monkeypatch.setattr("opengrid.api.routers.fleet.ledger_version", _fake_ledger_version)


def _propose_command(client) -> str:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"bank_id": "bank-01", "p_kw_setpoint": 3.5, "reason": "demo"},
    )
    assert resp.status_code == 202
    return resp.json()["proposal_id"]


def test_command_confirm_success(client, _passing_guardian, fake_store) -> None:
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "PASS"
    assert body["trace_id"] is not None
    assert fake_store.operator_actions[-1]["action_kind"] == "MANUAL_COMMAND"


def test_command_confirm_veto_is_409(client, _vetoing_guardian) -> None:
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["outcome"] == "VETOED"
    assert "G-19" in resp.json()["detail"]["vetoed_rule_ids"]


def test_command_confirm_unknown_proposal_404(client, _passing_guardian) -> None:
    resp = client.post(f"/og/api/fleet/command/{uuid4()}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


def test_command_confirm_is_single_use(client, _passing_guardian) -> None:
    proposal_id = _propose_command(client)
    first = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert first.status_code == 200
    second = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert second.status_code == 404


def test_command_confirm_expired_proposal_is_410(client, fake_proposals, _passing_guardian) -> None:
    proposal_id = _propose_command(client)
    # Force the 60 s TTL to have elapsed without waiting real time in the test.
    from uuid import UUID

    stored = fake_proposals._proposals[UUID(proposal_id)]
    stored.created_at -= 120.0
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 410


# -- safestop -----------------------------------------------------------------------------------------


@pytest.fixture
def _safestop_ok(monkeypatch) -> None:
    async def _fake_engage(scope, scope_ref, reason, initiator_ref) -> None:
        return None

    async def _fake_release(scope, scope_ref, approver_ref) -> None:
        return None

    monkeypatch.setattr("opengrid.api.routers.safestop.engage", _fake_engage)
    monkeypatch.setattr("opengrid.api.routers.safestop.release", _fake_release)


def test_safestop_propose_confirm(client, _safestop_ok, fake_store) -> None:
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "bank", "scope_id": "bank-01", "reason": "overload drill"},
    )
    assert propose.status_code == 202
    proposal_id = propose.json()["proposal_id"]

    confirm = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["engaged"] is True
    assert fake_store.operator_actions[-1]["action_kind"] == "SAFE_STOP_ENGAGE"


def test_safestop_release(client, _safestop_ok) -> None:
    resp = client.post(
        "/og/api/safestop/bank/bank-01/release", headers=OPERATOR_HEADERS, json={"reason": "clear"}
    )
    assert resp.status_code == 200
    assert resp.json()["released"] is True


def test_safestop_confirm_expired_is_410(client, fake_proposals, _safestop_ok) -> None:
    from uuid import UUID

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
