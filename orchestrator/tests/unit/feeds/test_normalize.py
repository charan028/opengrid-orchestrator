"""02b S2.7 normalization: ERCOT/EIA/NWS payload shapes (`interfaces/http/market-api.md`) -> FeedObs.

Fixture payloads mirror the REAL ERCOT field names/shapes confirmed against a live call (BUILD.md
follow-up finding), not the originally-assumed shapes (`deliveryDateTime`, tall `weatherZone`/`load`,
`actualSystemWideWindOutput`, lowercase `mcpc`, ...).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.feeds.normalize import (
    EIA_SYSTEM_LOAD_SERIES,
    FeedDataError,
    eia_demand_to_feed_obs,
    ercot_as_price_to_feed_obs,
    ercot_load_to_feed_obs,
    ercot_solar_to_feed_obs,
    ercot_spp_to_feed_obs,
    ercot_wind_to_feed_obs,
    nws_forecast_to_feed_obs,
)

RECORDED_AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def test_ercot_spp_envelope() -> None:
    payload = {
        "data": [
            ["2026-09-26", 1, 1, "LZ_NORTH", "LZ", 42.17, False],
            ["2026-09-26", 1, 1, "LZ_SOUTH", "LZ", 41.85, False],
        ],
        "fields": [
            {"name": "deliveryDate", "dataType": "DATE"},
            {"name": "deliveryHour", "dataType": "INTEGER"},
            {"name": "deliveryInterval", "dataType": "INTEGER"},
            {"name": "settlementPoint", "dataType": "STRING"},
            {"name": "settlementPointType", "dataType": "STRING"},
            {"name": "settlementPointPrice", "dataType": "NUMBER"},
            {"name": "DSTFlag", "dataType": "BOOLEAN"},
        ],
    }
    rows = ercot_spp_to_feed_obs(payload, product="np6-905-cd", recorded_at=RECORDED_AT)
    assert len(rows) == 2
    assert rows[0].series == "LZ_NORTH"
    assert rows[0].value == pytest.approx(42.17)
    assert rows[0].source == "ERCOT"
    assert rows[0].unit == "usd_per_mwh"
    assert rows[0].quality == "GOOD"
    # deliveryHour=1, deliveryInterval=1, DSTFlag=False -> local 00:00-00:15 CDT (UTC-5, late Sept) -> 05:00Z.
    assert rows[0].ts == datetime(2026, 9, 26, 5, 0, tzinfo=UTC)


def test_ercot_spp_interval_of_hour_offsets_by_15_minutes() -> None:
    payload = {
        "data": [["2026-09-26", 1, 3, "LZ_NORTH", "LZ", 42.17, False]],
        "fields": [
            {"name": "deliveryDate"},
            {"name": "deliveryHour"},
            {"name": "deliveryInterval"},
            {"name": "settlementPoint"},
            {"name": "settlementPointType"},
            {"name": "settlementPointPrice"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_spp_to_feed_obs(payload, product="np6-905-cd", recorded_at=RECORDED_AT)
    # interval 3 of hour-ending 1 -> local 00:30-00:45 CDT (UTC-5) -> 05:30Z.
    assert rows[0].ts == datetime(2026, 9, 26, 5, 30, tzinfo=UTC)


def test_ercot_envelope_missing_fields_raises() -> None:
    with pytest.raises(FeedDataError):
        ercot_spp_to_feed_obs({"data": []}, product="np6-905-cd", recorded_at=RECORDED_AT)


def test_ercot_envelope_wrong_arity_raises() -> None:
    payload = {
        "data": [["2026-09-26"]],  # too few columns
        "fields": [
            {"name": "deliveryDate"},
            {"name": "deliveryHour"},
            {"name": "deliveryInterval"},
            {"name": "settlementPoint"},
            {"name": "settlementPointType"},
            {"name": "settlementPointPrice"},
            {"name": "DSTFlag"},
        ],
    }
    with pytest.raises(FeedDataError):
        ercot_spp_to_feed_obs(payload, product="np6-905-cd", recorded_at=RECORDED_AT)


def test_ercot_load_by_weather_zone_wide_format() -> None:
    payload = {
        "data": [["2026-09-26", "01:00", 100.0, 200.0, None, 50.0, 60.0, 70.0, 80.0, 90.0, 650.0, False]],
        "fields": [
            {"name": "operatingDay"},
            {"name": "hourEnding"},
            {"name": "coast"},
            {"name": "east"},
            {"name": "farWest"},
            {"name": "north"},
            {"name": "northC"},
            {"name": "southern"},
            {"name": "southC"},
            {"name": "west"},
            {"name": "total"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_load_to_feed_obs(payload, product="np6-345-cd", recorded_at=RECORDED_AT)
    series = {r.series: r.value for r in rows}
    assert series["coast"] == pytest.approx(100.0)
    assert series["total"] == pytest.approx(650.0)
    assert "farWest" not in series  # null value skipped, not raised
    assert all(r.unit == "mw" for r in rows)
    # hourEnding "01:00" -> local 00:00-01:00 CDT (UTC-5) -> 05:00Z.
    assert rows[0].ts == datetime(2026, 9, 26, 5, 0, tzinfo=UTC)


def test_ercot_wind_actual_and_forecast_two_series() -> None:
    payload = {
        "data": [["2026-09-26T12:00:00", "2026-09-26", 1, 5000.0, 5100.0, 5200.0, 5300.0, 5400.0, False]],
        "fields": [
            {"name": "postedDatetime"},
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "genSystemWide"},
            {"name": "COPHSLSystemWide"},
            {"name": "STWPFSystemWide"},
            {"name": "WGRPPSystemWide"},
            {"name": "HSLSystemWide"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_wind_to_feed_obs(payload, product="np4-732-cd", recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"wind_actual", "wind_forecast"}
    actual = next(r for r in rows if r.series == "wind_actual")
    forecast = next(r for r in rows if r.series == "wind_forecast")
    assert actual.value == pytest.approx(5000.0)  # genSystemWide
    assert forecast.value == pytest.approx(5200.0)  # STWPFSystemWide


def test_ercot_wind_null_actual_skipped_for_forecast_horizon_row() -> None:
    """`genSystemWide` is null for a future delivery hour (confirmed live) -- only `forecast` posts."""
    payload = {
        "data": [["2026-09-26T12:00:00", "2026-10-02", 1, None, 11834.9, 12615.7, 7378.7, None, False]],
        "fields": [
            {"name": "postedDatetime"},
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "genSystemWide"},
            {"name": "COPHSLSystemWide"},
            {"name": "STWPFSystemWide"},
            {"name": "WGRPPSystemWide"},
            {"name": "HSLSystemWide"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_wind_to_feed_obs(payload, product="np4-732-cd", recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"wind_forecast"}


def test_ercot_solar_uses_stppf_forecast_column() -> None:
    payload = {
        "data": [["2026-09-26T12:00:00", "2026-09-26", 1, 0.0, 0.0, 0.0, 0.0, 0.0, False]],
        "fields": [
            {"name": "postedDatetime"},
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "genSystemWide"},
            {"name": "COPHSLSystemWide"},
            {"name": "STPPFSystemWide"},
            {"name": "PVGRPPSystemWide"},
            {"name": "HSLSystemWide"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_solar_to_feed_obs(payload, product="np4-737-cd", recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"solar_actual", "solar_forecast"}


def test_ercot_as_price_uppercase_mcpc_and_hour_ending_maps_to_utc() -> None:
    payload = {
        "data": [["2026-09-26", "14:00", "REGUP", 23.5, False]],
        "fields": [
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "ancillaryType"},
            {"name": "MCPC"},
            {"name": "DSTFlag"},
        ],
    }
    rows = ercot_as_price_to_feed_obs(payload, product="np4-188-cd", recorded_at=RECORDED_AT)
    assert rows[0].series == "REGUP"
    # hour-ending 14:00 -> interval start 13:00 local CDT (UTC-5) -> 18:00Z.
    assert rows[0].ts == datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    assert rows[0].value == pytest.approx(23.5)


def test_ercot_as_price_missing_uppercase_mcpc_raises() -> None:
    payload = {
        "data": [["2026-09-26", "14:00", "REGUP", "23.5"]],
        "fields": [
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "ancillaryType"},
            {"name": "mcpc"},  # old (wrong) lowercase name
        ],
    }
    with pytest.raises(FeedDataError):
        ercot_as_price_to_feed_obs(payload, product="np4-188-cd", recorded_at=RECORDED_AT)


def test_ercot_dst_flag_selects_the_repeated_fallback_hour() -> None:
    """`DSTFlag=True` marks the second (standard-time) occurrence of a repeated local hour, per
    ERCOT's own convention -- not a general "is this DST" toggle."""
    payload_first = {
        "data": [["2026-11-01", "02:00", "REGUP", 10.0, True]],
        "fields": [
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "ancillaryType"},
            {"name": "MCPC"},
            {"name": "DSTFlag"},
        ],
    }
    payload_second = {
        "data": [["2026-11-01", "02:00", "REGUP", 10.0, False]],
        "fields": payload_first["fields"],
    }
    ts_dst_true = ercot_as_price_to_feed_obs(payload_first, product="np4-188-cd", recorded_at=RECORDED_AT)[
        0
    ].ts
    ts_dst_false = ercot_as_price_to_feed_obs(payload_second, product="np4-188-cd", recorded_at=RECORDED_AT)[
        0
    ].ts
    assert (
        ts_dst_true != ts_dst_false
    )  # the two folds of the ambiguous hour resolve to different UTC instants


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


