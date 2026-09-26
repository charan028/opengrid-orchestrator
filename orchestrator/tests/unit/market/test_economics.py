"""$/kW economics (08 S3b/S3c, 09 S4; TS-19-18/19)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from opengrid.market.economics import (
    PeriodTotals,
    UnitEconomicsInputs,
    discounted_payback_years,
    illustrative_home_unit,
    illustrative_home_unit_inputs,
    npv,
    period_kw_economics,
    rollup,
    stored_energy_avg_cost,
    unit_economics,
    unit_totals,
)


def _approx(value: Decimal | None, expected: str, tol: str = "0.01") -> bool:
    return value is not None and abs(value - Decimal(expected)) <= Decimal(tol)


def test_reproduces_08_s3c_table() -> None:
    """TS-19-19: the S3c single-unit stack -- capacity 825, charging -361, energy +1,364 (35.3 kWh),
    O&M -210, net ~ 1,620/yr, payback ~ 4.3 years before scarcity upside."""
    totals = unit_totals(illustrative_home_unit_inputs())
    assert totals.capacity_revenue_usd == Decimal("825")
    assert _approx(totals.charging_energy_usd, "361.49")
    assert _approx(totals.energy_revenue_usd, "1363.22")
    assert totals.om_usd == Decimal("210.00")
    econ = illustrative_home_unit()
    assert _approx(econ.net_usd_per_yr, "1616.73")
    assert abs(econ.net_usd_per_yr - Decimal("1620")) < Decimal("5")
    assert _approx(econ.payback_years, "4.33")
    assert econ.meets_target is False  # 3-year target not met before scarcity upside
    assert _approx(econ.capex_usd_per_kw, "636.36")
    assert _approx(econ.in_usd_per_kw_yr, "32.86")
    assert _approx(econ.out_usd_per_kw_yr, "198.93")
    assert _approx(econ.net_usd_per_kw_yr, "146.98")


def test_about_700_more_per_year_reaches_three_years() -> None:
    """08 S3c: ~3 years needs roughly $700/yr more per unit (scarcity and DR on headroom)."""
    base = unit_totals(illustrative_home_unit_inputs())
    econ = period_kw_economics(replace(base, other_revenue_usd=Decimal("720")))
    assert econ.payback_years is not None and econ.payback_years <= Decimal("3")
    assert econ.meets_target is True


def test_free_market_adds_m1_to_cost_in() -> None:
    inputs = UnitEconomicsInputs(
        kw=Decimal("11"),
        capex_usd=Decimal("7000"),
        charged_kwh_per_cycle=Decimal("39.2"),
        eta_rt=Decimal("0.9"),
        cycles_per_year=Decimal("300"),
        capacity_price_usd_per_kw_yr=Decimal("0"),
        energy_value_usd_per_kwh=Decimal("0.10"),
        charging_cost_usd_per_kwh=Decimal("0.03"),
        delivery_charge_usd_per_kwh=Decimal("0.060295"),
        market="FREE",
    )
    econ = unit_economics(inputs)
    kwh_in = Decimal("39.2") * 300
    assert econ.delivery_charge_usd_per_yr == kwh_in * Decimal("0.060295")
    assert econ.cost_in_usd_per_yr == kwh_in * (Decimal("0.03") + Decimal("0.060295"))


def test_wear_uses_core_wear_cost_on_kwh_out() -> None:
    inputs = UnitEconomicsInputs(
        kw=Decimal("11"),
        capex_usd=Decimal("7000"),
        charged_kwh_per_cycle=Decimal("39.2"),
        eta_rt=Decimal("0.9"),
        cycles_per_year=Decimal("300"),
        capacity_price_usd_per_kw_yr=Decimal("0"),
        energy_value_usd_per_kwh=Decimal("0"),
        charging_cost_usd_per_kwh=Decimal("0"),
        wear_rate_usd_per_kwh=Decimal("0.03"),
    )
    # 09 S1.7's consistency check: $0.03/kWh on 35.3 kWh x 300 days is ~ $318/yr.
    assert _approx(unit_economics(inputs).wear_usd_per_yr, "317.52")


def test_period_totals_annualise_a_month() -> None:
    month = PeriodTotals(
        scope_kind="CONTRACT",
        scope_ref="c1",
        market="REGULATED",
        kw_basis=Decimal("2000"),
        hours=Decimal("730"),
        charging_energy_usd=Decimal("1000"),
        capacity_revenue_usd=Decimal("12500"),
        penalty_usd=Decimal("500"),
        wear_usd=Decimal("100"),
        om_usd=Decimal("400"),
        capex_usd=Decimal("1272727.27"),
        incentives_usd=Decimal("272727.27"),
    )
    econ = period_kw_economics(month)
    assert econ.in_usd_per_kw_yr == Decimal("1000") * 12 / 2000
    assert econ.out_usd_per_kw_yr == Decimal("12000") * 12 / 2000
    assert econ.net_usd_per_yr == Decimal("10500") * 12
    assert _approx(econ.payback_years, str(Decimal("1272727.27") / Decimal("126000")))
    assert _approx(econ.effective_payback_years, str(Decimal("1000000") / Decimal("126000")))
    assert econ.effective_investment_usd_per_kw == Decimal("500")


def test_non_positive_net_has_no_payback() -> None:
    econ = period_kw_economics(
        PeriodTotals(
            scope_kind="CONTRACT",
            scope_ref="c",
            market="FREE",
            kw_basis=Decimal("10"),
            hours=Decimal("8760"),
            charging_energy_usd=Decimal("100"),
            capex_usd=Decimal("1000"),
        )
    )
    assert econ.payback_years is None
    assert econ.discounted_payback_years is None
    assert econ.meets_target is False


def test_zero_kw_and_zero_capex_are_reported_not_divided() -> None:
    econ = period_kw_economics(
        PeriodTotals(
            scope_kind="MARKET", scope_ref="m", market="FREE", kw_basis=Decimal("0"), hours=Decimal("24")
        )
    )
    assert econ.in_usd_per_kw_yr is None
    assert econ.meets_target is None


def test_period_hours_must_be_positive() -> None:
    with pytest.raises(ValueError):
        period_kw_economics(
            PeriodTotals(
                scope_kind="FLEET", scope_ref="f", market=None, kw_basis=Decimal("1"), hours=Decimal("0")
            )
        )


def test_discounted_payback_and_npv() -> None:
    assert discounted_payback_years(Decimal("1000"), Decimal("300"), Decimal("0")) == 4
    assert discounted_payback_years(Decimal("1000"), Decimal("300"), Decimal("0.10")) == 5
    assert discounted_payback_years(Decimal("0"), Decimal("1"), Decimal("0")) == 0
    assert discounted_payback_years(Decimal("1000"), Decimal("1"), Decimal("0")) is None
    assert npv(Decimal("1000"), Decimal("300"), Decimal("0"), 5) == Decimal("500")


def test_rollup_sums_and_rejects_mixed_periods() -> None:
    a = PeriodTotals(
        scope_kind="CONTRACT", scope_ref="a", market="REGULATED", kw_basis=Decimal("100"), hours=Decimal("730"),
        capacity_revenue_usd=Decimal("600"), capex_usd=Decimal("10"),
    )  # fmt: skip
    b = PeriodTotals(
        scope_kind="CONTRACT", scope_ref="b", market="REGULATED", kw_basis=Decimal("300"), hours=Decimal("730"),
        capacity_revenue_usd=Decimal("1800"), capex_usd=Decimal("30"),
    )  # fmt: skip
    total = rollup([a, b], scope_kind="MARKET", scope_ref="REGULATED", market="REGULATED")
    assert total.kw_basis == Decimal("400")
    assert total.capacity_revenue_usd == Decimal("2400")
    assert total.capex_usd == Decimal("40")
    assert period_kw_economics(total).out_usd_per_kw_yr == Decimal("2400") * 12 / 400
    empty = rollup([], scope_kind="FLEET", scope_ref="f", market=None)
    assert empty.kw_basis == 0
    with pytest.raises(ValueError):
        rollup([a, PeriodTotals(scope_kind="CONTRACT", scope_ref="c", market=None, kw_basis=1, hours=Decimal("24"))],  # type: ignore[arg-type]
               scope_kind="FLEET", scope_ref="f", market=None)  # fmt: skip


def test_stored_energy_average_cost() -> None:
    """TS-19-18: c_bar = C_in / (E_dis + eta_d (e_end - e_start))."""
    assert stored_energy_avg_cost(
        charging_cost_usd=Decimal("30"), discharged_kwh=Decimal("90"), eta_d=Decimal("0.95"),
        energy_start_kwh=Decimal("10"), energy_end_kwh=Decimal("20"),
    ) == Decimal("30") / (Decimal("90") + Decimal("9.5"))  # fmt: skip
    assert stored_energy_avg_cost(
        charging_cost_usd=Decimal("30"), discharged_kwh=Decimal("0"), eta_d=Decimal("0.95"),
        energy_start_kwh=Decimal("10"), energy_end_kwh=Decimal("10"),
    ) is None  # fmt: skip
