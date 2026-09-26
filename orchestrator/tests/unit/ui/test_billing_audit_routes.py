"""Route-level tests for the Billing & audit screen's mutating/relay actions (BUILD.md code-review round
item 1/4): the chain-verify POST and the CSV export both now go through the one shared
`opengrid.ui.api_client` (`post_json`/`get_bytes`) instead of a private `_post_json` copy plus a second
bare `httpx.AsyncClient`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_run_chain_verify_renders_pass_result_via_shared_post_json(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/trace/verify": {"passed": True, "checked": 12, "first_broken": None}})

    response = client.post("/og/billing/trace/verify")

    assert response.status_code == 200
    body = response.text
    assert "PASS" in body
    assert "12 streams verified" in body


def test_run_chain_verify_degrades_when_api_unavailable(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({})  # nothing registered -> ApiUnavailable

    response = client.post("/og/billing/trace/verify")

    assert response.status_code == 200
    assert "FAIL" in response.text


def test_export_invoice_lines_csv_relays_via_shared_get_bytes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_bytes(path: str, *, params: dict[str, Any] | None = None) -> bytes:
        assert path == "/og/api/billing/invoice-lines"
        assert params is not None and params.get("format") == "csv"
        return b"invoice_line_id,amount\nLINE-1,10.0\n"

    import opengrid.ui.routes.billing_audit as billing_audit

    monkeypatch.setattr(billing_audit, "api_get_bytes", fake_get_bytes)

    response = client.get("/og/billing/invoice-lines/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content == b"invoice_line_id,amount\nLINE-1,10.0\n"
