"""Opportunity submission reuses `opengrid.contracts.admit_priced`; disputes are persisted and traced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import opengrid.contracts as contracts_module

from .conftest import CONTRACT_A, CUSTOMER_A, OPERATOR, USER_A


def _window(hours_ahead: int = 2) -> dict[str, str]:
    start = datetime.now(UTC) + timedelta(hours=hours_ahead)
    return {"window_start": start.isoformat(), "window_end": (start + timedelta(hours=1)).isoformat()}


def test_submission_goes_through_admit_priced(client, contracts_repo, monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    real = contracts_module.admit_priced

    async def spy(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(contracts_module, "admit_priced", spy)
    resp = client.post(
        "/og/api/customer/opportunities",
        headers=USER_A,
        json={"contract_id": str(CONTRACT_A), **_window(), "requested_kw": "75"},
    )
    assert resp.status_code == 201
    assert len(calls) == 1 and calls[0][0] == CONTRACT_A and calls[0][3] == Decimal("75")
    body = resp.json()
    assert body["state"] == "OFFERED"
    assert [o.state for o in contracts_repo.obligations.values()] == ["OFFERED"]


def test_admission_rejection_answers_409_with_its_reason_code(client, contracts_repo) -> None:
    start = datetime.now(UTC) + timedelta(hours=2)
    resp = client.post(
        "/og/api/customer/opportunities",
        headers=USER_A,
        json={
            "contract_id": str(CONTRACT_A),
            "window_start": start.isoformat(),
            "window_end": start.isoformat(),
            "requested_kw": "10",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == resp.json()["detail"]["reason_code"] == "R-ADMIT-INVALID-WINDOW"
    assert contracts_repo.opportunities == {}


def test_non_positive_quantity_is_rejected_before_admission(client) -> None:
    resp = client.post(
        "/og/api/customer/opportunities",
        headers=USER_A,
        json={"contract_id": str(CONTRACT_A), **_window(), "requested_kw": "0"},
    )
    assert resp.status_code == 422


def test_dispute_is_persisted_traced_and_visible_to_operators(
    client, add_invoice_line, customer_store, trace_backend
) -> None:
    line = add_invoice_line(CONTRACT_A)
    resp = client.post(
        f"/og/api/customer/invoices/{line.invoice_line_id}/dispute",
        headers=USER_A,
        json={"reason": "wrong kWh"},
    )
    assert resp.status_code == 201
    dispute = next(iter(customer_store.disputes.values()))
    assert (dispute.invoice_line_id, dispute.customer_id, dispute.status) == (
        line.invoice_line_id,
        CUSTOMER_A,
        "OPEN",
    )
    traced = [r for r in trace_backend.rows if r["trace_id"] == dispute.trace_id]
    assert (
        traced
        and traced[0]["event_class"] == "CUSTOMER_DISPUTE"
        and traced[0]["stream_id"] == f"customer-{CUSTOMER_A}"
    )
    listed = client.get("/og/api/customer-disputes", headers=OPERATOR).json()
    assert [d["dispute_id"] for d in listed] == [str(dispute.dispute_id)]


def test_second_live_dispute_on_the_same_line_is_a_conflict(client, add_invoice_line) -> None:
    line = add_invoice_line(CONTRACT_A)
    url = f"/og/api/customer/invoices/{line.invoice_line_id}/dispute"
    assert client.post(url, headers=USER_A, json={"reason": "one"}).status_code == 201
    second = client.post(url, headers=USER_A, json={"reason": "two"})
    assert second.status_code == 409
    assert second.json()["detail"]["reason_code"] == "R-CUSTOMER-DISPUTE-OPEN"


def test_operator_review_closes_a_dispute_and_is_audited(
    client, add_invoice_line, customer_store, billing_store
) -> None:
    line = add_invoice_line(CONTRACT_A)
    client.post(
        f"/og/api/customer/invoices/{line.invoice_line_id}/dispute", headers=USER_A, json={"reason": "x"}
    )
    dispute_id = next(iter(customer_store.disputes))
    resp = client.patch(
        f"/og/api/customer-disputes/{dispute_id}", headers=OPERATOR, json={"status": "RESOLVED"}
    )
    assert resp.status_code == 200 and resp.json()["reviewed_by"] == "operator"
    assert billing_store.operator_actions[0]["target_ref"] == str(dispute_id)
    again = client.patch(
        f"/og/api/customer-disputes/{dispute_id}", headers=OPERATOR, json={"status": "REJECTED"}
    )
    assert again.status_code == 404


def test_only_operators_review(client, add_invoice_line, customer_store) -> None:
    line = add_invoice_line(CONTRACT_A)
    client.post(
        f"/og/api/customer/invoices/{line.invoice_line_id}/dispute", headers=USER_A, json={"reason": "x"}
    )
    dispute_id = next(iter(customer_store.disputes))
    url = f"/og/api/customer-disputes/{dispute_id}"
    assert (
        client.patch(url, headers={"X-Remote-User": "viewer"}, json={"status": "RESOLVED"}).status_code == 403
    )
    assert client.patch(url, headers=USER_A, json={"status": "RESOLVED"}).status_code == 403
