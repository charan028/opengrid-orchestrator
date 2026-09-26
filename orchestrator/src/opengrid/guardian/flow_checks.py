"""Guardian discharge-flow checks (09-optimizer-dispatcher-update.md S2.6; K4, K15): G-02 (derated),
G-05 (synchronized step), G-26..G-33.

Pure functions over the guardian's OWN reads (hub telemetry it received itself, its SCADA reads, static rows),
delegating every bound to `opengrid.core.limits` / `opengrid.market.territory` -- the same functions the
allocator calls. The guardian never clips: each check returns PASS or a veto `CheckOutcome`.

Missing or stale data fails closed. A telemetry field a hub has NEVER reported is the one exception while
`flow_telemetry_required` is off: the static premise limits apply until the field lands (lead direction
2026-09-26); a reported value that goes stale always takes the spec's worst case.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from opengrid.core import limits as core_limits
from opengrid.core import reasons
from opengrid.core.models.market import UtilityId
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.ports import (
    AggregateFlow,
    HubSite,
    HubSnapshot,
    ObligationMarket,
    PoiLimit,
    ProposedItem,
    Reading,
    ServiceTransformer,
)
from opengrid.market.territory import (
    FREE,
    MarketModelError,
    MarketRef,
    check_territory,
    market_of,
    territory_of_zone,
)

G02, G05, G26, G27, G28, G29, G30, G31, G32, G33, G34 = (
    "G-02",
    "G-05",
    "G-26",
    "G-27",
    "G-28",
    "G-29",
    "G-30",
    "G-31",
    "G-32",
    "G-33",
    "G-34",
)


@dataclass(frozen=True, slots=True)
class FlowPolicy:
    """The `GuardianConfig` values these checks need (kept separate so the checks stay pure)."""

    telemetry_required: bool
    max_age_s: float
    unknown_temp_factor: float
    load_drop_kw: float
    inverter_cap_kw: float
    default_pv_rated_kw: float
    default_service_kw: float
    xfmr_forward_pct: float
    xfmr_reverse_pct: float
    xfmr_max_stale_fraction: float
    unmapped_xfmr_kva_per_home: float


def fresh(reading: Reading | None, max_age_s: float) -> float | None:
    """The reading's value when it is fresh, else None (stale or never reported)."""
    if reading is None or not math.isfinite(reading.age_s) or reading.age_s > max_age_s:
        return None
    return reading.value


def is_static(reading: Reading | None, policy: FlowPolicy) -> bool:
    """Never reported and not yet required: the static limits apply."""
    return reading is None and not policy.telemetry_required


# --- G-02 (changed) and G-31 --------------------------------------------------------------------------


def check_g31_peak(
    item: ProposedItem,
    hub: HubSnapshot,
    site: HubSite | None,
    lease_ttl_s: float,
    policy: FlowPolicy,
) -> CheckOutcome:
    """F5/G-31: above continuous only within the peak allowance (P_pk, tau_pk, the reported budget)."""
    continuous = core_limits.continuous_power_kw(hub.params, inverter_cap_kw=policy.inverter_cap_kw)
    result = core_limits.check_peak_power(
        item.p_kw_setpoint,
        continuous,
        peak_kw=site.peak_kw if site else None,
        tau_peak_s=site.tau_peak_s if site else None,
        lease_ttl_s=lease_ttl_s,
        peak_budget_kws=fresh(hub.flow.peak_budget_kws, policy.max_age_s),
    )
    return CheckOutcome(G31, result.ok, result.reason, item.hub_id)


def derated_bounds(
    hub: HubSnapshot, policy: FlowPolicy, *, base_kw: float | None = None, static_temp_unknown: bool = False
) -> core_limits.PowerBounds:
    """G-02's P_max(SoC, T) on the guardian's own telemetry. A stale (or required-but-missing) temperature
    takes `unknown_temp_factor`; one never reported (static mode) takes the nameplate curve (factor 1),
    unless `static_temp_unknown` -- G-19's capability evidence, which must match the allocator's own
    bound (`allocator.flow_limits.derated_discharge_kw`: any unknown temperature takes the factor).
    A stale BMS limit drops that term."""
    temp_reading = hub.flow.cell_temp_c
    temp = fresh(temp_reading, policy.max_age_s)
    unknown = (
        1.0 if is_static(temp_reading, policy) and not static_temp_unknown else policy.unknown_temp_factor
    )
    return core_limits.derated_power_bounds_kw(
        hub.params,
        hub.soc_kwh,
        temp,
        bms_discharge_kw=fresh(hub.flow.p_dis_max_kw, policy.max_age_s),
        bms_charge_kw=fresh(hub.flow.p_ch_max_kw, policy.max_age_s),
        unknown_temp_factor=unknown,
        inverter_cap_kw=policy.inverter_cap_kw,
        base_kw=base_kw,
    )


