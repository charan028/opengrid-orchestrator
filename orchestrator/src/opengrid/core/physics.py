"""Hub/bank physics: SoC step, P/E/kVA capability, ramp-limit application.

Single owner per 02b S12. `sim` forward-simulates real hub behavior with `soc_step`. `fleet` derives
`capability(bank, t)` from reported state using `hub_capability`/`bank_capability`/`recharge_headroom`
-- it never calls `soc_step` itself (02b S12, "sim and the fleet twin, restated").

Canonical SoC equation (06-first-principles-review.md S5.1, 02b S4.2):
    e[t+1] = e[t] + eta_c * p_c * dt - (dt / eta_d) * p_d - l * dt
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_ETA_C = 0.9487
DEFAULT_ETA_D = 0.9487
DEFAULT_SELF_DISCHARGE_KWH_PER_H = 0.0005
DEFAULT_INVERTER_CAP_KW = 11.0  # CORE-004: named default hub inverter power cap, not a bare literal


@dataclass(frozen=True, slots=True)
class HubParams:
    e_kwh: float  # usable energy capacity
    r_kwh: float  # reserve floor (never discharged below, K1)
    p_kw: float  # power limit (absolute value; charge and discharge both bounded by this)
    eta_c: float = DEFAULT_ETA_C
    eta_d: float = DEFAULT_ETA_D
    self_discharge_kwh_per_h: float = DEFAULT_SELF_DISCHARGE_KWH_PER_H
    ramp_kw_per_s: float | None = None  # None = unconstrained ramp for this hub


def soc_step(
    soc_kwh: float,
    p_kw: float,
    dt_s: float,
    params: HubParams,
) -> float:
    """Advance state of charge by one tick of dt_s seconds under commanded power p_kw
    (+charge / -discharge). Returns the new SoC in kWh, clamped to [0, e_kwh] as a physical
    backstop (callers should never command past the reserve floor -- that is a limits.py concern,
    not physics -- but the clamp keeps this function total and side-effect-free).
    """
    dt_h = dt_s / 3600.0
    p_c = max(p_kw, 0.0)
    p_d = max(-p_kw, 0.0)
    delta = params.eta_c * p_c * dt_h - (p_d * dt_h) / params.eta_d - params.self_discharge_kwh_per_h * dt_h
    new_soc = soc_kwh + delta
    return min(max(new_soc, 0.0), params.e_kwh)


def hub_sustainable_discharge_kw(
    soc_kwh: float,
    reserve_kwh: float,
    p_kw: float,
    dt_h: float,
    eta_d: float,
) -> float:
    """CORE-003/K1: the discharge power (kW, >= 0) actually sustainable for a FULL interval of length
    `dt_h` without breaching the reserve floor, given only `soc_kwh - reserve_kwh` of usable energy
    above reserve. A sliver of energy just above reserve cannot be commanded at the hub's full rated
    power (`p_kw`) for a whole interval -- that would draw more energy than exists above the floor and
    still leave the hub committed to the setpoint until the next cycle. Callers in the allocator's
    capability path use this (not the raw `p_kw` rating) as the ceiling offered to water-filling/PI.
    """
    available_kwh = max(soc_kwh - reserve_kwh, 0.0)
    if available_kwh <= 0.0 or dt_h <= 0.0:
        return 0.0
    energy_limited_kw = (available_kwh * eta_d) / dt_h
    return min(max(p_kw, 0.0), energy_limited_kw)


def hub_sustainable_charge_kw(
    soc_kwh: float,
    e_kwh: float,
    p_kw: float,
    dt_h: float,
    eta_c: float,
) -> float:
    """Symmetric to `hub_sustainable_discharge_kw` (CORE-003/K1): the charge power (kW, >= 0)
    actually sustainable for a FULL interval of length `dt_h` without overfilling the hub above
    `e_kwh`, given only the headroom `e_kwh - soc_kwh` available to absorb charge. A sliver of
    headroom just below full cannot be commanded at the hub's full rated power for a whole interval
    without exceeding `e_kwh` part-way through -- callers use this (not the raw `p_kw` rating) as the
    ceiling offered to any charge-side capability path (allocator continuous energy checks, guardian
    G-01-ENERGY charge-direction projection).
    """
    headroom_kwh = max(e_kwh - soc_kwh, 0.0)
    if headroom_kwh <= 0.0 or dt_h <= 0.0 or eta_c <= 0.0:
        return 0.0
    energy_limited_kw = headroom_kwh / (eta_c * dt_h)
    return min(max(p_kw, 0.0), energy_limited_kw)


def hub_available_energy_kwh(soc_kwh: float, reserve_kwh: float, eta_d: float) -> float:
    """Energy (kWh, >= 0) deliverable from a hub before its SoC reaches the reserve floor, at
    discharge efficiency `eta_d` (03-decision-engine.md S8.11's $E^{free}_i \\eta_d$ term). This is
    the ENERGY-side complement to `hub_sustainable_discharge_kw`'s power-side cap: capacity (kW) alone
    is not enough to know a hub can sustain a committed delivery for its whole remaining window -- the
    continuous per-obligation energy-sufficiency check (K1) sums this across a obligation's eligible
    hubs, net of energy already reserved for OTHER committed obligations on the same hubs (K2).
    """
    return max(soc_kwh - reserve_kwh, 0.0) * eta_d


def project_soc_over_lease_kwh(
    soc_kwh: float,
    p_kw: float,
    lease_ttl_h: float,
    eta_c: float,
    eta_d: float,
) -> float:
    """Project SoC forward over a command's full lease duration (`lease_ttl_h`) under a CONSTANT
    commanded power `p_kw` (+charge / -discharge, same sign convention as `soc_step`), ignoring
    self-discharge (a lease-duration guardian projection is deliberately conservative/simple, not a
    full forward simulation -- 02a S6's G-01 formula: `soc - p_cmd * lease_ttl_h / eta_d >= reserve`
    for discharge, symmetrically `soc + p_cmd * lease_ttl_h * eta_c <= e_kwh` for charge). Used by
    G-01-ENERGY to check the guardian's OWN independently-read SoC holds for the command's WHOLE lease,
    not just the instant it is issued.
    """
    p_c = max(p_kw, 0.0)
    p_d = max(-p_kw, 0.0)
    return soc_kwh + eta_c * p_c * lease_ttl_h - (p_d * lease_ttl_h) / eta_d


def hub_capability(soc_kwh: float, params: HubParams) -> tuple[float, float]:
    """Return (max_discharge_kw, max_charge_kw) available right now, honoring the reserve floor (K1)
    and the hub's power limit. Both values are >= 0.
    """
    usable_above_reserve = max(soc_kwh - params.r_kwh, 0.0)
    headroom_to_full = max(params.e_kwh - soc_kwh, 0.0)
    max_discharge_kw = params.p_kw if usable_above_reserve > 0 else 0.0
    max_charge_kw = params.p_kw if headroom_to_full > 0 else 0.0
    return max_discharge_kw, max_charge_kw


@dataclass(frozen=True, slots=True)
class BankParams:
    kva_rating: float
    reserve_kva: float = 0.0


def bank_capability(member_hub_discharge_kw: list[float], bank: BankParams) -> float:
    """Sum of member hubs' available discharge, capped by the bank's kVA rating minus its reserve.
    A simplifying assumption (kW ~= kVA at unity power factor) matching the MVP-S sim's scope.
    """
    total_kw = sum(member_hub_discharge_kw)
    ceiling = max(bank.kva_rating - bank.reserve_kva, 0.0)
    return min(total_kw, ceiling)


def recharge_headroom(bank_load_kva: float, bank: BankParams) -> float:
    """How much additional charging (kW) the bank can absorb before hitting its kVA rating.
    This is THE function both `allocator` (planning-time) and `guardian` G-03 (independent,
    signing-time, on its own independently-read SCADA input) call -- 02b S12's "one formula, two
    data paths" fix. Never re-derive this elsewhere.
    """
    return max(bank.kva_rating - bank.reserve_kva - bank_load_kva, 0.0)


def apply_ramp_limit(
    prev_p_kw: float,
    target_p_kw: float,
    dt_s: float,
    ramp_kw_per_s: float,
    *,
    ramp_down_kw_per_s: float | None = None,
) -> float:
    """Clamp a requested setpoint change to the allowed ramp rate over dt_s seconds (K4/G-04/G-05/G-06).

    `ramp_kw_per_s` bounds the upward rate of change (and, when `ramp_down_kw_per_s` is omitted, the
    downward rate too -- the common symmetric case). Pass `ramp_down_kw_per_s` to bound a faster/slower
    downward rate independently (ALLOC-03: some loops, e.g. the DIST_DEFERRAL PI, ramp down and up at
    different named rates). A non-positive rate for a given direction holds the setpoint (no movement
    in that direction), matching the original symmetric behavior when only `ramp_kw_per_s` is given.
    """
    ramp_up = ramp_kw_per_s
    ramp_down = ramp_down_kw_per_s if ramp_down_kw_per_s is not None else ramp_kw_per_s
    delta = target_p_kw - prev_p_kw
    if delta > 0:
        if ramp_up <= 0:
            return prev_p_kw
        return prev_p_kw + min(delta, ramp_up * dt_s)
    if delta < 0:
        if ramp_down <= 0:
            return prev_p_kw
        return prev_p_kw - min(-delta, ramp_down * dt_s)
    return target_p_kw
