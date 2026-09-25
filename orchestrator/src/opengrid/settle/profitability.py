"""Profitability formulas (02a S7.4): per-obligation-interval net value, the LP-vs-rule-baseline
comparison, and the forgone upside attributable to the commitment lock (K13, review KPI-22).

    net_value = revenue - energy_cost - degradation_cost - penalty
    revenue         = price_per_kwh * delivered_kwh
    energy_cost     = (wholesale_price_per_kwh / eta_d) * delivered_kwh
    degradation_cost = degradation_cost_per_kwh * delivered_kwh
    penalty         = Pen_o(shortfall_kwh)   -- convex piecewise-linear, slope alpha inside the
                      tolerance band (theta * committed_kwh), slope beta beyond it (beta >> alpha)

These are pure functions -- no I/O, no rounding beyond what the caller applies before persisting to
`numeric(18,6)` columns, so tests can hand-compute exact Decimal results.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import PenaltyParams, PnlBreakdown

_ZERO = Decimal("0")


def compute_penalty(shortfall_kwh: Decimal, committed_kwh: Decimal, penalty: PenaltyParams | None) -> Decimal:
    """Pen_o(z): alpha $/kWh inside the tolerance band (`theta * committed_kwh`), beta $/kWh beyond
    it. No penalty terms configured, no shortfall, or no committed capacity -> zero penalty."""
    if penalty is None or shortfall_kwh <= 0 or committed_kwh <= 0:
        return _ZERO
    tolerance_kwh = penalty.theta * committed_kwh
    within_band = min(shortfall_kwh, tolerance_kwh)
    beyond_band = max(_ZERO, shortfall_kwh - tolerance_kwh)
    return penalty.alpha * within_band + penalty.beta * beyond_band


def compute_pnl(
    *,
    delivered_kwh: Decimal,
    price_per_kwh: Decimal,
    wholesale_price_per_kwh: Decimal,
    eta_d: Decimal,
    degradation_cost_per_kwh: Decimal,
    shortfall_kwh: Decimal,
    committed_kwh: Decimal,
    penalty: PenaltyParams | None,
) -> PnlBreakdown:
    """02a S7.4's four-term decomposition for one obligation-interval, written to `og.pnl`."""
    revenue = price_per_kwh * delivered_kwh
    energy_cost = (wholesale_price_per_kwh / eta_d) * delivered_kwh if eta_d != 0 else _ZERO
    degradation_cost = degradation_cost_per_kwh * delivered_kwh
    penalty_amount = compute_penalty(shortfall_kwh, committed_kwh, penalty)
    net_value = revenue - energy_cost - degradation_cost - penalty_amount
    return PnlBreakdown(
        revenue=revenue,
        energy_cost=energy_cost,
        degradation_cost=degradation_cost,
        penalty=penalty_amount,
        net_value=net_value,
    )


def compute_forgone_upside(
    *,
    committed_kw: Decimal,
    duration_hours: Decimal,
    obligation_value_per_kwh: Decimal,
    best_competing_value_per_kwh: Decimal | None,
) -> Decimal:
    """02a S7.4's forgone-upside figure (review KPI-22): the value a strictly higher-value, newer
    candidate opportunity could have realized on this obligation's locked capacity this interval, had
    the commitment lock (K13) not held it. Zero when no competing candidate exists or it is not worth
    more than the obligation actually holding the capacity -- never netted against `net_value`, always
    reported as its own `pnl.forgone_upside` figure."""
    if best_competing_value_per_kwh is None or best_competing_value_per_kwh <= obligation_value_per_kwh:
        return _ZERO
    value_gap_per_kwh = best_competing_value_per_kwh - obligation_value_per_kwh
    return value_gap_per_kwh * committed_kw * duration_hours


def compute_value_added_by_lp(net_value: Decimal, rule_baseline_value: Decimal | None) -> Decimal | None:
    """UI's Profitability screen figure: "value added by the LP" = net_value - rule_baseline_value
    (02a S7.4). `None` when no rule-baseline shadow run is available for this interval."""
    if rule_baseline_value is None:
        return None
    return net_value - rule_baseline_value
