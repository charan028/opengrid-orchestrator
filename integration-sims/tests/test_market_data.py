"""Tests for ogsim.market.data.MarketData: synthetic-mode row generation and
anomaly overrides. Uses a fixed `now`, never real wall-clock time.

Field order/names/value types are fixed by interfaces/http/market-api.md §1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ogsim.market.anomalies import Anomaly, AnomalyStore
from ogsim.market.config import MarketConfig
from ogsim.market.data import AS_FIELDS, LOAD_FIELDS, SOLAR_FIELDS, SPP_FIELDS, WIND_FIELDS, MarketData
from ogsim.market.synthetic import HUBS, LOAD_ZONES, WEATHER_ZONES

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
CHICAGO = ZoneInfo("America/Chicago")


def make_data(anomalies: AnomalyStore | None = None) -> MarketData:
    cfg = MarketConfig(data_mode="synthetic", seed=99)
    return MarketData(cfg, anomalies or AnomalyStore())


def test_spp_rows_default_to_load_zone_settlement_points():
    data = make_data()
    rows = data.spp_rows(NOW, hours_back=0.25)
    points = {row[3] for row in rows}
    assert points == set(LOAD_ZONES)


def test_spp_rows_use_hubs_when_settlement_point_type_is_hu():
    data = make_data()
    rows = data.spp_rows(NOW, settlement_point_type="HU", hours_back=0.25)
    points = {row[3] for row in rows}
    assert points == set(HUBS)


def test_spp_rows_filter_to_one_settlement_point_when_requested():
    data = make_data()
    rows = data.spp_rows(NOW, settlement_point="LZ_NORTH", hours_back=0.25)
    assert rows
    assert all(row[3] == "LZ_NORTH" for row in rows)


def test_spp_rows_have_the_seven_documented_columns():
    data = make_data()
    rows = data.spp_rows(NOW, settlement_point="LZ_NORTH", hours_back=0.1)
    assert len(rows[0]) == len(SPP_FIELDS)
    delivery_date, delivery_hour, delivery_interval, settlement_point, spt, price, dst_flag = rows[-1]
    local = NOW.astimezone(CHICAGO)
    assert delivery_date == local.date().isoformat()
    assert delivery_hour == local.hour + 1
    assert 1 <= delivery_interval <= 4
    assert settlement_point == "LZ_NORTH"
    assert spt == "LZ"
    assert isinstance(price, float)
    assert price > 0
    assert isinstance(dst_flag, bool)


def test_price_spike_overrides_the_latest_spp_row():
    anomalies = AnomalyStore()
    anomalies.inject(
        Anomaly(
            id="a1",
            type="price_spike",
            target="np6-905-cd",
            params={"value_usd_per_mwh": 5000.0},
            start=NOW.timestamp() - 1,
            duration=120.0,
        )
    )
    data = make_data(anomalies)
    rows = data.spp_rows(NOW, settlement_point="LZ_NORTH", hours_back=0.25)
    assert rows[-1][5] == 5000.0


def test_negative_price_override_is_applied():
    anomalies = AnomalyStore()
    anomalies.inject(
        Anomaly(
            id="a1",
            type="negative_price",
            target="*",
            params={"value_usd_per_mwh": -75.0},
            start=NOW.timestamp() - 1,
            duration=120.0,
        )
    )
    data = make_data(anomalies)
    rows = data.spp_rows(NOW, settlement_point="LZ_SOUTH", hours_back=0.25)
    assert rows[-1][5] == -75.0


def test_stale_posting_freezes_the_clock_for_the_targeted_product():
    anomalies = AnomalyStore()
    start = NOW.timestamp() - 5.0
    anomalies.inject(
        Anomaly(
            id="a1", type="stale_posting", target="np6-345-cd", params={}, start=start, duration=3 * 3600.0
        )
    )
    data = make_data(anomalies)
    later = NOW + timedelta(hours=2)
    # Both calls fall inside the anomaly's window, so both should be pinned
    # to the same frozen "now" (the anomaly's start) and post the same row.
    rows_now = data.load_rows(NOW, days_back=0.1)
    rows_later = data.load_rows(later, days_back=0.1)
    assert rows_now[-1][:3] == rows_later[-1][:3]


def test_load_rows_are_wide_one_row_per_hour_with_a_column_per_zone():
    data = make_data()
    rows = data.load_rows(NOW, days_back=0.1)
    assert rows
    assert len(rows[0]) == len(LOAD_FIELDS)
    field_names = [name for name, _ in LOAD_FIELDS]
    zone_start = field_names.index(WEATHER_ZONES[0])
    row = rows[-1]
    operating_day, hour_ending, dst_flag = row[0], row[1], row[2]
    local = NOW.astimezone(CHICAGO)
    assert operating_day == local.date().isoformat()
    assert hour_ending == f"{local.hour + 1:02d}:00"
    assert isinstance(dst_flag, bool)
    zone_values = row[zone_start : zone_start + len(WEATHER_ZONES)]
    assert all(isinstance(v, float) for v in zone_values)
    total = row[field_names.index("total")]
    assert total == round(sum(zone_values), 1)


def test_wind_rows_have_the_five_documented_columns():
    data = make_data()
    rows = data.renewable_rows(NOW, "wind", hours_back=2)
    assert rows
    assert len(rows[0]) == len(WIND_FIELDS)
    delivery_date, hour_ending, dst_flag, actual, forecast = rows[-1]
    local = NOW.astimezone(CHICAGO)
    assert delivery_date == local.date().isoformat()
    assert hour_ending == local.hour + 1
    assert isinstance(dst_flag, bool)
    assert actual >= 0
    assert forecast >= 0


def test_solar_rows_have_the_five_documented_columns():
    data = make_data()
    rows = data.renewable_rows(NOW, "solar", hours_back=2)
    assert rows
    assert len(rows[0]) == len(SOLAR_FIELDS)


def test_as_rows_use_market_api_ancillary_type_codes():
    data = make_data()
    rows = data.as_rows(NOW, days_back=0.1)
    assert len(rows[0]) == len(AS_FIELDS)
    services = {row[3] for row in rows}
    assert services == {"REGUP", "REGDN", "RRS", "NSPIN", "ECRS"}


def test_as_rows_hour_ending_is_an_hh_mm_string_with_dst_flag():
    data = make_data()
    rows = data.as_rows(NOW, days_back=0.1)
    local = NOW.astimezone(CHICAGO)
    delivery_date, hour_ending, dst_flag, _service, mcpc = rows[-1]
    assert delivery_date == local.date().isoformat()
    assert hour_ending == f"{local.hour + 1:02d}:00"
    assert isinstance(dst_flag, bool)
    assert isinstance(mcpc, float)


def test_as_price_jump_overrides_only_the_targeted_service():
    anomalies = AnomalyStore()
    anomalies.inject(
        Anomaly(
            id="a1",
            type="as_price_jump",
            target="np4-188-cd",
            params={"service": "RRS", "value_usd_per_mwh": 900.0},
            start=NOW.timestamp() - 1,
            duration=120.0,
        )
    )
    data = make_data(anomalies)
    rows = data.as_rows(NOW, days_back=0.1)
    rrs_prices = [r[4] for r in rows if r[3] == "RRS"]
    regup_prices = [r[4] for r in rows if r[3] == "REGUP"]
    assert 900.0 in rrs_prices
    assert 900.0 not in regup_prices


def test_eia_response_has_one_row_per_hour_of_window_and_string_values():
    data = make_data()
    resp = data.eia_response(NOW)
    assert resp["response"]["total"] == str(len(resp["response"]["data"]))
    row = resp["response"]["data"][0]
    assert row["respondent"] == "ERCO"
    assert isinstance(row["value"], str)


def test_nws_extreme_weather_override_applies_to_every_forecast_period():
    anomalies = AnomalyStore()
    anomalies.inject(
        Anomaly(
            id="a1",
            type="nws_extreme_weather",
            target="nws",
            params={"condition": "Hurricane", "temperature_c": 40.0, "wind_kph": 100.0},
            start=NOW.timestamp() - 1,
            duration=3600.0,
        )
    )
    data = make_data(anomalies)
    forecast = data.nws_hourly_forecast(NOW, "EWX", 156, 91)
    periods = forecast["properties"]["periods"]
    assert all(p["shortForecast"] == "Hurricane" for p in periods)


def test_nws_extreme_weather_override_does_not_change_updated():
    baseline = make_data().nws_updated_at(NOW).isoformat()
    anomalies = AnomalyStore()
    anomalies.inject(
        Anomaly(
            id="a1",
            type="nws_extreme_weather",
            target="nws",
            params={"condition": "Hurricane"},
            start=NOW.timestamp() - 1,
            duration=3600.0,
        )
    )
    data = make_data(anomalies)
    forecast = data.nws_hourly_forecast(NOW, "EWX", 156, 91)
    assert forecast["properties"]["updated"] == baseline


def test_nws_forecast_periods_include_flat_sky_cover():
    data = make_data()
    forecast = data.nws_hourly_forecast(NOW, "EWX", 156, 91)
    period = forecast["properties"]["periods"][0]
    assert isinstance(period["skyCover"], int)
    assert "dewpoint" in period
