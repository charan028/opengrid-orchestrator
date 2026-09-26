"""Workstation review of integ/ui-merge (defects 1-4, c, 6). The identity/timeout tests use the real
`opengrid.ui.api_client` over an httpx MockTransport, so the headers and timeout are the ones really sent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import opengrid.ui.api_client as api_client
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.billing_audit import export_period
from opengrid.ui.routes.dispatch import as_awards_view, contract_products
from opengrid.ui.routes.fleet import api_error_result

from .conftest import PROXY_SECRET

OP = {"X-Remote-User": "alice"}


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route the real api_client through a MockTransport; record method/path/headers/timeout/status."""
    seen: list[dict[str, Any]] = []
    responses: dict[str, tuple[int, Any]] = {}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = responses.get(request.url.path, (200, {}))
        seen.append(
            {
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
                "timeout": request.extensions.get("timeout"),
            }
        )
        return (
            httpx.Response(status, json=body)
            if not isinstance(body, bytes)
            else httpx.Response(status, content=body)
        )

    def patched(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(api_client.httpx, "AsyncClient", patched)
    seen_responses = seen
    seen_responses.append({"_responses": responses})  # first entry: the response table
    return seen_responses


def _set(transport: list[dict[str, Any]], path: str, status: int, body: Any) -> None:
    transport[0]["_responses"][path] = (status, body)


# 1 -------------------------------------------------------------------------------------------------
def test_release_review_dialog_renders_a_js_null_expiry(client: TestClient) -> None:
    html = client.post("/og/fleet/safestop/release/review", data={"proposal_id": "p-1"}, headers=OP).text
    assert "remaining: null," in html and "remaining: None" not in html


# 2 -------------------------------------------------------------------------------------------------
def test_release_approve_waits_15s_and_renders_pending_on_202(
    client: TestClient, transport: list[Any]
) -> None:
    _set(
        transport,
        "/og/api/safestop/release/p-1/approve",
        202,
        {"proposal_id": "p-1", "scope": "BANK", "scope_ref": "bank-007", "released": False, "trace_id": "t"},
    )
    html = client.post("/og/fleet/safestop/release/p-1/approve", headers=OP).text
    assert "PENDING" in html and "bank-007" in html
    call = next(c for c in transport[1:] if c["path"].endswith("/approve"))
    assert call["timeout"]["read"] == 15.0
    assert call["headers"]["x-remote-user"] == "alice" and call["headers"]["x-og-proxy-auth"] == PROXY_SECRET


# 3 -------------------------------------------------------------------------------------------------
def test_chain_verify_forwards_the_identity_and_secret(client: TestClient, transport: list[Any]) -> None:
    _set(transport, "/og/api/trace/verify", 200, {"passed": True, "checked": 12, "first_broken": None})
    html = client.post("/og/billing/trace/verify", headers=OP).text
    call = next(c for c in transport[1:] if c["path"] == "/og/api/trace/verify")
    assert call["headers"]["x-remote-user"] == "alice" and call["headers"]["x-og-proxy-auth"] == PROXY_SECRET
    assert "12" in html


# 4 -------------------------------------------------------------------------------------------------
def test_a_409_veto_is_unwrapped_and_rendered_as_vetoed(client: TestClient, transport: list[Any]) -> None:
    veto = {"proposal_id": "c-1", "outcome": "VETOED", "vetoed_rule_ids": ["G-02"], "trace_id": "t-v"}
    _set(transport, "/og/api/fleet/command/c-1/confirm", 409, {"detail": veto})
    html = client.post("/og/fleet/command/c-1/confirm", headers=OP).text
    assert "VETOED" in html and "G-02" in html and "FAILED" not in html


def test_api_error_result_shapes() -> None:
    wrapped = ApiUnavailable("x", status_code=409, detail={"detail": {"outcome": "VETOED"}})
    assert api_error_result(wrapped) == {"outcome": "VETOED"}
    assert api_error_result(ApiUnavailable("x", status_code=410, detail={"detail": "expired"})) is None
    assert api_error_result(ApiUnavailable("x")) is None


# (c) -----------------------------------------------------------------------------------------------
def test_as_award_product_comes_from_the_contract() -> None:
    products = contract_products(
        [{"contract_id": "d03", "variant": "ECRS"}, {"contract_id": "d09", "variant": "NSPIN"}]
    )
    awards = [
        {"service_type": "ERCOT_AS", "obligation_id": "o1", "contract_id": "d03", "committed_qty_kw": "100"},
        {"service_type": "ERCOT_AS", "obligation_id": "o2", "contract_id": "d09", "committed_qty_kw": "100"},
        {"service_type": "ERCOT_AS", "obligation_id": "o3", "contract_id": "dxx", "committed_qty_kw": "100"},
    ]
    rows = {
        r["obligation_id"]: r
        for r in as_awards_view(awards, [], now=datetime.now(UTC), product_by_contract=products)
    }
    assert (rows["o1"]["product"], rows["o1"]["required_hours"], rows["o1"]["required_energy_kwh"]) == (
        "ECRS",
        1,
        100.0,
    )
    assert (rows["o2"]["product"], rows["o2"]["required_hours"]) == ("NSPIN", 4)
    assert rows["o3"]["required_hours"] is None and rows["o3"]["required_energy_kwh"] is None


# 6 -------------------------------------------------------------------------------------------------
def test_export_period_defaults_and_cuts_datetimes() -> None:
    now = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)  # 22:00 on Sep 26 in Chicago
    # `to` defaults to tomorrow (Chicago): the API keeps period_end <= to, so today's lines are included
    assert export_period(None, None, now=now) == ("2026-09-01", "2026-09-27")
    month_end = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # 23:00 on Sep 30 in Chicago
    assert export_period(None, None, now=month_end) == ("2026-09-01", "2026-10-01")
    assert export_period("2026-09-20T10:00", "2026-09-25T18:30", now=now) == ("2026-09-20", "2026-09-25")


def test_export_always_sends_both_dates_and_explains_errors(client: TestClient, transport: list[Any]) -> None:
    _set(transport, "/og/api/billing/invoice-lines", 200, b"invoice_line_id,amount\n")
    ok = client.get("/og/billing/invoice-lines/export.csv", params={"from": "", "to": ""}, headers=OP)
    assert ok.status_code == 200 and ok.text.startswith("invoice_line_id")
    call = next(c for c in transport[1:] if c["path"] == "/og/api/billing/invoice-lines")
    assert call["headers"].get("x-remote-user") == "alice"
    assert (
        len(call["params"]["from"]) == 10
        and len(call["params"]["to"]) == 10
        and call["params"]["format"] == "csv"
    )
    _set(transport, "/og/api/billing/invoice-lines", 422, {"detail": "bad dates"})
    bad = client.get("/og/billing/invoice-lines/export.csv", params={"from": "2026-09-20T10:00"}, headers=OP)
    assert bad.status_code == 422 and "could not be produced" in bad.text
