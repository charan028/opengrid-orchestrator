"""$/kW economics: $/kW-in (charging) vs $/kW-out (third-party revenue), net $/kW-yr and payback
(08 S3.4, S3b, S3c; 09 S4). Per contract, per market and fleet-wide.

09 S4, for one scope over period P (annualisation `Y_P = 8760 / hours(P)`, kW basis `K`):

    C_in  = charging energy + delivery (M1, FREE only) + demand/fixed charges
    R_out = capacity + energy + AS/availability - LD penalties - buyback      (third parties only; HOME
            self-serve is excluded)
    W     = wear on AC kWh discharged (`core.economics.wear_cost`, 09 D8)
    OM    = O&M
    N     = R_out - C_in - W - OM
    in = C_in Y_P / K,  out = R_out Y_P / K,  n = N Y_P / K                          [$/kW-yr]
    payback simple = I / (N Y_P);  effective = (I - incentives) / (N Y_P);  discounted = min L with
    sum_{y<=L} N Y_P / (1+r)^y >= I;  NPV_L = sum_{y<=L} N Y_P/(1+r)^y - I  for L = 5, 15.

Market and fleet figures are sums of their scopes' totals divided by the summed kW basis (`rollup`).
`unit_economics` is 08 S3b's forward-looking per-unit model; `illustrative_home_unit` reproduces the
08 S3c table (net ~ $1,620/yr, payback ~ 4.3 years before free-market scarcity upside).

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from opengrid.core.economics import wear_cost
from opengrid.core.models.market import Market
from opengrid.market.capacity import HOURS_PER_YEAR
from opengrid.market.charging import blend
from opengrid.market.config import (
    AUSTIN_ENERGY,
    HOME_UNIT_CAPEX_USD,
    HOME_UNIT_KW,
    HOME_UNIT_USABLE_KWH,
    TARGET_PAYBACK_YEARS,
)

ScopeKind = Literal["UNIT", "ASSET", "CONTRACT", "HEADROOM", "MARKET", "FLEET"]

METHOD_VERSION = "per-kw-v1 (09 S4, 08 S3b/S3c)"
_ZERO = Decimal("0")
_ONE = Decimal("1")
_MAX_PAYBACK_SEARCH_YEARS = 50
_NPV_HORIZONS = (5, 15)


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """One scope's dollar totals over a period of `hours` (a settlement month, or 8760 for a planning
    year). All amounts are positive magnitudes; `penalty_usd` and `buyback_usd` are subtracted from
    revenue. `kw_basis` is 09 S4's K (installed kW for an asset, time-weighted committed kW for a
    contract)."""

    scope_kind: ScopeKind
    scope_ref: str
    market: Market | None
    kw_basis: Decimal
    hours: Decimal
    charging_energy_usd: Decimal = _ZERO
    delivery_charge_usd: Decimal = _ZERO
    demand_charge_usd: Decimal = _ZERO
    capacity_revenue_usd: Decimal = _ZERO
    energy_revenue_usd: Decimal = _ZERO
    other_revenue_usd: Decimal = _ZERO
    penalty_usd: Decimal = _ZERO
    buyback_usd: Decimal = _ZERO
    wear_usd: Decimal = _ZERO
    om_usd: Decimal = _ZERO
    capex_usd: Decimal = _ZERO
    incentives_usd: Decimal = _ZERO
    discount_rate: Decimal = _ZERO
    notes: tuple[str, ...] = field(default=())

    @property
    def cost_in_usd(self) -> Decimal:
        return self.charging_energy_usd + self.delivery_charge_usd + self.demand_charge_usd

    @property
    def revenue_out_usd(self) -> Decimal:
        return (
            self.capacity_revenue_usd
            + self.energy_revenue_usd
            + self.other_revenue_usd
            - self.penalty_usd
            - self.buyback_usd
        )

    @property
    def net_usd(self) -> Decimal:
        return self.revenue_out_usd - self.cost_in_usd - self.wear_usd - self.om_usd


class KwEconomics(BaseModel):
    """The per-kW view of one scope (the profitability screen's row; 09 S4 `og.econ_rollup` shape)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_kind: ScopeKind
    scope_ref: str
    market: Market | None
    kw_basis: Decimal
    period_hours: Decimal
    # Annualised dollar totals.
    cost_in_usd_per_yr: Decimal
    revenue_out_usd_per_yr: Decimal
    wear_usd_per_yr: Decimal
    om_usd_per_yr: Decimal
    net_usd_per_yr: Decimal
    # Components of $-in and $-out, annualised.
    charging_energy_usd_per_yr: Decimal
    delivery_charge_usd_per_yr: Decimal
    demand_charge_usd_per_yr: Decimal
    capacity_revenue_usd_per_yr: Decimal
    energy_revenue_usd_per_yr: Decimal
    # The headline $/kW figures.
    in_usd_per_kw_yr: Decimal | None
    out_usd_per_kw_yr: Decimal | None
    net_usd_per_kw_yr: Decimal | None
    capex_usd: Decimal
    capex_usd_per_kw: Decimal | None
    effective_investment_usd_per_kw: Decimal | None
    payback_years: Decimal | None
    effective_payback_years: Decimal | None
    discounted_payback_years: int | None
    npv_5y_usd: Decimal
    npv_15y_usd: Decimal
    meets_target: bool | None
    target_payback_years: Decimal
    notes: tuple[str, ...] = ()


def _per_kw(amount: Decimal, kw: Decimal) -> Decimal | None:
    return amount / kw if kw > _ZERO else None


def _payback(investment: Decimal, annual_net: Decimal) -> Decimal | None:
    if annual_net <= _ZERO:
        return None
    return max(investment, _ZERO) / annual_net


def discounted_payback_years(investment: Decimal, annual_net: Decimal, rate: Decimal) -> int | None:
    """The smallest whole L with sum_{y=1..L} N/(1+r)^y >= I; `None` if never within 50 years."""
    if investment <= _ZERO:
        return 0
    cumulative = _ZERO
    for year in range(1, _MAX_PAYBACK_SEARCH_YEARS + 1):
        cumulative += annual_net / (_ONE + rate) ** year
        if cumulative >= investment:
            return year
    return None


def npv(investment: Decimal, annual_net: Decimal, rate: Decimal, years: int) -> Decimal:
    """Lifetime value (08 S3b): sum_{y=1..L} N/(1+r)^y - I."""
    return sum((annual_net / (_ONE + rate) ** y for y in range(1, years + 1)), _ZERO) - investment


def period_kw_economics(totals: PeriodTotals) -> KwEconomics:
    """09 S4's per-kW economics for one scope's period totals."""
    if totals.hours <= _ZERO:
        raise ValueError(f"period hours must be > 0, got {totals.hours}")
    y = HOURS_PER_YEAR / totals.hours
    k = totals.kw_basis
    annual_net = totals.net_usd * y
    payback = _payback(totals.capex_usd, annual_net)
    return KwEconomics(
        scope_kind=totals.scope_kind,
        scope_ref=totals.scope_ref,
        market=totals.market,
        kw_basis=k,
        period_hours=totals.hours,
        cost_in_usd_per_yr=totals.cost_in_usd * y,
        revenue_out_usd_per_yr=totals.revenue_out_usd * y,
        wear_usd_per_yr=totals.wear_usd * y,
        om_usd_per_yr=totals.om_usd * y,
        net_usd_per_yr=annual_net,
        charging_energy_usd_per_yr=totals.charging_energy_usd * y,
        delivery_charge_usd_per_yr=totals.delivery_charge_usd * y,
        demand_charge_usd_per_yr=totals.demand_charge_usd * y,
        capacity_revenue_usd_per_yr=totals.capacity_revenue_usd * y,
        energy_revenue_usd_per_yr=totals.energy_revenue_usd * y,
        in_usd_per_kw_yr=_per_kw(totals.cost_in_usd * y, k),
        out_usd_per_kw_yr=_per_kw(totals.revenue_out_usd * y, k),
        net_usd_per_kw_yr=_per_kw(annual_net, k),
        capex_usd=totals.capex_usd,
        capex_usd_per_kw=_per_kw(totals.capex_usd, k),
        effective_investment_usd_per_kw=_per_kw(totals.capex_usd - totals.incentives_usd, k),
        payback_years=payback,
        effective_payback_years=_payback(totals.capex_usd - totals.incentives_usd, annual_net),
        discounted_payback_years=discounted_payback_years(totals.capex_usd, annual_net, totals.discount_rate),
        npv_5y_usd=npv(totals.capex_usd, annual_net, totals.discount_rate, _NPV_HORIZONS[0]),
        npv_15y_usd=npv(totals.capex_usd, annual_net, totals.discount_rate, _NPV_HORIZONS[1]),
        # None when no investment is attributed to the scope (nothing to pay back).
        meets_target=None
        if totals.capex_usd <= _ZERO
        else payback is not None and payback <= TARGET_PAYBACK_YEARS,
        target_payback_years=TARGET_PAYBACK_YEARS,
        notes=totals.notes,
    )


_SUMMED_FIELDS = tuple(
    f.name
    for f in fields(PeriodTotals)
    if f.name not in {"scope_kind", "scope_ref", "market", "hours", "discount_rate", "notes"}
)


def rollup(
    scopes: Sequence[PeriodTotals], *, scope_kind: ScopeKind, scope_ref: str, market: Market | None
) -> PeriodTotals:
    """Sum scopes of the same period into one (09 S4: market and fleet figures are sums over their
    contracts and headroom pseudo-scopes, divided by the summed kW basis). Raises on mixed periods or
    discount rates, which cannot be summed meaningfully."""
    if not scopes:
        return PeriodTotals(
            scope_kind=scope_kind, scope_ref=scope_ref, market=market, kw_basis=_ZERO, hours=_ONE
        )
    hours = {s.hours for s in scopes}
    rates = {s.discount_rate for s in scopes}
    if len(hours) != 1 or len(rates) != 1:
        raise ValueError("rollup needs scopes over one period and one discount rate")
    sums: dict[str, Decimal] = {
        name: sum((getattr(s, name) for s in scopes), _ZERO) for name in _SUMMED_FIELDS
    }
    base = PeriodTotals(
        scope_kind=scope_kind,
        scope_ref=scope_ref,
        market=market,
        kw_basis=_ZERO,
        hours=hours.pop(),
        discount_rate=rates.pop(),
    )
    return replace(base, **sums)  # type: ignore[arg-type]  # every summed field is a Decimal


@dataclass(frozen=True, slots=True)
class UnitEconomicsInputs:
    """08 S3b's forward-looking model for one unit or asset over a planning year.

    Energy: `charged_kwh_per_cycle` in at `charging_cost_usd_per_kwh` (the blended c_in, incl. M1 in the
    FREE market), `charged x eta_rt` out at `energy_value_usd_per_kwh`, `cycles_per_year` times.
    Wear: `core.economics.wear_cost` on kWh out at `wear_rate_usd_per_kwh` (09 D8). O&M:
    `om_frac_of_capex` x capex per year (08 S3c folds degradation into this 3% and sets wear to 0)."""

    kw: Decimal
    capex_usd: Decimal
    charged_kwh_per_cycle: Decimal
    eta_rt: Decimal
    cycles_per_year: Decimal
    capacity_price_usd_per_kw_yr: Decimal
    energy_value_usd_per_kwh: Decimal
    charging_cost_usd_per_kwh: Decimal
    delivery_charge_usd_per_kwh: Decimal = _ZERO
    om_frac_of_capex: Decimal = _ZERO
    wear_rate_usd_per_kwh: Decimal = _ZERO
    incentives_usd: Decimal = _ZERO
    discount_rate: Decimal = _ZERO
    market: Market | None = None
    scope_ref: str = "unit"


def unit_totals(inputs: UnitEconomicsInputs) -> PeriodTotals:
    """08 S3b as one planning year of `PeriodTotals`."""
    kwh_in = inputs.charged_kwh_per_cycle * inputs.cycles_per_year
    kwh_out = kwh_in * inputs.eta_rt
    return PeriodTotals(
        scope_kind="UNIT",
        scope_ref=inputs.scope_ref,
        market=inputs.market,
        kw_basis=inputs.kw,
        hours=HOURS_PER_YEAR,
        charging_energy_usd=kwh_in * inputs.charging_cost_usd_per_kwh,
        delivery_charge_usd=kwh_in * inputs.delivery_charge_usd_per_kwh,
        capacity_revenue_usd=inputs.kw * inputs.capacity_price_usd_per_kw_yr,
        energy_revenue_usd=kwh_out * inputs.energy_value_usd_per_kwh,
        wear_usd=wear_cost(kwh_out, inputs.wear_rate_usd_per_kwh),
        om_usd=inputs.capex_usd * inputs.om_frac_of_capex,
        capex_usd=inputs.capex_usd,
        incentives_usd=inputs.incentives_usd,
        discount_rate=inputs.discount_rate,
    )


def unit_economics(inputs: UnitEconomicsInputs) -> KwEconomics:
    """08 S3b per-kW economics of one unit or asset."""
    return period_kw_economics(unit_totals(inputs))


#: 08 S3c's assumptions (owner-confirmed 2026-09-26, D-23).
ILLUSTRATIVE_ETA_RT = Decimal("0.9")
ILLUSTRATIVE_CYCLES_PER_YEAR = Decimal("300")
ILLUSTRATIVE_ENERGY_VALUE_USD_PER_KWH = Decimal("0.1288")  # AE value of solar, Oct 2026
ILLUSTRATIVE_OM_FRAC = Decimal("0.03")


def illustrative_home_unit_inputs() -> UnitEconomicsInputs:
    """08 S3c: one 11 kW / 39.2 kWh home unit, $7,000, in Austin Energy territory at $75/kW-yr; charging
    30% solar at 4.0 cents + 70% AE off-peak at 2.677 cents = 3.07 cents; energy out at 12.88 cents; 300
    cycles; degradation and O&M 3% of capex."""
    ae = AUSTIN_ENERGY
    c_in = blend(ae.solar_share_floor, ae.solar_cost_usd_per_kwh, ae.off_peak_rate_usd_per_kwh)
    return UnitEconomicsInputs(
        kw=HOME_UNIT_KW,
        capex_usd=HOME_UNIT_CAPEX_USD,
        charged_kwh_per_cycle=HOME_UNIT_USABLE_KWH,
        eta_rt=ILLUSTRATIVE_ETA_RT,
        cycles_per_year=ILLUSTRATIVE_CYCLES_PER_YEAR,
        capacity_price_usd_per_kw_yr=ae.capacity_price_usd_per_kw or _ZERO,
        energy_value_usd_per_kwh=ILLUSTRATIVE_ENERGY_VALUE_USD_PER_KWH,
        charging_cost_usd_per_kwh=c_in,
        om_frac_of_capex=ILLUSTRATIVE_OM_FRAC,
        market="REGULATED",
        scope_ref="08-S3c-home-unit",
    )


def illustrative_home_unit() -> KwEconomics:
    """The 08 S3c table: net ~ $1,617/yr (~ $147/kW-yr), payback ~ 4.33 years before scarcity upside."""
    return unit_economics(illustrative_home_unit_inputs())


def stored_energy_avg_cost(
    *,
    charging_cost_usd: Decimal,
    discharged_kwh: Decimal,
    eta_d: Decimal,
    energy_start_kwh: Decimal,
    energy_end_kwh: Decimal,
) -> Decimal | None:
    """09 S4 attribution (fixes G4): the stored-energy average cost per AC kWh discharged,
    `c_bar = C_in / (E_dis + eta_d (e_end - e_start))`. The energy-inventory change is valued at the same
    average so period boundaries do not distort. `None` when nothing was discharged or stored."""
    denominator = discharged_kwh + eta_d * (energy_end_kwh - energy_start_kwh)
    if denominator <= _ZERO:
        return None
    return charging_cost_usd / denominator
