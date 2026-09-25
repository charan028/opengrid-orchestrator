"""TS-08-06 (net margin, hand-computed), TS-08-07 (LP vs rule-baseline), TS-08-08 (forgone upside
from the lock) -- 02a S7.4."""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import PenaltyParams
from opengrid.settle.profitability import (
    compute_forgone_upside,
    compute_penalty,
    compute_pnl,
    compute_value_added_by_lp,
)


def test_penalty_hand_computed_split_across_tolerance_band():
    """shortfall = 20 kWh, committed = 100 kWh, theta = 0.05 -> tolerance band = 5 kWh.
    within band = min(20, 5) = 5 kWh at alpha=0.01 $/kWh = 0.05
    beyond band = 20 - 5 = 15 kWh at beta=0.5 $/kWh = 7.50
    penalty = 0.05 + 7.50 = 7.55
    """
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    amount = compute_penalty(Decimal("20"), Decimal("100"), penalty)
    assert amount == Decimal("7.55")


def test_penalty_zero_when_no_shortfall():
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    assert compute_penalty(Decimal("0"), Decimal("100"), penalty) == Decimal("0")


def test_penalty_zero_when_no_penalty_params():
    assert compute_penalty(Decimal("20"), Decimal("100"), None) == Decimal("0")


def test_pnl_net_margin_hand_computed():
    """TS-08-06 worked example:
    delivered_kwh = 100, price_per_kwh = 0.10, wholesale_price_per_kwh = 0.03, eta_d = 0.5,
    degradation_cost_per_kwh = 0.03, shortfall_kwh = 20, committed_kwh = 100,
    penalty(alpha=0.01, beta=0.5, theta=0.05).

    revenue          = 0.10 * 100                 = 10.00
    energy_cost      = (0.03 / 0.5) * 100 = 0.06*100 = 6.00
    degradation_cost = 0.03 * 100                 = 3.00
    penalty          = 7.55 (see test_penalty_hand_computed_split_across_tolerance_band)
    net_value        = 10.00 - 6.00 - 3.00 - 7.55 = -6.55
    """
    penalty = PenaltyParams(alpha=Decimal("0.01"), beta=Decimal("0.5"), theta=Decimal("0.05"))
    result = compute_pnl(
        delivered_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        wholesale_price_per_kwh=Decimal("0.03"),
        eta_d=Decimal("0.5"),
        degradation_cost_per_kwh=Decimal("0.03"),
        shortfall_kwh=Decimal("20"),
        committed_kwh=Decimal("100"),
        penalty=penalty,
    )
    assert result.revenue == Decimal("10.00")
    assert result.energy_cost == Decimal("6.00")
    assert result.degradation_cost == Decimal("3.00")
    assert result.penalty == Decimal("7.55")
    assert result.net_value == Decimal("-6.55")
    # the invariant the whole module exists to guarantee, to the cent:
    assert result.net_value == result.revenue - result.energy_cost - result.degradation_cost - result.penalty


def test_pnl_no_penalty_configured_and_no_shortfall():
    result = compute_pnl(
        delivered_kwh=Decimal("50"),
        price_per_kwh=Decimal("0.20"),
        wholesale_price_per_kwh=Decimal("0.04"),
        eta_d=Decimal("0.9487"),
        degradation_cost_per_kwh=Decimal("0.03"),
        shortfall_kwh=Decimal("0"),
        committed_kwh=Decimal("50"),
        penalty=None,
    )
    assert result.penalty == Decimal("0")
    assert result.net_value == result.revenue - result.energy_cost - result.degradation_cost


def test_value_added_by_lp_hand_computed():
    """TS-08-07: LP net value 10.00 vs rule-baseline shadow net value 6.00 -> value added = 4.00."""
    assert compute_value_added_by_lp(Decimal("10.00"), Decimal("6.00")) == Decimal("4.00")


def test_value_added_by_lp_none_when_no_shadow_run():
    assert compute_value_added_by_lp(Decimal("10.00"), None) is None


def test_forgone_upside_hand_computed():
    """TS-08-08 worked example: a committed obligation holds 50 kW for a 15-min (0.25 h) interval at
    $0.05/kWh; a rejected candidate call was worth $0.08/kWh on the same capacity.
    forgone_upside = (0.08 - 0.05) * 50 * 0.25 = 0.03 * 12.5 = 0.375
    """
    upside = compute_forgone_upside(
        committed_kw=Decimal("50"),
        duration_hours=Decimal("0.25"),
        obligation_value_per_kwh=Decimal("0.05"),
        best_competing_value_per_kwh=Decimal("0.08"),
    )
    assert upside == Decimal("0.375")


def test_forgone_upside_zero_when_no_competing_candidate():
    upside = compute_forgone_upside(
        committed_kw=Decimal("50"),
        duration_hours=Decimal("0.25"),
        obligation_value_per_kwh=Decimal("0.05"),
        best_competing_value_per_kwh=None,
    )
    assert upside == Decimal("0")


def test_forgone_upside_zero_when_competing_not_more_valuable():
    """The lock only has a measurable cost when the competing call was genuinely worth more --
    never netted, never assumed (review KPI-22)."""
    upside = compute_forgone_upside(
        committed_kw=Decimal("50"),
        duration_hours=Decimal("0.25"),
        obligation_value_per_kwh=Decimal("0.05"),
        best_competing_value_per_kwh=Decimal("0.05"),
    )
    assert upside == Decimal("0")
