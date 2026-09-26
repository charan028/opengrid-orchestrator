"""Charging-cost model (08 S3b, 09 D4/D5, TS-19-08/09) and the regulated capacity payment."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from opengrid.market.capacity import annual_capacity_price, regulated_capacity_payment
from opengrid.market.charging import blend, regulated_charging_cost, tou_period, utility_grid_rate
from opengrid.market.config import AUSTIN_ENERGY, CPS_ENERGY
from opengrid.market.free_charging import free_charging_cost
from opengrid.settle.tariffs import TdspTariff

CT = ZoneInfo("America/Chicago")
ONCOR = TdspTariff("ONCOR", date(2026, 9, 1), Decimal("0.060295"), ("LZ_NORTH",))


@pytest.mark.parametrize(
    ("local", "period"),
    [
        (datetime(2026, 9, 28, 2, 0, tzinfo=CT), "OFF_PEAK"),  # Monday night
        (datetime(2026, 9, 28, 22, 0, tzinfo=CT), "OFF_PEAK"),
        (datetime(2026, 9, 28, 7, 0, tzinfo=CT), "MID_PEAK"),
        (datetime(2026, 9, 28, 14, 45, tzinfo=CT), "MID_PEAK"),
        (datetime(2026, 9, 28, 15, 0, tzinfo=CT), "ON_PEAK"),
        (datetime(2026, 9, 28, 17, 45, tzinfo=CT), "ON_PEAK"),
        (datetime(2026, 9, 28, 18, 0, tzinfo=CT), "MID_PEAK"),
        (datetime(2026, 9, 26, 16, 0, tzinfo=CT), "OFF_PEAK"),  # Saturday: all day off-peak
    ],
)
def test_ae_tou_periods(local: datetime, period: str) -> None:
    assert tou_period(AUSTIN_ENERGY, local) == period
    assert tou_period(AUSTIN_ENERGY, local.astimezone(UTC)) == period


def test_night_rate_utility_periods() -> None:
    assert tou_period(CPS_ENERGY, datetime(2026, 9, 28, 23, 0, tzinfo=CT)) == "NIGHT"
    assert tou_period(CPS_ENERGY, datetime(2026, 9, 28, 12, 0, tzinfo=CT)) == "DAY"
    # CPS publishes no day rate: never a cheaper guess than the known rate.
    assert utility_grid_rate(CPS_ENERGY, "DAY") == CPS_ENERGY.off_peak_rate_usd_per_kwh


def test_ae_night_charging_matches_08_s3c() -> None:
    """TS-19-09: 30% solar at 4.0 cents + 70% AE off-peak at 2.677 cents = 3.0739 cents; no M1."""
    cost = regulated_charging_cost(AUSTIN_ENERGY, "LZ_AEN", datetime(2026, 9, 28, 3, 0, tzinfo=CT))
    assert cost.market == "REGULATED"
    assert cost.utility_id == "AUSTIN_ENERGY"
    assert cost.period == "OFF_PEAK"
    assert cost.delivery_usd_per_kwh == 0
    assert cost.grid_energy_usd_per_kwh == Decimal("0.02677")
    assert cost.blended_usd_per_kwh == Decimal("0.0307390")


def test_ae_on_peak_charging_is_priced_on_peak() -> None:
    cost = regulated_charging_cost(AUSTIN_ENERGY, "LZ_AEN", datetime(2026, 9, 28, 16, 0, tzinfo=CT))
    assert cost.grid_energy_usd_per_kwh == Decimal("0.08442")


def test_more_solar_than_the_floor_is_costed() -> None:
    cost = regulated_charging_cost(
        AUSTIN_ENERGY, "LZ_AEN", datetime(2026, 9, 28, 3, 0, tzinfo=CT), solar_share=Decimal("0.5")
    )
    assert cost.blended_usd_per_kwh == Decimal("0.5") * Decimal("0.040") + Decimal("0.5") * Decimal("0.02677")


def test_free_charging_adds_m1_to_grid_kwh_only() -> None:
    """TS-19-08: FREE grid kWh cost lambda + w_tau; BTM solar pays no w."""
    cost = free_charging_cost("LZ_NORTH", wholesale_usd_per_kwh=Decimal("0.025"), tdsp_tariff=ONCOR)
    assert cost.delivery_usd_per_kwh == Decimal("0.060295")
    assert cost.grid_all_in_usd_per_kwh == Decimal("0.085295")
    assert cost.blended_usd_per_kwh == Decimal("0.085295")
    solar_half = free_charging_cost(
        "LZ_NORTH", wholesale_usd_per_kwh=Decimal("0.025"), tdsp_tariff=ONCOR, solar_share=Decimal("0.5")
    )
    assert solar_half.blended_usd_per_kwh == Decimal("0.5") * Decimal("0.025") + Decimal("0.5") * Decimal(
        "0.085295"
    )


def test_free_charging_unmapped_zone_has_no_m1() -> None:
    cost = free_charging_cost("LZ_X", wholesale_usd_per_kwh=Decimal("0.03"), tdsp_tariff=None)
    assert cost.delivery_usd_per_kwh == 0
    assert cost.tariff_ref.endswith("M1-NONE")


def test_blend_rejects_share_outside_unit_interval() -> None:
    with pytest.raises(ValueError, match="solar_share"):
        blend(Decimal("1.1"), Decimal("0.04"), Decimal("0.03"))


def test_capacity_payment_per_kw_year() -> None:
    """$75/kW-yr on 2 MW for a full year is $150,000; for a 24 h gate it is 1/365 of that."""
    year = regulated_capacity_payment(
        committed_kw=Decimal("2000"),
        price_usd_per_kw=Decimal("75"),
        basis="USD_PER_KW_YEAR",
        hours=Decimal("8760"),
    )
    assert year == Decimal("150000")
    day = regulated_capacity_payment(
        committed_kw=Decimal("2000"),
        price_usd_per_kw=Decimal("75"),
        basis="USD_PER_KW_YEAR",
        hours=Decimal("24"),
    )
    assert day == Decimal("150000") * Decimal("24") / Decimal("8760")


def test_capacity_payment_per_kw_month_equals_twelve_months() -> None:
    assert annual_capacity_price(Decimal("6.25"), "USD_PER_KW_MONTH") == Decimal("75.00")
    month = regulated_capacity_payment(
        committed_kw=Decimal("100"),
        price_usd_per_kw=Decimal("6.25"),
        basis="USD_PER_KW_MONTH",
        hours=Decimal("730"),
    )
    assert month == Decimal("625")


def test_capacity_payment_performance_factor_is_clamped() -> None:
    kwargs = {
        "committed_kw": Decimal("100"),
        "price_usd_per_kw": Decimal("75"),
        "basis": "USD_PER_KW_YEAR",
        "hours": Decimal("8760"),
    }
    assert regulated_capacity_payment(**kwargs, performance_factor=Decimal("0.8")) == Decimal("6000")  # type: ignore[arg-type]
    assert regulated_capacity_payment(**kwargs, performance_factor=Decimal("1.4")) == Decimal("7500")  # type: ignore[arg-type]
    assert regulated_capacity_payment(**kwargs, performance_factor=Decimal("-1")) == 0  # type: ignore[arg-type]


def test_capacity_payment_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError):
        regulated_capacity_payment(
            committed_kw=Decimal("-1"),
            price_usd_per_kw=Decimal("75"),
            basis="USD_PER_KW_YEAR",
            hours=Decimal("1"),
        )
    with pytest.raises(ValueError):
        annual_capacity_price(Decimal("-1"), "USD_PER_KW_YEAR")
