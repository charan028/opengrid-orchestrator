"""Profitability's $/kW panel (GET /og/api/profitability/per-kw): fleet, markets, contracts (same labels as
the settlement tables), the reference unit, and a clear message when the endpoint is absent or operator-only."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.profitability as profitability_route
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.settlement import PER_KW_PATH, SETTLEMENT_VIEW_PATH, per_kw_view

from .conftest import load_fixture


def test_per_kw_rows_use_the_settlement_contract_label() -> None:
    view = per_kw_view(load_fixture("profitability_per_kw.json"), load_fixture("views_settlement.json"))
    assert view is not None
    labels = [(r["kind"], r["label"]) for r in view["rows"]]
    assert labels[0] == ("fleet", "Fleet")
    # d02 is not in the settlement fixture's contracts: short-code fallback, never a raw UUID
    assert ("contract", "contract d02") in labels
    assert labels[-1][0] == "reference"
    fleet = view["rows"][0]
    assert fleet["net_per_kw"] == pytest.approx(49.650757575757574)
    contract = next(r for r in view["rows"] if r["kind"] == "contract")
    assert contract["payback_years"] == pytest.approx(12.8168, abs=1e-3) and contract["meets_target"] is False
    assert per_kw_view(None, {}) is None


def _serve(monkeypatch: pytest.MonkeyPatch, per_kw: Any) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == SETTLEMENT_VIEW_PATH:
            return load_fixture("views_settlement.json")
        if path == PER_KW_PATH:
            if isinstance(per_kw, int):
                raise ApiUnavailable("x", status_code=per_kw)
            return per_kw
        raise ApiUnavailable("no fixture", status_code=404)

    monkeypatch.setattr(profitability_route, "get_json", fake_get_json)


def test_panel_renders_for_an_operator(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, load_fixture("profitability_per_kw.json"))
    html = client.get("/og/profitability", headers={"X-Remote-User": "alice"}).text
    assert 'id="per-kw-table"' in html and "Free market (ERCOT competitive)" in html
    assert "not exposed by the optimizer yet" in html


@pytest.mark.parametrize(
    ("status", "text"), [(404, "not available on this deployment yet"), (403, "Operator role required")]
)
def test_panel_explains_why_it_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, status: int, text: str
) -> None:
    _serve(monkeypatch, status)
    html = client.get("/og/profitability", headers={"X-Remote-User": "viewer"}).text
    assert 'id="per-kw-unavailable"' in html and text in html
    assert 'id="pnl-table"' in html  # the rest of the screen still renders