def check_g02_derated(
    item: ProposedItem, hub: HubSnapshot, policy: FlowPolicy, *, peak_kw: float | None = None
) -> CheckOutcome:
    """G-02 (changed, F1): the setpoint within P_max(SoC, T) per direction. `peak_kw` is the base only when
    G-31 has already admitted an above-continuous setpoint."""
    continuous = core_limits.continuous_power_kw(hub.params, inverter_cap_kw=policy.inverter_cap_kw)
    if peak_kw is None and abs(item.p_kw_setpoint) > continuous + 1e-9:
        return CheckOutcome(G02, False, reasons.R_HUB_POWER_LIMIT, item.hub_id)
    result = core_limits.check_hub_power_derated(
        item.p_kw_setpoint, derated_bounds(hub, policy, base_kw=peak_kw)
    )
    return CheckOutcome(G02, result.ok, result.reason, item.hub_id)


# --- G-26 home meter ----------------------------------------------------------------------------------


def net_load_bounds_kw(hub: HubSnapshot, site: HubSite, policy: FlowPolicy) -> tuple[float, float]:
    """(export-side, import-side) net home load L = M - p the guardian assumes. Fresh meter: measured on both
    sides. Stale or required-but-missing: -PV rated (no load, full PV) and the service rating. Never
    reported (static mode): no load."""
    meter = fresh(hub.flow.meter_kw, policy.max_age_s)
    if meter is not None:
        load = meter - hub.prev_p_kw
        return load, load
    if is_static(hub.flow.meter_kw, policy):
        return 0.0, 0.0
    service = site.service_kw if site.service_kw is not None else policy.default_service_kw
    return -site.pv_rated_kw, service


def check_g26_home_meter(
    item: ProposedItem, hub: HubSnapshot, site: HubSite | None, policy: FlowPolicy
) -> CheckOutcome:
    """F2/G-26: M = L + p within [-X_exp, S_svc] over the lease, net of home load. Unknown X_exp is 0
    (discharge only to the measured load). A setpoint no further outside the band than the current one
    is relief and passes."""
    if site is None:
        return CheckOutcome(G26, False, "HUB_SITE_UNKNOWN", item.hub_id)
    low_load, high_load = net_load_bounds_kw(hub, site, policy)
    band = core_limits.home_meter_setpoint_band_kw(
        low_load,
        high_load,
        site.export_limit_kw if site.export_limit_kw is not None else 0.0,
        site.service_kw if site.service_kw is not None else policy.default_service_kw,
        load_drop_kw=policy.load_drop_kw,
    )
    # Each side on its own, with its own relief: a setpoint that does not export more than now (or import
    # more than now) is never refused on that side -- with a stale meter the worst-case import assumption
    # alone must not force a discharge.
    low, high = band
    p, prev = item.p_kw_setpoint, hub.prev_p_kw
    if p < low - 1e-9 and p < prev - 1e-9:
        return CheckOutcome(G26, False, reasons.R_HOME_EXPORT_LIMIT, item.hub_id)
    if p > high + 1e-9 and p > prev + 1e-9:
        return CheckOutcome(G26, False, reasons.R_HOME_IMPORT_LIMIT, item.hub_id)
    return CheckOutcome.passed(G26, hub_id=item.hub_id)


# --- G-27 service transformer (group) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransformerMember:
    hub: HubSnapshot | None
    site: HubSite | None


def member_flow_bounds_kw(member: TransformerMember, policy: FlowPolicy) -> tuple[float, float, bool]:
    """(reverse-worst, forward-worst, stale) meter flow of one transformer member. A fresh meter is exact; a
    never-reported one (static mode) is its own setpoint with no load; a stale or unknown one takes the
    worst case per direction (-PV rated, the service rating)."""
    hub, site = member.hub, member.site
    if hub is not None:
        meter = fresh(hub.flow.meter_kw, policy.max_age_s)
        if meter is not None:
            return meter, meter, False
        if is_static(hub.flow.meter_kw, policy) and hub.health == "online":
            return hub.prev_p_kw, hub.prev_p_kw, False
    pv = site.pv_rated_kw if site is not None else policy.default_pv_rated_kw
    service = (
        site.service_kw if site is not None and site.service_kw is not None else policy.default_service_kw
    )
    return -pv, service, True


def check_g27_transformer(
    transformer: ServiceTransformer,
    members: Mapping[str, TransformerMember],
    batch_delta_kw: float,
    policy: FlowPolicy,
) -> tuple[bool, str | None]:
    """F3/G-27: -rho_rev S <= F_x + delta <= rho_xf S over ALL members, evaluated at both worst-case ends. Relief
    (no further outside the band) passes. More than `xfmr_max_stale_fraction` of the members stale: any
    change that increases |F| at either end is vetoed. Returns (ok, reason)."""
    low = high = 0.0
    stale = 0
    for hub_id in transformer.members:
        m_low, m_high, m_stale = member_flow_bounds_kw(
            members.get(hub_id, TransformerMember(None, None)), policy
        )
        low += m_low
        high += m_high
        stale += int(m_stale)
    lower = -policy.xfmr_reverse_pct * transformer.rating_kva
    upper = policy.xfmr_forward_pct * transformer.rating_kva
    if transformer.members and stale / len(transformer.members) > policy.xfmr_max_stale_fraction:
        increases = (
            abs(low + batch_delta_kw) > abs(low) + 1e-9 or abs(high + batch_delta_kw) > abs(high) + 1e-9
        )
        return (False, "XFMR_MEMBERS_STALE") if increases else (True, None)
    for now in (low, high):
        result = core_limits.check_flow_band(
            now,
            now + batch_delta_kw,
            lower,
            upper,
            reverse_reason=reasons.R_XFMR_LIMIT,
            forward_reason=reasons.R_XFMR_LIMIT,
        )
        if not result.ok:
            return False, result.reason
    return True, None