def test_wind_and_solar_series_names_never_collide() -> None:
    """`FeedStore.latest`/`window` key `og.feed_obs` by `series` alone, with no `product` filter -- so
    wind and solar (both "system-wide actual + forecast" products) must never share a bare
    `"actual"`/`"forecast"` series name, or a caller reading one product's series through
    `opengrid.feeds`'s fixed `latest()`/`window()` interface would silently also get the other's rows."""
    from opengrid.feeds.normalize import (
        SOLAR_ACTUAL_SERIES,
        SOLAR_FORECAST_SERIES,
        WIND_ACTUAL_SERIES,
        WIND_FORECAST_SERIES,
    )

    names = {WIND_ACTUAL_SERIES, WIND_FORECAST_SERIES, SOLAR_ACTUAL_SERIES, SOLAR_FORECAST_SERIES}
    assert len(names) == 4


def test_nws_missing_periods_raises() -> None:
    with pytest.raises(FeedDataError):
        nws_forecast_to_feed_obs({"properties": {"periods": []}}, recorded_at=RECORDED_AT)


def test_nws_forecast_without_sky_cover_still_returns_temperature_and_dewpoint() -> None:
    """Live defect: a real `/gridpoints/{office}/{x},{y}/forecast/hourly` response (confirmed against
    grid EWX/156,91) never carries `skyCover` on a period at all -- that field only exists on the raw
    `/gridpoints/{office}/{x},{y}` time-series product, a different payload shape entirely. Requiring it
    made every real response raise `FeedDataError`, so og-feeds' NWS feed never recorded a single
    success. `sky_cover` must be optional; temperature/dewpoint (which this endpoint does document)
    must still come through."""
    payload = {
        "properties": {
            "generatedAt": "2026-09-26T17:00:00+00:00",
            "periods": [
                {
                    "number": 1,
                    "name": "",
                    "startTime": "2026-09-26T18:00:00-05:00",
                    "endTime": "2026-09-26T19:00:00-05:00",
                    "isDaytime": False,
                    "temperature": 91,
                    "temperatureUnit": "F",
                    "temperatureTrend": None,
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 0},
                    "dewpoint": {"unitCode": "wmoUnit:degC", "value": 21.1},
                    "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 45},
                    "windSpeed": "5 mph",
                    "windDirection": "SE",
                    "icon": "https://api.weather.gov/icons/land/night/bkn?size=small",
                    "shortForecast": "Mostly Cloudy",
                    "detailedForecast": "",
                }
            ],
        }
    }
    rows = nws_forecast_to_feed_obs(payload, recorded_at=RECORDED_AT)
    assert {r.series for r in rows} == {"temperature", "dewpoint"}
    temp = next(r for r in rows if r.series == "temperature")
    assert temp.value == pytest.approx((91 - 32) * 5 / 9)
