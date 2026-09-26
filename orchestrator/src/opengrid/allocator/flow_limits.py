"""Dispatch-side discharge-flow limits (09-optimizer-dispatcher-update.md S1.9 F1-F3, owner decision
D-26): the RT caps the allocator applies before water-filling. The guardian re-checks each on its own
reads (G-02 changed, G-26..G-29) with the SAME `opengrid.core.limits` functions, so the allocator's cap
never exceeds the guardian's bound on the same inputs (one formula, two data paths).

- **F1** `P_max(SoC, T)` (`core.limits.derated_power_bounds_kw`): always applied to a hub with a known
  rating and live SoC. An unknown cell temperature takes the guardian's fail-closed factor (0.5), a
  reported BMS limit binds.
- **F2** home load first, then the meter export limit (`core.limits.home_meter_setpoint_band_kw`), where
  the hub's export limit is known (its own, else the configured default). An unknown home load counts as
  zero load (all discharge exports).
- **F3** aggregate flows: a service-transformer group cap (`D_x <= S_x + L_x`, nested inside the
  obligation's water-fill), and per-cycle feeder and substation discharge budgets split across their
  banks in proportion to capability, where the topology and its ratings are known.

F2 and F3 run behind `FlowLimits.enabled`; F1 always, since the guardian's G-02 always applies it.

Pure functions; no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, MutableMapping, Sequence

from opengrid.allocator.models import BankSnapshot, FlowLimits, HubSnapshot
from opengrid.core.limits import (
    DERATE_UNKNOWN_TEMP_FACTOR,
    HOME_LOAD_DROP_KW,
    derated_power_bounds_kw,
    home_meter_setpoint_band_kw,
)
from opengrid.core.physics import DEFAULT_INVERTER_CAP_KW, HubParams

_EPS = 1e-9
#: F2's import side is not a discharge limit; an unbounded service rating leaves only the export side.
_NO_IMPORT_LIMIT_KW = float("inf")


def hub_units(rated_kw: float, units: int | None) -> int:
    """The home's unit count for the per-unit inverter cap (`core.limits.continuous_power_kw`): the
    registry's (`og.hub.units`) when known; else derived from the rating (11 kW per unit, at least 1), so a
    20 kW dual-unit home is never planned at a single unit's 11 kW."""
    if units is not None and units >= 1:
        return units
    return max(1, math.ceil(rated_kw / DEFAULT_INVERTER_CAP_KW - 1e-9))


def derated_discharge_kw(hub: HubSnapshot) -> float | None:
    """F1: the hub's derated discharge bound, or `None` when its rating or live SoC is unknown (a hub
    without a live SoC already offers 0 kW, K1)."""
    if hub.rated_kw is None or hub.soc_kwh is None or hub.reserve_kwh is None or hub.e_kwh is None:
        return None
    params = HubParams(
        e_kwh=hub.e_kwh,
        r_kwh=hub.reserve_kwh,
        p_kw=hub.rated_kw,
        eta_d=hub.eta_d,
        units=hub_units(hub.rated_kw, hub.units),
    )
    bounds = derated_power_bounds_kw(
        params,
        hub.soc_kwh,
        hub.cell_temp_c,
        bms_discharge_kw=hub.p_dis_max_kw,
        unknown_temp_factor=DERATE_UNKNOWN_TEMP_FACTOR,
    )
    return bounds.discharge_kw


def home_net_load_kw(hub: HubSnapshot) -> float | None:
    """`L_net = meter_kw - p_kw` (the home's own net load, + import), or `None` when either is unknown."""
    if hub.meter_kw is None or hub.p_kw is None:
        return None
    return hub.meter_kw - hub.p_kw


def export_cap_kw(hub: HubSnapshot, default_export_limit_kw: float | None) -> float | None:
    """F2: the most the hub may discharge so the meter export stays within its limit after serving the
    home first (`core.limits.home_meter_setpoint_band_kw`'s export side). An unknown load counts as 0.
    `None`: no export limit known for this hub (not capped here)."""
    limit = hub.export_limit_kw if hub.export_limit_kw is not None else default_export_limit_kw
    if limit is None:
        return None
    load = home_net_load_kw(hub)
    low_load = load if load is not None else 0.0
    lowest_p, _ = home_meter_setpoint_band_kw(low_load, low_load, max(limit, 0.0), _NO_IMPORT_LIMIT_KW)
    return max(-lowest_p, 0.0)


LOAD_DROP_ALLOWANCE_KW = HOME_LOAD_DROP_KW


def cap_hub(hub: HubSnapshot, limits: FlowLimits) -> HubSnapshot:
    """F1 (always) and F2 (when enabled) applied to one hub's `free_discharge_kw` (only ever lowered)."""
    cap = hub.free_discharge_kw
    derated = derated_discharge_kw(hub)
    if derated is not None:
        cap = min(cap, derated)
    if limits.enabled:
        export = export_cap_kw(hub, limits.default_export_limit_kw)
        if export is not None:
            cap = min(cap, export)
    cap = max(cap, 0.0)
    return hub if cap >= hub.free_discharge_kw else hub.evolve(free_discharge_kw=cap)


