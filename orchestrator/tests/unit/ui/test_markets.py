"""View-model tests for the markets & feeds screen (02b S8 screen 4), maps to TS-10-* (UI).

No HTTP, no DB: every function under test is pure, given JSON fixtures shaped like the
`opengrid.api` responses documented in 02b S7.1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opengrid.ui.routes.markets import (
    bid_funnel_view,
    forecast_band_view,
    freshness_table_view,
    series_chart_view,
)

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


def test_bid_funnel_view_totals_stages_and_percentages_of_available() -> None:
    payload = _load("markets_bid_funnel.json")

    view = bid_funnel_view(payload, now=_NOW)

    assert view["has_data"] is True
    assert view["totals"] == {"available": 108, "submitted": 69, "awarded": 44, "rejected": 25}
    # Fixed stage order, and every percentage is of `available` so the four bars narrow as one funnel.
    assert [(s["key"], s["count"], s["pct"]) for s in view["stages"]] == [
        ("available", 108, 100.0),
        ("submitted", 69, 63.9),
        ("awarded", 44, 40.7),
        ("rejected", 25, 23.1),
    ]
    assert [s["label"] for s in view["stages"]] == [
        "Made available by ERCOT",
        "We submitted",
        "We won",
        "Rejected",
    ]


def test_bid_funnel_view_reports_win_rate_of_submitted_per_product() -> None:
    payload = _load("markets_bid_funnel.json")

    view = bid_funnel_view(payload, now=_NOW)

    # awarded / submitted, not awarded / available: opportunities we skipped were a decision, not a loss.
    assert [(row["product"], row["win_rate_pct"]) for row in view["rows"]] == [
        ("energy", 70.0),
        ("RRS", 60.0),
        ("ECRS", 50.0),
        ("NSPIN", 61.1),
    ]
    by_product = {row["product"]: row for row in view["rows"]}
    assert by_product["NSPIN"]["available"] == 24
    assert by_product["NSPIN"]["submitted"] == 18
    assert by_product["NSPIN"]["awarded"] == 11
    assert by_product["NSPIN"]["rejected"] == 7


def test_bid_funnel_view_aggregates_rejection_reasons_across_products() -> None:
    payload = _load("markets_bid_funnel.json")

    view = bid_funnel_view(payload, now=_NOW)

    # PRICE_ABOVE_CLEARING is 6+4+5 across three products; the two four-count reasons tie and break on
    # the code so the list cannot reshuffle between 30 s polls. Humanised label, raw code kept alongside.
    assert [(r["reason"], r["label"], r["count"], r["pct_of_rejected"]) for r in view["reasons"]] == [
        ("PRICE_ABOVE_CLEARING", "Price above clearing", 15, 60.0),
        ("INSUFFICIENT_CAPACITY", "Insufficient capacity", 4, 16.0),
        ("TELEMETRY_GAP", "Telemetry gap", 4, 16.0),
        ("LATE_SUBMISSION", "Late submission", 2, 8.0),
    ]
    # The reasons account for every rejection, so the panel never implies an unexplained remainder.
    assert sum(r["count"] for r in view["reasons"]) == view["totals"]["rejected"]


def test_bid_funnel_view_empty_state_when_endpoint_returns_nothing() -> None:
    # The endpoint 404s today, so the route passes None; the template needs `has_data` false and lists it
    # can still iterate over rather than a KeyError mid-render.
    view = bid_funnel_view(None, now=_NOW)

    assert view["has_data"] is False
    assert view["stages"] == []
    assert view["rows"] == []
    assert view["reasons"] == []
    assert view["totals"] == {"available": 0, "submitted": 0, "awarded": 0, "rejected": 0}
    assert bid_funnel_view({}, now=_NOW)["has_data"] is False
    assert bid_funnel_view({"products": []}, now=_NOW)["has_data"] is False


def test_bid_funnel_view_reports_zero_percent_for_a_quiet_window() -> None:
    # A window where ERCOT offered nothing and we bid on nothing is a quiet hour, not a division error.
    view = bid_funnel_view(
        {"products": [{"product": "RegDn", "available": 0, "submitted": 0, "awarded": 0, "rejected": 0}]},
        now=_NOW,
    )

    assert view["has_data"] is True
    assert [s["pct"] for s in view["stages"]] == [0.0, 0.0, 0.0, 0.0]
    assert view["rows"][0]["win_rate_pct"] == 0.0
    assert view["reasons"] == []