# --- G-28/G-29/G-30 aggregate flows (batch) -----------------------------------------------------------------


def check_aggregate_flow(
    rule_id: str,
    flow: AggregateFlow,
    prior_cycle_delta_kw: float,
    cycle_delta_kw: float,
    *,
    max_age_s: float,
    reverse_reason: str,
    forward_reason: str,
    ref: str,
) -> CheckOutcome:
    """-R_rev <= L + (this cycle's accumulated change across banks) <= rho F. A stale or missing reading, or
    an unknown limit, vetoes any batch that increases |this cycle's change| and passes relief. A flow known
    only as an interval (unsigned SCADA) is checked at both ends, each with its own relief."""
    load, lower, upper = flow.flow_kw, flow.lower_kw, flow.upper_kw
    if (
        load is None
        or lower is None
        or upper is None
        or not math.isfinite(flow.age_s)
        or flow.age_s > max_age_s
    ):
        if abs(cycle_delta_kw) > abs(prior_cycle_delta_kw) + 1e-9:
            return CheckOutcome(rule_id, False, "FLOW_UNKNOWN", ref)
        return CheckOutcome.passed(rule_id, hub_id=ref)
    ends = (load,) if flow.flow_low_kw is None else (flow.flow_low_kw, load)
    for now in ends:
        result = core_limits.check_flow_band(
            now + prior_cycle_delta_kw,
            now + cycle_delta_kw,
            lower,
            upper,
            reverse_reason=reverse_reason,
            forward_reason=forward_reason,
        )
        if not result.ok:
            return CheckOutcome(rule_id, False, result.reason, ref)
    return CheckOutcome.passed(rule_id, hub_id=ref)


def check_g29_poi(poi: PoiLimit, batch_setpoint_kw: float) -> CheckOutcome:
    """G-29 POI: a substation asset's net setpoint within [-P_exp, P_imp] (import-positive)."""
    if batch_setpoint_kw < -poi.export_kw - 1e-9 or batch_setpoint_kw > poi.import_kw + 1e-9:
        return CheckOutcome(G29, False, "POI_LIMIT", poi.asset_id)
    return CheckOutcome.passed(G29, hub_id=poi.asset_id)


# --- G-33 territory (K15) -----------------------------------------------------------------------------------


def is_idle(item: ProposedItem) -> bool:
    """A 0 kW item: it can neither export into nor import from any market."""
    return abs(item.p_kw_setpoint) <= 1e-9


def check_hub_in_bank(item: ProposedItem, bank_id: str, hub_bank: str | None) -> CheckOutcome:
    """G-34: every item's hub sits on the proposal's bank per the guardian's own og.hub read (an unknown
    hub fails closed). The batch's lease, epoch/seq and every bank-level check are scoped to `bank_id`."""
    if hub_bank is not None and hub_bank == bank_id:
        return CheckOutcome.passed(G34, hub_id=item.hub_id)
    return CheckOutcome(G34, False, reasons.R_HUB_NOT_IN_BANK, item.hub_id)


def obligation_market_ref(row: ObligationMarket | None) -> MarketRef | None:
    """The obligation's MarketRef from the guardian's own contract read; None (UNKNOWN) when missing or
    inconsistent -- never a guess."""
    if row is None:
        return None
    try:
        return market_of(market=row.market, utility_id=row.utility_id, service_type=row.service_type)
    except MarketModelError:
        return None


def check_g33_territory(
    item: ProposedItem,
    *,
    ref: MarketRef | None,
    zone: str | None,
    zone_territory: Mapping[str, UtilityId],
    free_access: bool,
) -> CheckOutcome:
    """K15/G-33: `market.check_territory` (the ONE predicate) on the guardian's own reads. Headroom (no
    obligation) is FREE. A 0 kW item serves no market (the engine's own territory-block items are 0 kW
    grants carrying the R-TERRITORY-* reason) and passes; a non-zero setpoint is always checked, whatever
    its reason code."""
    if is_idle(item):
        return CheckOutcome.passed(G33, hub_id=item.hub_id)
    reason = check_territory(ref, territory_of_zone(zone, zone_territory), free_access=free_access)
    if reason is None:
        return CheckOutcome.passed(G33, hub_id=item.hub_id)
    return CheckOutcome(
        G33, False, reason, item.hub_id, str(item.obligation_id) if item.obligation_id is not None else None
    )


HEADROOM_MARKET = FREE
