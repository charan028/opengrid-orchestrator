"""`charging_cost_from_proxy`/`measured_need_kwh_from_row`: settle's energy-cost basis (09
S0.2 finding G4) and D-18's need-basis site-meter lookup, both derived from a single DB row."""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.pg_backend import (
    as_price_from_mcpc,
    charging_cost_from_proxy,
    measured_need_kwh_from_row,
)


def test_charging_cost_proxy_averages_off_peak_spp_in_dollars_per_kwh():
    row = {"zone": "LZ_NORTH", "avg_value": 82.0, "sample_count": 8}
    assert charging_cost_from_proxy(row) == (Decimal("0.082"), "TRAILING_24H_OFFPEAK_PROXY")


def test_charging_cost_proxy_missing_when_no_off_peak_observation():
    assert charging_cost_from_proxy({"zone": "LZ_WEST", "avg_value": None, "sample_count": 0}) == (
        Decimal("0"),
        "MISSING",
    )
    assert charging_cost_from_proxy(None) == (Decimal("0"), "MISSING")


def test_measured_need_kwh_is_average_site_kw_times_duration():
    """avg site import 4 kW over a 0.25h (15-min) interval -> 1.0 kWh of measured need."""
    row = {"avg_kw": 4.0, "sample_count": 15}
    assert measured_need_kwh_from_row(row, Decimal("0.25")) == Decimal("1.0")


def test_measured_need_kwh_is_none_when_no_reading():
    assert measured_need_kwh_from_row({"avg_kw": None, "sample_count": 0}, Decimal("0.25")) is None
    assert measured_need_kwh_from_row(None, Decimal("0.25")) is None


def test_as_price_is_the_cleared_mcpc_else_the_opportunity_flagged() -> None:
    fallback = Decimal("0.001")
    assert as_price_from_mcpc({"product_code": "ECRS", "value": 0.28}, fallback) == (
        Decimal("0.00028"),
        "MCPC",
    )
    assert as_price_from_mcpc({"product_code": "ECRS", "value": None}, fallback) == (
        fallback,
        "OPPORTUNITY_PRICE",
    )
    assert as_price_from_mcpc(None, fallback) == (fallback, "OPPORTUNITY_PRICE")
