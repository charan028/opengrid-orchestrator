"""U1 live-run defect: the Markets screen queried `/og/api/markets/series` with `series_key=price` etc.,
which the API matches against raw `feed_obs.series` values (`LZ_NORTH`, `total`, `actual`, `RRS`), so
every chart was empty and every tile read "None"."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.routes.markets as markets
from opengrid.ui.routes.markets import SERIES_KEYS, series_chart_view


def test_every_screen_series_queries_a_real_product() -> None:
    assert set(markets._SERIES_QUERY) == set(SERIES_KEYS)
    assert all(q["product"].startswith("np") for q in markets._SERIES_QUERY.values())


def test_series_chart_view_plots_one_line_per_zone_and_averages_the_latest() -> None:
    rows = [
        {"ts": "2026-09-26T03:00:00Z", "series": "LZ_NORTH", "value": 20.0, "unit": "usd_per_mwh"},
        {"ts": "2026-09-26T03:00:00Z", "series": "LZ_SOUTH", "value": 30.0, "unit": "usd_per_mwh"},
        {"ts": "2026-09-26T02:45:00Z", "series": "LZ_NORTH", "value": 10.0, "unit": "usd_per_mwh"},
    ]
    view = series_chart_view(rows, series_key="price")
    names = {line["name"] for line in view["chart_option"]["series"]}
    assert names == {"LZ_NORTH", "LZ_SOUTH"}
    assert view["chart_option"]["xAxis"]["data"] == ["2026-09-26T02:45:00Z", "2026-09-26T03:00:00Z"]
    assert view["latest_value"] == 25.0 and view["latest_ts"] == "2026-09-26T03:00:00Z"
    assert view["point_count"] == 3


def test_single_series_product_keeps_the_screen_label() -> None:
    rows = [{"ts": "2026-09-26T03:00:00Z", "series": "total", "value": 41000.0, "unit": "mw"}]
    view = series_chart_view(rows, series_key="load")
    assert view["chart_option"]["series"][0]["name"] == "Load (MW)"
    assert view["latest_value"] == 41000.0


def test_markets_page_sends_product_params(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any] | None] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/markets/series":
            seen.append(params)
            return []
        return {"feeds": []} if path.endswith("/health") else {"ts": [], "p10": [], "p50": [], "p90": []}

    monkeypatch.setattr(markets, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    assert TestClient(app).get("/og/markets").status_code == 200
    assert seen and all(p is not None and "product" in p for p in seen)


def test_markets_page_asks_the_forecast_for_a_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/forecast":
            seen.update(params or {})
            return {"ts": [], "p10": [], "p50": [], "p90": []}
        return [] if "series" in path else {"feeds": []}

    monkeypatch.setattr(markets, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    assert TestClient(app).get("/og/markets").status_code == 200
    assert seen == {"series_key": markets._FORECAST_ZONE, "kind": "price"}
    assert seen["series_key"].startswith("LZ_")
