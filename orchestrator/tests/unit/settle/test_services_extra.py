"""Tests for `opengrid.settle.services_extra` (PJM_CAPACITY/MOBILE_STORAGE/LARGE_LOAD settlement math,
owner decision 2026-09-26). Pure-function tests only, matching the style of the sibling `settle` test
modules (`test_performance.py`, `test_baselines.py`)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.settle.services_extra import (
    EXTRA_METER_SOURCE_BY_SERVICE,
    large_load_curtailment_compliance_pct,
    mobile_deployment_availability_pct,
    pjm_non_performance_charge,
)


def test_extra_meter_source_covers_exactly_the_three_new_service_types() -> None:
    assert set(EXTRA_METER_SOURCE_BY_SERVICE) == {"PJM_CAPACITY", "MOBILE_STORAGE", "LARGE_LOAD"}


def test_extra_meter_source_uses_valid_meter_source_values() -> None:
    assert EXTRA_METER_SOURCE_BY_SERVICE["PJM_CAPACITY"] == "DIRECT_HUB_METER"
    assert EXTRA_METER_SOURCE_BY_SERVICE["MOBILE_STORAGE"] == "AMI_INTERVAL"
    assert EXTRA_METER_SOURCE_BY_SERVICE["LARGE_LOAD"] == "DIRECT_HUB_METER"


# =========================================================================================================
# pjm_non_performance_charge
# =========================================================================================================


def test_pjm_no_charge_outside_a_declared_emergency_performance_hour() -> None:
    charge = pjm_non_performance_charge(
        committed_kw=Decimal("1000"),
        delivered_kw=Decimal("0"),
        duration_hours=Decimal("1"),
        non_performance_rate_per_kwh=Decimal("5"),
        is_emergency_performance_hour=False,
    )
    assert charge == Decimal("0")


def test_pjm_no_charge_when_fully_delivered_during_emergency_hour() -> None:
    charge = pjm_non_performance_charge(
        committed_kw=Decimal("1000"),
        delivered_kw=Decimal("1000"),
        duration_hours=Decimal("1"),
        non_performance_rate_per_kwh=Decimal("5"),
        is_emergency_performance_hour=True,
    )
    assert charge == Decimal("0")


def test_pjm_charges_shortfall_at_the_configured_rate_during_emergency_hour() -> None:
    charge = pjm_non_performance_charge(
        committed_kw=Decimal("1000"),
        delivered_kw=Decimal("600"),
        duration_hours=Decimal("1"),
        non_performance_rate_per_kwh=Decimal("5"),
        is_emergency_performance_hour=True,
    )
    # shortfall = (1000 - 600) kWh = 400 kWh, x $5/kWh = $2000
    assert charge == Decimal("2000")


def test_pjm_charge_is_never_negative_on_overdelivery() -> None:
    charge = pjm_non_performance_charge(
        committed_kw=Decimal("1000"),
        delivered_kw=Decimal("1200"),
        duration_hours=Decimal("1"),
        non_performance_rate_per_kwh=Decimal("5"),
        is_emergency_performance_hour=True,
    )
    assert charge == Decimal("0")


@given(
    committed_kw=st.decimals(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False),
    delivered_kw=st.decimals(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False),
    rate=st.decimals(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
)
def test_pjm_charge_never_negative_property(
    committed_kw: Decimal, delivered_kw: Decimal, rate: Decimal
) -> None:
    charge = pjm_non_performance_charge(
        committed_kw=committed_kw,
        delivered_kw=delivered_kw,
        duration_hours=Decimal("1"),
        non_performance_rate_per_kwh=rate,
        is_emergency_performance_hour=True,
    )
    assert charge >= Decimal("0")


# =========================================================================================================
# mobile_deployment_availability_pct
# =========================================================================================================


def test_mobile_availability_full_window_energized() -> None:
    assert mobile_deployment_availability_pct(120, 120) == Decimal("1")


def test_mobile_availability_partial_window() -> None:
    assert mobile_deployment_availability_pct(60, 120) == Decimal("0.5")


def test_mobile_availability_zero_window_is_none() -> None:
    assert mobile_deployment_availability_pct(0, 0) is None


def test_mobile_availability_never_exceeds_one() -> None:
    assert mobile_deployment_availability_pct(150, 120) == Decimal("1")


def test_mobile_availability_never_negative() -> None:
    assert mobile_deployment_availability_pct(-10, 120) == Decimal("0")


# =========================================================================================================
# large_load_curtailment_compliance_pct
# =========================================================================================================


def test_large_load_compliance_full_delivery() -> None:
    assert large_load_curtailment_compliance_pct(Decimal("500"), Decimal("500")) == Decimal("1")


def test_large_load_compliance_partial_delivery() -> None:
    assert large_load_curtailment_compliance_pct(Decimal("250"), Decimal("500")) == Decimal("0.5")


def test_large_load_compliance_no_schedule_is_none() -> None:
    assert large_load_curtailment_compliance_pct(Decimal("0"), Decimal("0")) is None


@pytest.mark.parametrize("delivered", [Decimal("0"), Decimal("-5")])
def test_large_load_compliance_never_negative(delivered: Decimal) -> None:
    result = large_load_curtailment_compliance_pct(delivered, Decimal("500"))
    assert result is not None
    assert result >= Decimal("0")
