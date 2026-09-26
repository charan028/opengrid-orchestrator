"""Cancel and renominate under the commitment lock (K13): pre-commit cancel applies, anything later is an
operator-reviewed request or a queued renomination, and a committed obligation is never changed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import uuid4

import pytest

from opengrid.core.models.engine import ObligationState, RenominationPoint
from opengrid.customer_api.rules import (
    R_CUSTOMER_CANCEL_FINISHED,
    R_CUSTOMER_CANCEL_LOCKED_REVIEW,
    R_CUSTOMER_CANCEL_PRE_COMMIT,
    R_CUSTOMER_RENOM_NO_POINT,
    R_CUSTOMER_RENOM_NOT_ALLOWED,
    R_CUSTOMER_RENOM_NOT_COMMITTED,
    Disposition,
    cancel_decision,
    renominate_decision,
)

from .conftest import CONTRACT_A, CONTRACT_B, OPERATOR, USER_A, USER_B

ALL_STATES: tuple[ObligationState, ...] = get_args(ObligationState)
LOCKED = {"COMMITTED", "DELIVERING"}
FINISHED = {"REJECTED", "EXPIRED", "FULFILLED", "SHORTFALL", "SETTLED"}


@pytest.mark.parametrize("state", ALL_STATES)
def test_cancel_rule_covers_every_state(state: ObligationState) -> None:
    decision = cancel_decision(state)
    if state == "OFFERED":
        assert decision.disposition is Disposition.APPLY_NOW
    elif state in LOCKED or state == "SELECTED":
        assert decision.disposition is Disposition.OPERATOR_REVIEW
    else:
        assert state in FINISHED and decision.disposition is Disposition.REFUSE


@pytest.mark.parametrize("state", ALL_STATES)
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("point", [True, False])
def test_renominate_rule_only_queues_a_locked_obligation_with_a_point(state, allowed, point) -> None:
    decision = renominate_decision(state, renomination_allowed=allowed, has_upcoming_point=point)
    queued = allowed and point and state in LOCKED
    assert (decision.disposition is Disposition.QUEUE_FOR_RENOMINATION) == queued
    assert decision.disposition in (Disposition.QUEUE_FOR_RENOMINATION, Disposition.REFUSE)


def test_pre_commit_cancel_rejects_the_obligation_through_contracts(
    client, add_obligation, contracts_repo, customer_store
) -> None:
    obligation = add_obligation(CONTRACT_A, "OFFERED")
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/cancel",
        headers=USER_A,
        json={"reason": "not needed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPLIED" and resp.json()["rule_code"] == R_CUSTOMER_CANCEL_PRE_COMMIT
    after = contracts_repo.obligations[obligation.obligation_id]
    assert after.state == "REJECTED" and after.last_reason_code == "R-ADMIT-REJECT"
    assert len(customer_store.requests) == 1


@pytest.mark.parametrize("state", ["SELECTED", "COMMITTED", "DELIVERING"])
def test_cancel_after_selection_is_a_review_request_and_the_commitment_is_untouched(
    client, add_obligation, contracts_repo, customer_store, state
) -> None:
    obligation = add_obligation(CONTRACT_A, state)
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/cancel", headers=USER_A, json={}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert (
        body["status"] == "PENDING_OPERATOR_REVIEW" and body["rule_code"] == R_CUSTOMER_CANCEL_LOCKED_REVIEW
    )
    assert body["penalty_terms"] == {"penalty_alpha": "0.02", "penalty_beta": "0.4", "penalty_theta": "0.05"}
    assert contracts_repo.obligations[obligation.obligation_id] == obligation  # K13: nothing changed


def test_duplicate_live_cancel_request_is_a_conflict(client, add_obligation) -> None:
    obligation = add_obligation(CONTRACT_A, "COMMITTED")
    url = f"/og/api/customer/obligations/{obligation.obligation_id}/cancel"
    assert client.post(url, headers=USER_A, json={}).status_code == 202
    assert (
        client.post(url, headers=USER_A, json={}).json()["detail"]["reason_code"] == "R-CUSTOMER-REQUEST-OPEN"
    )


@pytest.mark.parametrize("state", sorted(FINISHED))
def test_cancel_of_a_finished_obligation_is_refused(client, add_obligation, customer_store, state) -> None:
    obligation = add_obligation(CONTRACT_A, state)
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/cancel", headers=USER_A, json={}
    )
    assert resp.status_code == 409 and resp.json()["detail"]["reason_code"] == R_CUSTOMER_CANCEL_FINISHED
    assert customer_store.requests == {}


def test_cancel_racing_a_selector_gate_is_refused_without_a_request(
    client, add_obligation, contracts_repo, customer_store, monkeypatch
) -> None:
    obligation = add_obligation(CONTRACT_A, "OFFERED")
    real_get = customer_store.obligation_for_customer

    async def stale_read(customer_id, obligation_id):
        seen = await real_get(customer_id, obligation_id)
        contracts_repo.obligations[obligation_id] = seen.model_copy(update={"state": "SELECTED"})
        return seen

    monkeypatch.setattr(customer_store, "obligation_for_customer", stale_read)
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/cancel", headers=USER_A, json={}
    )
    assert (
        resp.status_code == 409 and resp.json()["detail"]["reason_code"] == "R-CUSTOMER-CANCEL-STATE-CHANGED"
    )
    assert customer_store.requests == {}
    assert contracts_repo.obligations[obligation.obligation_id].state == "SELECTED"


def _point(contract_id, obligation_id, *, hours: float) -> RenominationPoint:
    return RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=contract_id,
        obligation_id=obligation_id,
        scheduled_at=datetime.now(UTC) + timedelta(hours=hours),
    )


def test_renomination_is_queued_for_the_next_point(
    client, add_obligation, contracts_repo, customer_store
) -> None:
    obligation = add_obligation(CONTRACT_A, "DELIVERING")
    later = _point(CONTRACT_A, obligation.obligation_id, hours=3)
    first = _point(CONTRACT_A, None, hours=2)
    customer_store.points += [later, first]
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/renominate",
        headers=USER_A,
        json={"requested_kw": "60"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED_FOR_RENOMINATION"
    assert body["renomination_point_id"] == str(first.renomination_point_id)
    assert body["requested_kw"] == "60"
    assert contracts_repo.obligations[obligation.obligation_id] == obligation  # K13: nothing changed


def test_renomination_needs_contract_permission(client, add_obligation, customer_store) -> None:
    obligation = add_obligation(CONTRACT_B, "COMMITTED")
    customer_store.points.append(_point(CONTRACT_B, obligation.obligation_id, hours=2))
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/renominate",
        headers=USER_B,
        json={"requested_kw": "5"},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["reason_code"] == R_CUSTOMER_RENOM_NOT_ALLOWED


def test_renomination_needs_an_upcoming_point_inside_the_window(
    client, add_obligation, customer_store
) -> None:
    obligation = add_obligation(CONTRACT_A, "COMMITTED")
    customer_store.points.append(_point(CONTRACT_A, obligation.obligation_id, hours=12))  # after window_end
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/renominate",
        headers=USER_A,
        json={"requested_kw": "5"},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["reason_code"] == R_CUSTOMER_RENOM_NO_POINT


def test_renomination_before_commitment_is_refused(client, add_obligation, customer_store) -> None:
    obligation = add_obligation(CONTRACT_A, "OFFERED")
    customer_store.points.append(_point(CONTRACT_A, obligation.obligation_id, hours=2))
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/renominate",
        headers=USER_A,
        json={"requested_kw": "5"},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["reason_code"] == R_CUSTOMER_RENOM_NOT_COMMITTED


def test_operator_review_of_a_request_records_the_decision_only(
    client, add_obligation, contracts_repo, customer_store
) -> None:
    obligation = add_obligation(CONTRACT_A, "COMMITTED")
    client.post(f"/og/api/customer/obligations/{obligation.obligation_id}/cancel", headers=USER_A, json={})
    request_id = next(iter(customer_store.requests))
    resp = client.patch(
        f"/og/api/customer-requests/{request_id}",
        headers=OPERATOR,
        json={"status": "ACCEPTED", "note": "penalty per contract"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "ACCEPTED"
    assert contracts_repo.obligations[obligation.obligation_id] == obligation
