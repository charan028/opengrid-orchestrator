"""View-model tests for the markets & feeds screen (02b S8 screen 4), maps to TS-10-* (UI).

No HTTP, no DB: every function under test is pure, given JSON fixtures shaped like the
`opengrid.api` responses documented in 02b S7.1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opengrid.ui.routes.markets import forecast_band_view, freshness_table_view, series_chart_view

_FIXTURES = Path(__file__).parent / "fixtures"
_NOW = datetime(2026, 9, 25, 18, 10, tzinfo=UTC)


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_series_chart_view_sorts_by_time_and_reports_latest() -> None:
    observations = _load("markets_series_price.json")

    view = series_chart_view(observations, series_key="price")

    assert view["chart_option"]["xAxis"]["data"] == [
        "2026-09-25T17:45:00+00:00",
        "2026-09-25T18:00:00+00:00",
        "2026-09-25T18:15:00+00:00",
    ]
    assert view["latest_value"] == 41.0
    assert view["latest_ts"] == "2026-09-25T18:15:00+00:00"
    assert view["unit"] == "usd_per_mwh"
    assert view["point_count"] == 3


def test_series_chart_view_handles_empty_observations() -> None:
    view = series_chart_view([], series_key="wind")

    assert view["latest_value"] is None
    assert view["point_count"] == 0


def test_forecast_band_view_computes_stacked_band_deltas() -> None:
    forecast = _load("markets_forecast.json")

    view = forecast_band_view(forecast)

    series_by_name = {s["name"]: s for s in view["chart_option"]["series"]}
    assert series_by_name["P10"]["data"] == [20.0, 22.0, 21.0]
    assert series_by_name["P10-P50"]["data"] == [10.0, 11.0, 10.0]
    assert series_by_name["P50"]["data"] == [30.0, 33.0, 31.0]
    assert series_by_name["P50-P90"]["data"] == [15.0, 17.0, 16.0]
    assert view["step_count"] == 3


def test_freshness_table_view_reports_age_mode_and_breaker() -> None:
    feed_statuses = _load("markets_feed_status.json")

    rows = freshness_table_view(feed_statuses, now=_NOW)

    by_source = {row["source"]: row for row in rows}
    assert by_source["ercot"]["mode"] == "LIVE"
    assert by_source["ercot"]["age_s"] == 30.0
    assert by_source["sim"]["mode"] == "SIM"
    assert by_source["eia"]["breaker_open"] is True
    assert by_source["eia"]["consecutive_failures"] == 4
