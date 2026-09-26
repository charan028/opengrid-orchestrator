"""Envelope/limit checks: reserve, hub P, bank kVA, ramp, feeder ceiling, fleet ramp cap.

Single owner per 02b S12. `allocator` calls these at planning time (S1-S7); `guardian` calls the SAME
functions at signing time (G-01..G-06) on its own independently-read inputs. Every check returns
(ok, reason_code) rather than raising, so callers can accumulate multiple violations per cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from opengrid.core import reasons
from opengrid.core.physics import BankParams, HubParams, project_soc_over_lease_kwh, recharge_headroom


@dataclass(frozen=True, slots=True)
class LimitResult:
    ok: bool
    reason: str | None = None

    @staticmethod
    def passed() -> LimitResult:
        return LimitResult(True, None)

    @staticmethod
    def failed(reason: str) -> LimitResult:
        return LimitResult(False, reason)


def check_reserve_floor(soc_kwh: float, params: HubParams, *, margin_pct: float = 0.01) -> LimitResult:
    """G-01/K1: SoC must stay >= reserve + margin for the command duration. margin_pct is a fraction
    of the usable energy capacity (default 1%, per 02a G-01's "reserve + 1%")."""
    margin_kwh = params.e_kwh * margin_pct
    if soc_kwh < params.r_kwh + margin_kwh:
        return LimitResult.failed(reasons.R_RESERVE_FLOOR)
    return LimitResult.passed()


def check_reserve_floor_over_lease(
    soc_kwh: float,
    p_kw: float,
    lease_ttl_h: float,
    params: HubParams,
    *,
    margin_pct: float = 0.01,
) -> LimitResult:
    """G-01-ENERGY/K1: project the hub's reported SoC across the command's FULL lease duration (not
    just the instant it is issued) and check it still clears reserve + margin throughout (discharge)
    or never overfills above `e_kwh` (charge). A command whose power is well within the instantaneous
    G-01 check can still drain a hub below reserve *before its lease expires* if the hub does not hold
    enough energy above reserve for the whole lease -- capacity (kW) alone is not sufficient (user
    requirement: energy above reserve must be checked continuously, not just power headroom).
    """
    projected = project_soc_over_lease_kwh(soc_kwh, p_kw, lease_ttl_h, params.eta_c, params.eta_d)
    if p_kw < 0:  # discharging
        margin_kwh = params.e_kwh * margin_pct
        if projected < params.r_kwh + margin_kwh:
            return LimitResult.failed(reasons.R_RESERVE_FLOOR_LEASE)
    elif p_kw > 0:  # charging
        if projected > params.e_kwh + 1e-9:
            return LimitResult.failed(reasons.R_CHARGE_CEILING_LEASE)
    return LimitResult.passed()


def check_hub_power(p_kw: float, params: HubParams, *, inverter_cap_kw: float = 11.0) -> LimitResult:
    """G-02/K4: |P| <= min(inverter cap, per-hub cap)."""
    cap = min(inverter_cap_kw, params.p_kw)
    if abs(p_kw) > cap + 1e-9:
        return LimitResult.failed(reasons.R_HUB_POWER_LIMIT)
    return LimitResult.passed()


def check_bank_kva(
    bank_load_kva: float,
    additional_kw: float,
    bank: BankParams,
    *,
    loading_pct: float = 0.95,
) -> LimitResult:
    """G-03/K4: net bank loading (existing + proposed) must stay within loading_pct of rating,
    net of reserve. Calls the same recharge_headroom the allocator uses (02b S12)."""
    headroom = recharge_headroom(bank_load_kva, bank)
    allowed = headroom * loading_pct if additional_kw >= 0 else float("inf")
    if additional_kw > 0 and additional_kw > allowed + 1e-9:
        return LimitResult.failed(reasons.R_BANK_KVA_LIMIT)
    net = bank_load_kva + additional_kw
    if net > bank.kva_rating * loading_pct + 1e-9:
        return LimitResult.failed(reasons.R_BANK_KVA_LIMIT)
    return LimitResult.passed()


def check_hub_ramp(
    prev_p_kw: float,
    target_p_kw: float,
    dt_s: float,
    ramp_kw_per_s: float,
) -> LimitResult:
    """G-04/K4: hub/firm ramp bound."""
    if ramp_kw_per_s <= 0:
        return LimitResult.passed()
    if abs(target_p_kw - prev_p_kw) > ramp_kw_per_s * dt_s + 1e-9:
        return LimitResult.failed(reasons.R_HUB_RAMP_LIMIT)
    return LimitResult.passed()


def check_fleet_ramp_cap(
    fleet_delta_kw: float,
    dt_s: float,
    *,
    discretionary_cap_kw_per_min: float = 50_000.0,
    non_firm_cap_kw_per_min: float = 10_000.0,
    is_firm_event: bool = False,
) -> LimitResult:
    """G-05/K4: fleet-wide ramp cap for synchronized steps. Firm events use the discretionary cap;
    non-firm (market/AS) steps use the tighter cap."""
    cap_per_min = discretionary_cap_kw_per_min if is_firm_event else non_firm_cap_kw_per_min
    cap_kw = cap_per_min * (dt_s / 60.0)
    if abs(fleet_delta_kw) > cap_kw + 1e-9:
        return LimitResult.failed(reasons.R_FLEET_RAMP_CAP)
    return LimitResult.passed()


def check_feeder_ramp_ceiling(
    feeder_delta_kw: float,
    dt_s: float,
    feeder_ceiling_kw_per_min: float,
    *,
    is_firm_event: bool = False,
) -> LimitResult:
    """G-06/K4: per-feeder/substation ramp ceiling for firm events, independent of the fleet-wide cap."""
    if not is_firm_event:
        return LimitResult.passed()
    cap_kw = feeder_ceiling_kw_per_min * (dt_s / 60.0)
    if abs(feeder_delta_kw) > cap_kw + 1e-9:
        return LimitResult.failed(reasons.R_FEEDER_RAMP_CEILING)
    return LimitResult.passed()


def check_one_buyer(reservations_kw: list[float], capability_kw: float) -> LimitResult:
    """K2: sum of reservations against a hub/bank/interval must not exceed capability."""
    total = sum(reservations_kw)
    if total > capability_kw + 1e-9:
        return LimitResult.failed(reasons.R_ONE_BUYER_EXCEEDED)
    return LimitResult.passed()


def check_commitment_lock(
    new_kw: float,
    frozen_kw: float,
    prior_kw: float,
    reason_code: str | None,
    *,
    allowed_release_reasons: frozenset[str] = reasons.COMMIT_LOCK_OVERRIDE_REASONS,
    as_release_enabled: bool = False,
) -> LimitResult:
    """K13/G-19: a committed allocation may never be reduced below min(frozen, prior) without an
    allowed reason code. R-AS-RELEASE is only allowed when as_release_enabled (default off)."""
    floor = min(frozen_kw, prior_kw)
    if new_kw >= floor - 1e-9:
        return LimitResult.passed()
    if reason_code in allowed_release_reasons:
        return LimitResult.passed()
    if reason_code == reasons.R_AS_RELEASE and as_release_enabled:
        return LimitResult.passed()
    return LimitResult.failed(reasons.R_COMMIT_LOCK_VIOLATION)
