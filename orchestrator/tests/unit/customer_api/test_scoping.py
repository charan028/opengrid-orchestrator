"""Customer scoping: a customer sees and acts on its own data only, and roles do not cross over."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.api.app import create_app

from .conftest import CONTRACT_A, CONTRACT_B, CUSTOMER_A, OPERATOR, USER_A, USER_B, VIEWER


def test_customer_api_is_not_mounted_unless_enabled(monkeypatch) -> None:
    """Ships dark: `[api.customer_api].enabled` defaults to false (no OG_CONFIG here at all)."""
    monkeypatch.delenv("OG_CONFIG", raising=False)
    app = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(p.startswith("/og/api/customer") for p in paths)


def test_me_reports_the_mapped_customer(client) -> None:
    body = client.get("/og/api/customer/me", headers=USER_A).json()
    assert body == {"user": "og-cust-a", "customer_id": str(CUSTOMER_A)}


def test_operator_and_viewer_cannot_use_the_customer_api(client) -> None:
    assert client.get("/og/api/customer/obligations", headers=OPERATOR).status_code == 403
    assert client.get("/og/api/customer/obligations", headers=VIEWER).status_code == 403


def test_customer_without_a_valid_customer_id_mapping_is_forbidden(client) -> None:
    assert client.get("/og/api/customer/me", headers={"X-Remote-User": "og-cust-bad"}).status_code == 403


def test_customer_api_requires_the_proxy_secret(client) -> None:
    resp = client.get("/og/api/customer/me", headers={**USER_A, "X-OG-Proxy-Auth": "wrong"})
    assert resp.status_code == 401


def test_customer_cannot_reach_operator_endpoints(client) -> None:
    assert client.get("/og/api/customer-disputes", headers=USER_A).status_code == 403
    assert client.get("/og/api/contracts", headers=USER_A).status_code == 403


def test_contracts_are_scoped(client) -> None:
    ids = {c["id"] for c in client.get("/og/api/customer/contracts", headers=USER_A).json()["contracts"]}
    assert ids == {str(CONTRACT_A)}


def test_obligations_list_only_the_callers(client, add_obligation) -> None:
    own = add_obligation(CONTRACT_A, "COMMITTED")
    add_obligation(CONTRACT_B, "COMMITTED")
    rows = client.get("/og/api/customer/obligations", headers=USER_A).json()["obligations"]
    assert [r["obligation_id"] for r in rows] == [str(own.obligation_id)]
    assert rows[0]["id"] == str(own.obligation_id)
    assert rows[0]["state"] == "COMMITTED"
    assert rows[0]["at_risk"] is False


def test_another_customers_obligation_is_not_found(client, add_obligation) -> None:
    other = add_obligation(CONTRACT_B, "DELIVERING")
    base = f"/og/api/customer/obligations/{other.obligation_id}"
    assert client.get(base, headers=USER_A).status_code == 404
    assert client.post(f"{base}/cancel", headers=USER_A, json={}).status_code == 404
    assert client.post(f"{base}/renominate", headers=USER_A, json={"requested_kw": "5"}).status_code == 404
    assert client.get(base, headers=USER_B).status_code == 200


def test_invoice_lines_are_scoped(client, add_invoice_line) -> None:
    own = add_invoice_line(CONTRACT_A)
    add_invoice_line(CONTRACT_B)
    body = client.get("/og/api/customer/invoices?from=2026-09-01&to=2026-09-30", headers=USER_A).json()
    assert [line["id"] for line in body["invoices"]] == [str(own.invoice_line_id)]


def test_another_customers_invoice_line_cannot_be_disputed(client, add_invoice_line, customer_store) -> None:
    other = add_invoice_line(CONTRACT_B)
    resp = client.post(
        f"/og/api/customer/invoices/{other.invoice_line_id}/dispute", headers=USER_A, json={"reason": "no"}
    )
    assert resp.status_code == 404
    assert customer_store.disputes == {}


def test_opportunity_under_another_customers_contract_is_not_found(client, contracts_repo) -> None:
    start = datetime.now(UTC) + timedelta(hours=2)
    resp = client.post(
        "/og/api/customer/opportunities",
        headers=USER_A,
        json={
            "contract_id": str(CONTRACT_B),
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(hours=1)).isoformat(),
            "requested_kw": "50",
        },
    )
    assert resp.status_code == 404
    assert contracts_repo.opportunities == {}


def test_lists_of_disputes_and_requests_are_scoped(client, add_invoice_line, add_obligation) -> None:
    client.post(
        f"/og/api/customer/invoices/{add_invoice_line(CONTRACT_B).invoice_line_id}/dispute",
        headers=USER_B,
        json={"reason": "B disputes"},
    )
    client.post(
        f"/og/api/customer/obligations/{add_obligation(CONTRACT_B, 'COMMITTED').obligation_id}/cancel",
        headers=USER_B,
        json={},
    )
    assert client.get("/og/api/customer/disputes", headers=USER_A).json() == []
    assert client.get("/og/api/customer/requests", headers=USER_A).json() == []
    assert len(client.get("/og/api/customer/disputes", headers=USER_B).json()) == 1
    assert len(client.get("/og/api/customer/requests", headers=USER_B).json()) == 1
