"""TS-08-06 (net margin, hand-computed), TS-08-07 (LP vs rule-baseline), TS-08-08 (forgone upside
from the lock) -- 02a S7.4."""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import PenaltyParams
from opengrid.settle.profitability import (
    compute_forgone_upside,
    compute_penalty,
    compute_pnl,
    compute_revenue,
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
    """TS-08-06 worked example (DIST_DEFERRAL: the generic price_per_kwh * delivered_kwh revenue
    branch, not one of the service-specific ones):
    delivered_kwh = 100, price_per_kwh = 0.10, charging_cost_per_kwh = 0.03, eta_d = 0.5,
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
        service_type="DIST_DEFERRAL",
        delivered_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        charging_cost_per_kwh=Decimal("0.03"),
        discharge_spp_per_kwh=Decimal("0"),
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
        service_type="DIST_DEFERRAL",
        delivered_kwh=Decimal("50"),
        price_per_kwh=Decimal("0.20"),
        charging_cost_per_kwh=Decimal("0.04"),
        discharge_spp_per_kwh=Decimal("0"),
        eta_d=Decimal("0.9487"),
        degradation_cost_per_kwh=Decimal("0.03"),
        shortfall_kwh=Decimal("0"),
        committed_kwh=Decimal("50"),
        penalty=None,
    )
    assert result.penalty == Decimal("0")
    assert result.net_value == result.revenue - result.energy_cost - result.degradation_cost


# -- ERCOT_AS revenue: capacity held, not energy delivered (2026-09-26 live-soak correction) --------


def test_ercot_as_revenue_is_mcpc_times_committed_capacity_held():
    """Realistic NSPIN MCPC ~$8.50/MW-h (NPRR1282-era range): a 500 kW award held for a full 15-min
    (0.25 h) interval, never deployed (delivered_kwh = 0).
    committed_kwh = 500 kW * 0.25 h = 125 kWh; price_per_kwh = $8.50/MWh / 1000 = $0.0085/kWh.
    revenue = 125 * 0.0085 + 0 (nothing deployed) = 1.0625, not 0 (delivered_kwh * price_per_kwh)."""
    revenue = compute_revenue(
        service_type="ERCOT_AS",
        delivered_kwh=Decimal("0"),
        committed_kwh=Decimal("125"),
        price_per_kwh=Decimal("0.0085"),
        discharge_spp_per_kwh=Decimal("0.030"),
    )
    assert revenue == Decimal("1.0625")


def test_ercot_as_revenue_adds_energy_value_only_for_kwh_actually_deployed():
    """Same 500 kW/125 kWh NSPIN award, but ERCOT calls a deployment and 20 kWh is actually
    discharged this interval at a realistic real-time SPP of $0.045/kWh ($45/MWh).
    revenue = capacity (125 * 0.0085 = 1.0625) + energy (20 * 0.045 = 0.90) = 1.9625."""
    revenue = compute_revenue(
        service_type="ERCOT_AS",
        delivered_kwh=Decimal("20"),
        committed_kwh=Decimal("125"),
        price_per_kwh=Decimal("0.0085"),
        discharge_spp_per_kwh=Decimal("0.045"),
    )
    assert revenue == Decimal("1.9625")


def test_generic_service_revenue_is_price_times_delivered():
    assert compute_revenue(
        service_type="DIST_DEFERRAL",
        delivered_kwh=Decimal("100"),
        committed_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),
        discharge_spp_per_kwh=Decimal("0.999"),  # must be ignored for a generic service
    ) == Decimal("10.00")


# -- ERCOT_ENERGY revenue: the zone's real-time SPP at delivery (2026-09-26 live P&L review) ---------


def test_ercot_energy_revenue_is_delivered_kwh_times_zone_spp():
    """Live bug: ERCOT_ENERGY revenue was `price_per_kwh * delivered_kwh`, and `price_per_kwh` (the
    opportunity's `value_per_mwh`) is null for many admission paths -- revenue settled at $0 despite
    real delivered energy and a live SPP feed. 02a S7.1's own baseline is "ISO_SETTLEMENT_SHADOW"
    against the simulated real-time price: revenue must be delivered_kwh * the zone's SPP, not the
    stale/absent forward price."""
    revenue = compute_revenue(
        service_type="ERCOT_ENERGY",
        delivered_kwh=Decimal("100"),
        committed_kwh=Decimal("100"),
        price_per_kwh=Decimal("0"),  # null opportunity value_per_mwh -- must NOT zero out revenue
        discharge_spp_per_kwh=Decimal("0.045"),  # $45/MWh real-time SPP
    )
    assert revenue == Decimal("4.50")


def test_ercot_energy_revenue_is_zero_only_when_spp_is_genuinely_missing():
    revenue = compute_revenue(
        service_type="ERCOT_ENERGY",
        delivered_kwh=Decimal("100"),
        committed_kwh=Decimal("100"),
        price_per_kwh=Decimal("0.10"),  # ignored even when present
        discharge_spp_per_kwh=Decimal("0"),  # MISSING flag -- no SPP within 1h
    )
    assert revenue == Decimal("0")


def test_ercot_as_pnl_held_not_deployed_has_no_shortfall_penalty_and_wear_only_on_discharge():
    """Full pnl worked example for a held-but-not-deployed AS interval: revenue is the capacity
    payment only, energy_cost and degradation are both zero (delivered_kwh = 0, so no discharge to
    price or wear), and shortfall_kwh = 0 (the caller, opengrid.settle, never derives a shortfall
    from AS non-discharge -- this test exercises compute_pnl with that same shortfall_kwh=0 input)."""
    result = compute_pnl(
        service_type="ERCOT_AS",
        delivered_kwh=Decimal("0"),
        price_per_kwh=Decimal("0.0085"),
        charging_cost_per_kwh=Decimal("0.06"),
        discharge_spp_per_kwh=Decimal("0.030"),
        eta_d=Decimal("0.9487"),
        degradation_cost_per_kwh=Decimal("0.03"),
        shortfall_kwh=Decimal("0"),
        committed_kwh=Decimal("125"),
        penalty=PenaltyParams(alpha=Decimal("0.02"), beta=Decimal("0.30"), theta=Decimal("0.10")),
    )
    assert result.revenue == Decimal("1.0625")
    assert result.energy_cost == Decimal("0")
    assert result.degradation_cost == Decimal("0")
    assert result.penalty == Decimal("0")
    assert result.net_value == Decimal("1.0625")


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