def xfmr_budgets_kw(hubs: Sequence[HubSnapshot], limits: FlowLimits) -> dict[str, float]:
    """F3 service transformer, discharge direction: the total discharge of a transformer's homes may
    reach `S_x + L_x` (reverse flow up to the rating, after their own net load `L_x`; an unknown load
    counts as 0). Only transformers with a known rating are capped."""
    if not limits.enabled or not limits.xfmr_kva:
        return {}
    load_by_xfmr: dict[str, float] = {}
    for hub in hubs:
        if hub.xfmr_id is None or hub.xfmr_id not in limits.xfmr_kva:
            continue
        load = home_net_load_kw(hub)
        load_by_xfmr[hub.xfmr_id] = load_by_xfmr.get(hub.xfmr_id, 0.0) + max(load or 0.0, 0.0)
    return {x: max(limits.xfmr_kva[x] + load, 0.0) for x, load in load_by_xfmr.items()}


def apply_group_caps(
    hubs: tuple[HubSnapshot, ...], remaining_by_xfmr: Mapping[str, float]
) -> tuple[HubSnapshot, ...]:
    """Scale each capped transformer's member hubs so their free kW sums to at most the transformer's
    remaining budget this cycle (a nested cap: water-filling then redistributes within the obligation)."""
    if not remaining_by_xfmr:
        return hubs
    free_by_xfmr: dict[str, float] = {}
    for hub in hubs:
        if hub.xfmr_id is not None and hub.xfmr_id in remaining_by_xfmr:
            free_by_xfmr[hub.xfmr_id] = free_by_xfmr.get(hub.xfmr_id, 0.0) + hub.free_discharge_kw
    scale = {
        x: max(remaining_by_xfmr[x], 0.0) / free
        for x, free in free_by_xfmr.items()
        if free > _EPS and free > remaining_by_xfmr[x] + _EPS
    }
    if not scale:
        return hubs
    scaled: list[HubSnapshot] = []
    for hub in hubs:
        factor = scale.get(hub.xfmr_id) if hub.xfmr_id is not None else None
        scaled.append(hub if factor is None else hub.evolve(free_discharge_kw=hub.free_discharge_kw * factor))
    return tuple(scaled)


def consume_group_budget(
    per_hub_kw: Mapping[str, float], xfmr_of: Mapping[str, str], remaining_by_xfmr: MutableMapping[str, float]
) -> None:
    """Deduct one obligation's realized per-hub kW from its transformers' remaining budgets."""
    for hub_id, kw in per_hub_kw.items():
        xfmr = xfmr_of.get(hub_id)
        if xfmr is not None and xfmr in remaining_by_xfmr:
            remaining_by_xfmr[xfmr] -= kw


def bank_budget_caps_kw(banks: Sequence[BankSnapshot], limits: FlowLimits) -> dict[str, float]:
    """F3 feeder and substation, discharge direction: each per-cycle budget split across the banks on
    that feeder/substation in proportion to their capability (09 S1.9: "runs before each bank's S3").
    Returns a cap per bank that has a budgeted feeder or substation."""
    if not limits.enabled:
        return {}
    caps: dict[str, float] = {}
    for budgets, key, mapping in (
        (limits.feeder_budget_kw, "feeder_id", limits.feeder_by_bank),
        (limits.substation_budget_kw, "substation_id", limits.substation_by_bank),
    ):
        if not budgets:
            continue
        members: dict[str, list[BankSnapshot]] = {}
        for bank in banks:
            group = getattr(bank, key) or mapping.get(bank.bank_id)
            if group is not None and group in budgets:
                members.setdefault(group, []).append(bank)
        for group, group_banks in members.items():
            total = sum(max(b.capability_kw, 0.0) for b in group_banks)
            budget = max(budgets[group], 0.0)
            for bank in group_banks:
                share = budget * (max(bank.capability_kw, 0.0) / total) if total > _EPS else 0.0
                caps[bank.bank_id] = min(caps.get(bank.bank_id, share), share)
    return caps


def with_topology(hub: HubSnapshot, limits: FlowLimits) -> HubSnapshot:
    """The hub with its registry topology (transformer, export limit) filled in where the snapshot has none."""
    xfmr = limits.xfmr_by_hub.get(hub.hub_id) if hub.xfmr_id is None else None
    export = limits.export_limit_by_hub.get(hub.hub_id) if hub.export_limit_kw is None else None
    if xfmr is None and export is None:
        return hub
    return hub.evolve(
        xfmr_id=xfmr if xfmr is not None else hub.xfmr_id,
        export_limit_kw=export if export is not None else hub.export_limit_kw,
    )
