"""U1 live-run defect: `/og/billing` called the invoice-lines endpoint with no `from`/`to`, which the API
requires as dates, so the screen always rendered degraded (422)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.routes.billing_audit as billing
from opengrid.ui.routes.billing_audit import api_date


def test_api_date_defaults_and_truncates() -> None:
    default = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    assert api_date(None, default) == "2026-09-25"
    assert api_date("", default) == "2026-09-25"
    assert api_date("2026-09-20T08:15", default) == "2026-09-20"
    assert api_date("2026-09-20", default) == "2026-09-20"


def test_billing_page_always_sends_a_date_period(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, dict[str, Any] | None] = {}

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        calls[path] = params
        return {"lines": [], "performance": []} if "invoice" in path else {"events": []}

    monkeypatch.setattr(billing, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    client = TestClient(app)
    assert client.get("/og/billing").status_code == 200
    invoice = calls[billing._INVOICE_LINES_PATH]
    assert invoice is not None and len(invoice["from"]) == 10 and len(invoice["to"]) == 10
    assert invoice["from"] < invoice["to"]

    assert client.get("/og/billing?from=2026-09-01T00:00&to=2026-09-02T00:00").status_code == 200
    assert calls[billing._INVOICE_LINES_PATH] == {"from": "2026-09-01", "to": "2026-09-02"}
