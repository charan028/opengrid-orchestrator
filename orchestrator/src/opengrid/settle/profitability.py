"""Profitability formulas (02a S7.4): per-obligation-interval net value, the LP-vs-rule-baseline
comparison, and the forgone upside attributable to the commitment lock (K13, review KPI-22).

    net_value = revenue - energy_cost - degradation_cost - penalty - delivery_charge
    revenue         = compute_revenue(...)  -- delivered_kwh * the zone's real-time SPP for
                      ERCOT_ENERGY (2026-09-26 live P&L review: the opportunity's `value_per_mwh` is a
                      forward/planned bid, often null, and 02a S7.1's baseline is explicitly
                      "ISO_SETTLEMENT_SHADOW" against the simulated real-time price, not a fixed
                      contract rate); `committed_kwh * MCPC` for ERCOT_AS (see compute_revenue's
                      docstring: an AS award pays for capacity held, not energy delivered); the
                      contract price for everything else
    energy_cost     = (charging_cost_per_kwh / eta_d) * delivered_kwh
    degradation_cost = wear_cost(delivered_kwh, degradation_cost_per_kwh)  -- opengrid.core.economics
    penalty         = Pen_o(shortfall_kwh)   -- convex piecewise-linear, slope alpha inside the
                      tolerance band (theta * committed_kwh), slope beta beyond it (beta >> alpha)
    delivery_charge = opengrid.settle.tariffs.m1_delivery_charge(...) -- the caller resolves the TDSP
                      tariff and passes the already-computed dollar figure in (09 D5); zero for a
                      regulated-territory asset or behind-the-meter solar charging

`charging_cost_per_kwh` (09-optimizer-dispatcher-update.md S0.2 finding G4, S4): what was actually
PAID to charge the energy now being discharged, never the discharge-interval wholesale price. G4's
bug was pricing energy at the discharge-interval SPP / eta_d, which overstates the cost of an evening
delivery charged overnight at a much lower price and has no relationship to M1/regulated tariffs.
`opengrid.settle.pg_backend.charging_cost_from_proxy` resolves this per obligation-interval: the
obligation's own charging intervals where known (not yet implemented -- MVP-S has no per-obligation
charge attribution), else the documented proxy, the trailing 24h off-peak SPP average for the
obligation's bank zone.

These are pure functions -- no I/O, no rounding beyond what the caller applies before persisting to
`numeric(18,6)` columns, so tests can hand-compute exact Decimal results.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.economics import wear_cost
from opengrid.core.models.engine import ServiceType
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


def compute_revenue(
    *,
    service_type: ServiceType,
    delivered_kwh: Decimal,
    committed_kwh: Decimal,
    price_per_kwh: Decimal,
    discharge_spp_per_kwh: Decimal,
    regulated_capacity_amount: Decimal | None = None,
) -> Decimal:
    """02a S7.4 revenue -- service-specific for `ERCOT_AS` (2026-09-26 live-soak correction): an AS
    award pays for the CAPACITY HELD, not for energy delivered, so its revenue is `committed_kwh *
    price_per_kwh` (award x MCPC, `price_per_kwh` already carries the opportunity's MCPC per
    `ObligationSettlementContext.price_per_kwh`) PLUS energy value only for kWh actually deployed
    (`delivered_kwh * discharge_spp_per_kwh`, the discharge-interval SPP -- an AS award earns no
    energy revenue while merely held, never called). Every other service still prices delivered kWh
    at its own contract price (`price_per_kwh * delivered_kwh`, unchanged).

    `REGULATED_CAPACITY` revenue is `regulated_capacity_amount` verbatim -- the caller
    (`opengrid.settle.__init__`) resolves it via `opengrid.market.capacity.regulated_capacity_payment`
    against the contract's `og.utility` terms, since this function has no access to the utility's
    $/kW-month/year rate or payment basis. Zero (not a guess) when the caller has no rate to resolve
    (`regulated_capacity_amount is None`).

    `ERCOT_ENERGY` revenue is `delivered_kwh * discharge_spp_per_kwh` (2026-09-26 live P&L review,
    finding #1): market energy sales settle at the zone's real-time SPP AT DELIVERY, per 02a S7.1's
    own "ISO_SETTLEMENT_SHADOW" baseline description -- never `price_per_kwh` (the opportunity's
    `value_per_mwh`, a forward/planned bid that is null for many admission paths, which is why live
    ERCOT_ENERGY revenue was settling at $0 despite real delivered energy and a live SPP feed).
    `price_per_kwh` is still used for every other, non-market-dispatch service (its own contract
    price is the right basis there, e.g. a regulated or capacity-hold rate)."""
    if service_type == "ERCOT_AS":
        return committed_kwh * price_per_kwh + delivered_kwh * discharge_spp_per_kwh
    if service_type == "REGULATED_CAPACITY":
        return regulated_capacity_amount if regulated_capacity_amount is not None else _ZERO
    if service_type == "ERCOT_ENERGY":
        return delivered_kwh * discharge_spp_per_kwh
    return price_per_kwh * delivered_kwh


def compute_pnl(
    *,
    service_type: ServiceType,
    delivered_kwh: Decimal,
    price_per_kwh: Decimal,
    charging_cost_per_kwh: Decimal,
    discharge_spp_per_kwh: Decimal,
    eta_d: Decimal,
    degradation_cost_per_kwh: Decimal,
    shortfall_kwh: Decimal,
    committed_kwh: Decimal,
    penalty: PenaltyParams | None,
    delivery_charge: Decimal = _ZERO,
    regulated_capacity_amount: Decimal | None = None,
) -> PnlBreakdown:
    """02a S7.4's four-term (now five-term) decomposition for one obligation-interval, written to
    `og.pnl`. `charging_cost_per_kwh` is what was paid to charge the energy, not the
    discharge-interval wholesale price (module docstring, G4 fix). `discharge_spp_per_kwh` only
    affects `ERCOT_AS`'s revenue (`compute_revenue`) -- every other service ignores it.
    `delivery_charge` is the already-resolved M1 TDSP charge (`opengrid.settle.tariffs.
    m1_delivery_charge`, 09 D5) -- this function does not resolve tariffs itself, it only nets the
    dollar figure the caller computed. `regulated_capacity_amount` is likewise the already-resolved
    `opengrid.settle.regulated.regulated_capacity_payment` figure, used only for `REGULATED_CAPACITY`
    revenue."""
    revenue = compute_revenue(
        service_type=service_type,
        delivered_kwh=delivered_kwh,
        committed_kwh=committed_kwh,
        price_per_kwh=price_per_kwh,
        discharge_spp_per_kwh=discharge_spp_per_kwh,
        regulated_capacity_amount=regulated_capacity_amount,
    )
    energy_cost = (charging_cost_per_kwh / eta_d) * delivered_kwh if eta_d != 0 else _ZERO
    # Frank #7 (09 D8): wear is charged on AC kWh actually discharged (`delivered_kwh`, never a held
    # or charging quantity) via the ONE shared formula both settle and the selector use
    # (`opengrid.core.economics.wear_cost`) -- settle never re-derives the multiplication itself.
    degradation_cost = wear_cost(delivered_kwh, degradation_cost_per_kwh)
    penalty_amount = compute_penalty(shortfall_kwh, committed_kwh, penalty)
    net_value = revenue - energy_cost - degradation_cost - penalty_amount - delivery_charge
    return PnlBreakdown(
        revenue=revenue,
        energy_cost=energy_cost,
        degradation_cost=degradation_cost,
        penalty=penalty_amount,
        delivery_charge=delivery_charge,
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
