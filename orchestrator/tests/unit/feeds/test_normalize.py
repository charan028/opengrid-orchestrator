"""02b S2.7 normalization: ERCOT/EIA/NWS payload shapes (`interfaces/http/market-api.md`) -> FeedObs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.feeds.normalize import (
    EIA_SYSTEM_LOAD_SERIES,
    FeedDataError,
    eia_demand_to_feed_obs,
    ercot_as_price_to_feed_obs,
    ercot_load_to_feed_obs,
    ercot_spp_to_feed_obs,
    ercot_wind_to_feed_obs,
    nws_forecast_to_feed_obs,
)

RECORDED_AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def test_ercot_spp_envelope() -> None:
    payload = {
        "data": [
            ["2026-09-26", "2026-09-26T18:00:00", "LZ_NORTH", "42.17"],
            ["2026-09-26", "2026-09-26T18:00:00", "LZ_SOUTH", "41.85"],
        ],
        "fields": [
            {"name": "deliveryDate", "dataType": "DATE"},
            {"name": "deliveryDateTime", "dataType": "TIMESTAMP"},
            {"name": "settlementPoint", "dataType": "STRING"},
            {"name": "settlementPointPrice", "dataType": "STRING"},
        ],
    }
    rows = ercot_spp_to_feed_obs(payload, product="np6-905-cd", recorded_at=RECORDED_AT)
    assert len(rows) == 2
    assert rows[0].series == "LZ_NORTH"
    assert rows[0].value == pytest.approx(42.17)
    assert rows[0].source == "ERCOT"
    assert rows[0].unit == "usd_per_mwh"
    assert rows[0].quality == "GOOD"


def test_ercot_envelope_missing_fields_raises() -> None:
    with pytest.raises(FeedDataError):
        ercot_spp_to_feed_obs({"data": []}, product="np6-905-cd", recorded_at=RECORDED_AT)


def test_ercot_envelope_wrong_arity_raises() -> None:
    payload = {
        "data": [["2026-09-26"]],  # too few columns
        "fields": [
            {"name": "deliveryDate"},
            {"name": "deliveryDateTime"},
            {"name": "settlementPoint"},
            {"name": "settlementPointPrice"},
        ],
    }
    with pytest.raises(FeedDataError):
        ercot_spp_to_feed_obs(payload, product="np6-905-cd", recorded_at=RECORDED_AT)


def test_ercot_load_by_weather_zone() -> None:
    payload = {
        "data": [["2026-09-26T18:00:00", "LZ_WEST", "1234.5"]],
        "fields": [
            {"name": "operatingDateTime"},
            {"name": "weatherZone"},
            {"name": "load"},
        ],
    }
    rows = ercot_load_to_feed_obs(payload, product="np6-345-cd", recorded_at=RECORDED_AT)
    assert rows[0].series == "LZ_WEST"
    assert rows[0].unit == "mw"


def test_ercot_wind_actual_and_forecast_two_series() -> None:
    payload = {
        "data": [["2026-09-26T18:00:00", "5000", "5200"]],
        "fields": [
            {"name": "postedDatetime"},
            {"name": "actualSystemWideWindOutput"},
            {"name": "windOutputForecastSystemWide"},
        ],
    }
    rows = ercot_wind_to_feed_obs(payload, product="np4-732-cd", recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"actual", "forecast"}
    actual = next(r for r in rows if r.series == "actual")
    forecast = next(r for r in rows if r.series == "forecast")
    assert actual.value == pytest.approx(5000)
    assert forecast.value == pytest.approx(5200)


def test_ercot_as_price_hour_ending_maps_to_utc() -> None:
    payload = {
        "data": [["2026-09-26", "14:00", "REGUP", "23.5"]],
        "fields": [
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "ancillaryType"},
            {"name": "mcpc"},
        ],
    }
    rows = ercot_as_price_to_feed_obs(payload, product="np4-188-cd", recorded_at=RECORDED_AT)
    assert rows[0].series == "REGUP"
    assert rows[0].ts == datetime(2026, 9, 26, 13, tzinfo=UTC)  # hour-ending 14:00 -> interval start 13:00
    assert rows[0].value == pytest.approx(23.5)


def test_eia_demand_marked_estimated() -> None:
    payload = {
        "response": {
            "data": [
                {"period": "2026-09-26T18", "respondent": "ERCO", "type": "D", "value": "52104"},
            ]
        }
    }
    rows = eia_demand_to_feed_obs(payload, recorded_at=RECORDED_AT)
    assert rows[0].source == "EIA"
    assert rows[0].quality == "ESTIMATED"
    assert rows[0].series == EIA_SYSTEM_LOAD_SERIES
    assert rows[0].ts == datetime(2026, 9, 26, 18, tzinfo=UTC)


def test_eia_malformed_row_raises() -> None:
    with pytest.raises(FeedDataError):
        eia_demand_to_feed_obs({"response": {"data": [{"period": "bad"}]}}, recorded_at=RECORDED_AT)


def test_nws_forecast_three_series_and_f_to_c() -> None:
    payload = {
        "properties": {
            "updated": "2026-09-26T17:00:00+00:00",
            "periods": [
                {
                    "number": 1,
                    "startTime": "2026-09-26T18:00:00-05:00",
                    "temperature": 91,
                    "dewpoint": {"unitCode": "wmoUnit:degC", "value": 21.1},
                    "skyCover": 20,
                }
            ],
        }
    }
    rows = nws_forecast_to_feed_obs(payload, recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"temperature", "dewpoint", "sky_cover"}
    temp = next(r for r in rows if r.series == "temperature")
    assert temp.value == pytest.approx((91 - 32) * 5 / 9)
    assert temp.source == "NWS"


def test_nws_missing_periods_raises() -> None:
    with pytest.raises(FeedDataError):
        nws_forecast_to_feed_obs({"properties": {"periods": []}}, recorded_at=RECORDED_AT)
