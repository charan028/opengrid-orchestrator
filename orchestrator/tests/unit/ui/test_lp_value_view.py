"""Profitability's LP value-added panel (GET /og/api/profitability/lp-value): latest gate + trend, never a
sum across gates (their horizons overlap), and "not available yet" when the API says so."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.profitability as profitability_route
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.settlement import LP_VALUE_PATH, SETTLEMENT_VIEW_PATH, lp_value_view

from .conftest import load_fixture


def test_latest_gate_is_shown_and_nothing_is_summed() -> None:
    view = lp_value_view(load_fixture("profitability_lp_value.json"))
    assert view["available"] and view["count"] == 2
    assert view["latest"]["value_added"] == pytest.approx(26.0)  # the latest gate, not 22.3 + 26.0
    assert view["latest"]["plan_id"].endswith("0002")
    assert [p[1] for p in view["chart"]["series"][0]["data"]] == pytest.approx([22.3, 26.0])
    assert {b["label"] for b in view["latest"]["breakdown"]} == {
        "energy arbitrage",
        "ancillary services",
        "degradation",
    }


@pytest.mark.parametrize(
    "payload",
    [{"available": False, "items": []}, {"available": False, "items": [{"value_added": "1"}]}, None, [], {}],
)
def test_not_available(payload: Any) -> None:
    assert lp_value_view(payload)["available"] is False


def test_a_bare_list_is_accepted_and_ordered_by_time() -> None:
    rows = list(reversed(load_fixture("profitability_lp_value.json")["items"]))
    assert lp_value_view(rows)["latest"]["value_added"] == pytest.approx(26.0)


def _serve(monkeypatch: pytest.MonkeyPatch, lp: Any) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == SETTLEMENT_VIEW_PATH:
            return load_fixture("views_settlement.json")
        if path == LP_VALUE_PATH:
            if isinstance(lp, int):
                raise ApiUnavailable("x", status_code=lp)
            return lp
        raise ApiUnavailable("no fixture", status_code=404)

    monkeypatch.setattr(profitability_route, "get_json", fake_get_json)


def test_panel_renders(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, load_fixture("profitability_lp_value.json"))
    html = client.get("/og/profitability", headers={"X-Remote-User": "alice"}).text
    assert 'id="kpi-lp-value-added"' in html and "26.00" in html and "never summed" in html


@pytest.mark.parametrize(
    ("lp", "text"),
    [({"available": False, "items": []}, "Not available yet"), (404, "not available on this deployment yet")],
)
def test_panel_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch, lp: Any, text: str) -> None:
    _serve(monkeypatch, lp)
    html = client.get("/og/profitability", headers={"X-Remote-User": "alice"}).text
    assert 'id="lp-value-unavailable"' in html and text in html
