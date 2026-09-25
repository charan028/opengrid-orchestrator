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
) -> float:
    """Clamp a requested setpoint change to the allowed ramp rate over dt_s seconds (K4/G-04/G-05/G-06).
    Positive ramp_kw_per_s bounds the rate of change in either direction.
    """
    if ramp_kw_per_s <= 0:
        return prev_p_kw
    max_delta = ramp_kw_per_s * dt_s
    delta = target_p_kw - prev_p_kw
    if delta > max_delta:
        return prev_p_kw + max_delta
    if delta < -max_delta:
        return prev_p_kw - max_delta
    return target_p_kw
