"""The request/response shapes the customer simulator (`integration-sims/src/ogsim/customer`) uses:
payloads as `ogsim.customer.requests` builds them, `{"obligations": [...]}` / `{"invoices": [...]}` with
an `id` per item, disputes by `reason_code`, and renomination by window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from opengrid.core.models.engine import RenominationPoint

from .conftest import CONTRACT_A, CUSTOMER_A, CUSTOMER_B, USER_A


def _sim_opportunity(**overrides: object) -> dict[str, object]:
    start = datetime.now(UTC) + timedelta(hours=2)
    payload: dict[str, object] = {
        "contract_id": str(CONTRACT_A),
        "customer_id": str(CUSTOMER_A),
        "service_profile": "DATA_CENTER",
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(hours=1)).isoformat(),
        "requested_kw": 250.0,
    }
    return payload | overrides


def test_sim_opportunity_payload_is_admitted(client) -> None:
    resp = client.post("/og/api/customer/opportunities", headers=USER_A, json=_sim_opportunity())
    assert resp.status_code == 201
    assert resp.json()["id"] == resp.json()["opportunity_id"]


def test_sim_payload_naming_another_customer_is_not_found(client, contracts_repo) -> None:
    resp = client.post(
        "/og/api/customer/opportunities", headers=USER_A, json=_sim_opportunity(customer_id=str(CUSTOMER_B))
    )
    assert resp.status_code == 404
    assert contracts_repo.opportunities == {}


def test_sim_malformed_payload_is_rejected(client, contracts_repo) -> None:
    malformed = _sim_opportunity(window_start="not-a-timestamp", unexpected_field="x" * 100)
    assert client.post("/og/api/customer/opportunities", headers=USER_A, json=malformed).status_code == 422
    assert contracts_repo.opportunities == {}


def test_obligations_come_wrapped_with_ids(client, add_obligation) -> None:
    obligation = add_obligation(CONTRACT_A, "COMMITTED")
    obligations = client.get("/og/api/customer/obligations", headers=USER_A).json()["obligations"]
    assert [o["id"] for o in obligations] == [str(obligation.obligation_id)]


def test_invoices_default_window_includes_the_current_month(client, add_invoice_line, billing_store) -> None:
    today = datetime.now(UTC).date()
    line = add_invoice_line(CONTRACT_A)
    billing_store.lines[:] = [
        line.model_copy(update={"period_start": today.replace(day=1), "period_end": today})
    ]
    invoices = client.get("/og/api/customer/invoices", headers=USER_A).json()["invoices"]
    assert [i["id"] for i in invoices] == [str(line.invoice_line_id)]


def test_dispute_by_reason_code(client, add_invoice_line, customer_store) -> None:
    line = add_invoice_line(CONTRACT_A)
    resp = client.post(
        f"/og/api/customer/invoices/{line.invoice_line_id}/dispute",
        headers=USER_A,
        json={"reason_code": "USAGE_MISMATCH"},
    )
    assert resp.status_code == 201
    assert next(iter(customer_store.disputes.values())).reason_code == "USAGE_MISMATCH"


def test_dispute_without_any_reason_is_rejected(client, add_invoice_line) -> None:
    line = add_invoice_line(CONTRACT_A)
    resp = client.post(f"/og/api/customer/invoices/{line.invoice_line_id}/dispute", headers=USER_A, json={})
    assert resp.status_code == 422


def test_renomination_by_window(client, add_obligation, customer_store) -> None:
    obligation = add_obligation(CONTRACT_A, "DELIVERING")
    customer_store.points.append(
        RenominationPoint(
            renomination_point_id=uuid4(),
            contract_id=CONTRACT_A,
            obligation_id=obligation.obligation_id,
            scheduled_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    new_start = obligation.window_start + timedelta(hours=1)
    resp = client.post(
        f"/og/api/customer/obligations/{obligation.obligation_id}/renominate",
        headers=USER_A,
        json={"window_start": new_start.isoformat(), "window_end": obligation.window_end.isoformat()},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["requested_kw"] is None
    assert datetime.fromisoformat(body["requested_window_start"]) == new_start


def test_renomination_asking_for_nothing_is_rejected(client, add_obligation) -> None:
    obligation = add_obligation(CONTRACT_A, "DELIVERING")
    url = f"/og/api/customer/obligations/{obligation.obligation_id}/renominate"
    assert client.post(url, headers=USER_A, json={}).status_code == 422
    start = datetime.now(UTC).isoformat()
    assert client.post(url, headers=USER_A, json={"window_start": start}).status_code == 422
