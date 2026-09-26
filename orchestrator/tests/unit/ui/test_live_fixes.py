"""U1 live-run fixes on the Dispatch and Health screens (see tests-e2e/ui/README.md, live findings)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.routes.dispatch as dispatch
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.dispatch import pipeline_view, plan_view
from opengrid.ui.routes.health import ack_message, format_age

_NOW = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)


def test_pipeline_view_separates_open_from_closed_obligations() -> None:
    rows = [
        {"obligation_id": "a", "state": "OFFERED", "contract_id": "c1"},
        {"obligation_id": "b", "state": "EXPIRED", "contract_id": "c1"},
        {"obligation_id": "c", "state": "REJECTED", "contract_id": "c2"},
    ]
    view = pipeline_view(rows, now=_NOW)
    assert view["total"] == 3 and view["open"] == 1 and view["closed"] == 2


def test_plan_view_horizon_is_shown_to_the_minute() -> None:
    view = plan_view(
        {"plan_mode": "L-ID", "horizon_start": "2026-09-25T22:34:38.153420-05:00", "horizon_end": None}
    )
    assert view["horizon_display"] == "2026-09-25T22:34-05:00 to -"
    assert view["horizon_start"] == "2026-09-25T22:34:38.153420-05:00"  # raw value kept for the badge


def test_dispatch_opens_on_a_real_bank(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_paths: list[str] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/fleet/hubs":
            return {
                "items": [
                    {"hub_id": "hub-00001", "bank_id": "bank-001"},
                    {"hub_id": "h", "bank_id": "bank-000"},
                ]
            }
        if path.startswith("/og/api/ledger/"):
            ledger_paths.append(path)
            return {"reservations": [], "grants": [], "commitments": [], "bank_capacity_kw": 0}
        return [] if "opportunities" in path else None

    monkeypatch.setattr(dispatch, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    assert TestClient(app).get("/og/dispatch").status_code == 200
    assert ledger_paths == ["/og/api/ledger/bank-000/timeline"]


def test_format_age_is_human() -> None:
    assert format_age(None) == "unknown"
    assert format_age(5) == "5s"
    assert format_age(2075) == "34m"
    assert format_age(5675) == "1h 34m"


def test_ack_message_is_operator_readable() -> None:
    assert ack_message(ApiUnavailable("x", status_code=404), 1) == "No open alert with id 1."
    assert "operator" in ack_message(ApiUnavailable("x", status_code=403), 1).lower()
    assert "unreachable" in ack_message(ApiUnavailable("x"), 7)
    assert "https://" not in ack_message(
        ApiUnavailable("Client error ... https://developer.mozilla.org", status_code=500), 7
    )


def test_pipeline_cards_carry_energy_fields_or_dash() -> None:
    rows = [
        {"obligation_id": "a", "state": "COMMITTED", "energy_margin_kwh": 12.5, "time_to_depletion_h": 0.67},
        {"obligation_id": "b", "state": "COMMITTED"},
    ]
    cards = pipeline_view(rows, now=_NOW)["columns"][2]["items"]
    assert cards[0]["energy_margin_kwh"] == 12.5 and cards[0]["time_to_depletion_h"] == 0.67
    assert cards[1]["energy_margin_kwh"] is None and cards[1]["time_to_depletion_h"] is None


def test_dispatch_page_renders_energy_line(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if "opportunities" in path:
            return [
                {
                    "obligation_id": "a",
                    "state": "COMMITTED",
                    "energy_margin_kwh": 12.5,
                    "time_to_depletion_h": 0.67,
                }
            ]
        if path.startswith("/og/api/ledger/"):
            return {"reservations": [], "grants": [], "commitments": [], "bank_capacity_kw": 0}
        return {"items": [{"hub_id": "h", "bank_id": "bank-000"}]} if path.endswith("/hubs") else None

    monkeypatch.setattr(dispatch, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    body = TestClient(app).get("/og/dispatch").text
    assert "energy margin: 12.50 kWh" in body and "depletes in: 0.67h" in body
