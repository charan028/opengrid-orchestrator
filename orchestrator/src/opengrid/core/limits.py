"""Envelope/limit checks: reserve, hub P, bank kVA, ramp, feeder ceiling, fleet ramp cap.

Single owner per 02b S12. `allocator` calls these at planning time (S1-S7); `guardian` calls the SAME
functions at signing time (G-01..G-06) on its own independently-read inputs. Every check returns
(ok, reason_code) rather than raising, so callers can accumulate multiple violations per cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from opengrid.core import reasons
from opengrid.core.physics import (
    DEFAULT_INVERTER_CAP_KW,
    BankParams,
    HubParams,
    project_soc_over_lease_kwh,
    recharge_headroom,
)


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


#: Base battery specs (2026-09-25): a dual-unit home is rated 20 kW continuous -- NOT 2 x the 11 kW
#: per-unit inverter cap (78.4 kWh / 20 kW, `opengrid.fleet.seed`'s dual-unit default).
DUAL_UNIT_RATED_KW = 20.0


def unit_rating_kw(units: int | None, *, inverter_cap_kw: float = DEFAULT_INVERTER_CAP_KW) -> float:
    """G-02: the continuous rating the home's installed units allow. Two units: min(2 x per-unit cap,
    `DUAL_UNIT_RATED_KW`). Anything else -- one unit, an unknown count (None) or an out-of-range value --
    fails closed to ONE unit, so a missing count can never lift the cap to a mis-seeded `p_kw`."""
    if units == 2:
        return min(2 * inverter_cap_kw, DUAL_UNIT_RATED_KW)
    return inverter_cap_kw


def continuous_power_kw(params: HubParams, *, inverter_cap_kw: float = DEFAULT_INVERTER_CAP_KW) -> float:
    """The home's continuous rated power P_cont: min(`params.p_kw`, `unit_rating_kw(params.units)`) --
    11 kW for a single-unit home, 20 kW for a dual-unit one. The seeded `p_kw` alone never sets the cap:
    a single-unit home mis-seeded at 20 kW is still held to 11 kW."""
    return min(params.p_kw, unit_rating_kw(params.units, inverter_cap_kw=inverter_cap_kw))


def check_hub_power(
    p_kw: float, params: HubParams, *, inverter_cap_kw: float = DEFAULT_INVERTER_CAP_KW
) -> LimitResult:
    """G-02/K4: |P| <= the home's continuous rated power (`continuous_power_kw`)."""
    if abs(p_kw) > continuous_power_kw(params, inverter_cap_kw=inverter_cap_kw) + 1e-9:
        return LimitResult.failed(reasons.R_HUB_POWER_LIMIT)
    return LimitResult.passed()


# --- F1: SoC- and temperature-dependent derating (09-optimizer-dispatcher-update.md S1.9) -------------
# Default curves, LFP assumed -- to be confirmed with Base. The ONE definition: the allocator's per-hub cap
# (DISPATCH) and the guardian's G-02 both call `derated_power_bounds_kw`.

#: Discharge SoC factor: 0.3 at the floor, rising 7 per unit of nameplate above it, 1.0 from floor + 0.1.
DERATE_DIS_SOC_AT_FLOOR = 0.3
DERATE_DIS_SOC_SLOPE = 7.0
#: Charge SoC taper: 1.0 up to this fraction of nameplate, linear to 0 at full.
DERATE_CH_SOC_KNEE = 0.90
#: Discharge temperature factor, piecewise-linear over cell °C (0 outside the ends).
DERATE_DIS_TEMP_POINTS: tuple[tuple[float, float], ...] = (
    (-10.0, 0.0),
    (0.0, 0.3),
    (10.0, 0.8),
    (15.0, 1.0),
    (35.0, 1.0),
    (45.0, 0.7),
    (50.0, 0.4),
    (55.0, 0.0),
)
#: Charge temperature factor: steps below 15 °C, full to 40 °C, linear to 0.5 at 45 and 0 at 50.
DERATE_CH_TEMP_STEPS: tuple[tuple[float, float], ...] = ((0.0, 0.3), (10.0, 0.6), (15.0, 1.0))
DERATE_CH_TEMP_HOT_POINTS: tuple[tuple[float, float], ...] = ((40.0, 1.0), (45.0, 0.5), (50.0, 0.0))
#: The temperature factor when the cell temperature is unknown or stale (09 S2.6; 0 = strict).
DERATE_UNKNOWN_TEMP_FACTOR = 0.5


def _interp(points: tuple[tuple[float, float], ...], x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in pairwise(points):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def derate_discharge_soc(soc_kwh: float, params: HubParams) -> float:
    if params.e_kwh <= 0 or soc_kwh <= params.r_kwh:
        return 0.0
    return min(1.0, DERATE_DIS_SOC_AT_FLOOR + DERATE_DIS_SOC_SLOPE * (soc_kwh - params.r_kwh) / params.e_kwh)


def derate_charge_soc(soc_kwh: float, params: HubParams) -> float:
    if params.e_kwh <= 0:
        return 0.0
    fraction = soc_kwh / params.e_kwh
    return max(0.0, min(1.0, (1.0 - fraction) / (1.0 - DERATE_CH_SOC_KNEE)))


def derate_discharge_temp(cell_temp_c: float) -> float:
    return max(0.0, _interp(DERATE_DIS_TEMP_POINTS, cell_temp_c))


def derate_charge_temp(cell_temp_c: float) -> float:
    if cell_temp_c < DERATE_CH_TEMP_STEPS[0][0]:
        return 0.0
    if cell_temp_c >= DERATE_CH_TEMP_HOT_POINTS[0][0]:
        return max(0.0, _interp(DERATE_CH_TEMP_HOT_POINTS, cell_temp_c))
    factor = 0.0
    for threshold, value in DERATE_CH_TEMP_STEPS:
        if cell_temp_c >= threshold:
            factor = value
    return factor


@dataclass(frozen=True, slots=True)
class PowerBounds:
    """Magnitudes (>= 0) of the most a hub may discharge and charge right now."""

    discharge_kw: float
    charge_kw: float


def derated_power_bounds_kw(
    params: HubParams,
    soc_kwh: float,
    cell_temp_c: float | None,
    *,
    bms_discharge_kw: float | None = None,
    bms_charge_kw: float | None = None,
    unknown_temp_factor: float = DERATE_UNKNOWN_TEMP_FACTOR,
    inverter_cap_kw: float = DEFAULT_INVERTER_CAP_KW,
    base_kw: float | None = None,
) -> PowerBounds:
    """F1/G-02: P_max(SoC, T) = min(P_base x f_SoC x f_T, BMS limit) per direction. `base_kw` defaults to
    the continuous rating (the peak is only ever granted through G-31). `cell_temp_c` None means unknown:
    `unknown_temp_factor` applies. A BMS limit of None drops that term (the curve alone applies)."""
    base = continuous_power_kw(params, inverter_cap_kw=inverter_cap_kw) if base_kw is None else base_kw
    if cell_temp_c is None:
        f_t_dis = f_t_ch = unknown_temp_factor
    else:
        f_t_dis, f_t_ch = derate_discharge_temp(cell_temp_c), derate_charge_temp(cell_temp_c)
    discharge = base * derate_discharge_soc(soc_kwh, params) * f_t_dis
    charge = base * derate_charge_soc(soc_kwh, params) * f_t_ch
    if bms_discharge_kw is not None:
        discharge = min(discharge, max(bms_discharge_kw, 0.0))
    if bms_charge_kw is not None:
        charge = min(charge, max(bms_charge_kw, 0.0))
    return PowerBounds(discharge_kw=max(discharge, 0.0), charge_kw=max(charge, 0.0))


def check_hub_power_derated(p_kw: float, bounds: PowerBounds) -> LimitResult:
    """G-02 (changed, 09 S2.6): -p <= discharge bound and p <= charge bound."""
    if -p_kw > bounds.discharge_kw + 1e-9 or p_kw > bounds.charge_kw + 1e-9:
        return LimitResult.failed(reasons.R_HUB_POWER_DERATED)
    return LimitResult.passed()


# --- F2: home load first, then the meter limits (G-26) --------------------------------------------------

#: Load-drop allowance over one lease (09 S2.6 G-26 default).
HOME_LOAD_DROP_KW = 0.5


def home_meter_setpoint_band_kw(
    net_load_low_kw: float,
    net_load_high_kw: float,
    export_limit_kw: float,
    service_kw: float,
    *,
    load_drop_kw: float = HOME_LOAD_DROP_KW,
) -> tuple[float, float]:
    """F2/G-26: the setpoint band (min, max) keeping the meter M = L + p within [-X_exp, S_svc] over the lease.
    `net_load_low_kw` is the net home load assumed on the export side (fail-closed: -PV rated), and
    `net_load_high_kw` on the import side (fail-closed: the service rating); the load may move by
    `load_drop_kw` during the lease."""
    lowest_p = -export_limit_kw - (net_load_low_kw - load_drop_kw)
    highest_p = service_kw - (net_load_high_kw + load_drop_kw)
    return lowest_p, highest_p


def check_home_meter(p_kw: float, band: tuple[float, float]) -> LimitResult:
    if p_kw < band[0] - 1e-9:
        return LimitResult.failed(reasons.R_HOME_EXPORT_LIMIT)
    if p_kw > band[1] + 1e-9:
        return LimitResult.failed(reasons.R_HOME_IMPORT_LIMIT)
    return LimitResult.passed()


# --- F3/F4: aggregate flows in both directions (G-27..G-30) ------------------------------------------


def band_excess_kw(flow_kw: float, lower_kw: float, upper_kw: float) -> float:
    """How far `flow_kw` lies outside [lower, upper] (0 inside)."""
    return max(lower_kw - flow_kw, flow_kw - upper_kw, 0.0)


def check_flow_band(
    now_kw: float,
    projected_kw: float,
    lower_kw: float,
    upper_kw: float,
    *,
    reverse_reason: str,
    forward_reason: str,
) -> LimitResult:
    """An aggregate flow (import-positive) must stay within [lower, upper]. Relief always passes: a change that
    does not move an already-violated flow further outside the band is never refused (as G-03)."""
    excess = band_excess_kw(projected_kw, lower_kw, upper_kw)
    if excess <= 1e-9 or excess <= band_excess_kw(now_kw, lower_kw, upper_kw) + 1e-9:
        return LimitResult.passed()
    return LimitResult.failed(reverse_reason if projected_kw < lower_kw else forward_reason)


# --- F5: sustained vs peak (G-31) -----------------------------------------------------------------------


def check_peak_power(
    p_kw: float,
    continuous_kw: float,
    *,
    peak_kw: float | None,
    tau_peak_s: float | None,
    lease_ttl_s: float,
    peak_budget_kws: float | None,
) -> LimitResult:
    """F5/G-31: above continuous only up to P_pk, for a lease no longer than tau_pk, within the hub's reported
    peak budget (kW*s). A missing P_pk means P_pk = P_cont and a missing budget means 0 (fail closed)."""
    magnitude = abs(p_kw)
    if magnitude <= continuous_kw + 1e-9:
        return LimitResult.passed()
    if peak_kw is None or tau_peak_s is None or peak_budget_kws is None:
        return LimitResult.failed(reasons.R_PEAK_POWER_LIMIT)
    over = magnitude - continuous_kw
    if (
        magnitude > peak_kw + 1e-9
        or lease_ttl_s > tau_peak_s + 1e-9
        or over * lease_ttl_s > peak_budget_kws + 1e-9
    ):
        return LimitResult.failed(reasons.R_PEAK_POWER_LIMIT)
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
    return check_feeder_ramp(
        feeder_delta_kw, dt_s, feeder_ceiling_kw_per_min, reason=reasons.R_FEEDER_RAMP_CEILING
    )


def check_feeder_ramp(
    feeder_delta_kw: float, dt_s: float, feeder_ceiling_kw_per_min: float, *, reason: str
) -> LimitResult:
    """|net change on one feeder in one step| <= its ramp ceiling pro rata (F6: firm via G-06, non-firm via
    G-32 -- a single large asset alone can exceed the fleet's non-firm cap on its own feeder)."""
    if abs(feeder_delta_kw) > feeder_ceiling_kw_per_min * (dt_s / 60.0) + 1e-9:
        return LimitResult.failed(reason)
    return LimitResult.passed()


def check_synchronized_step(
    gross_step_kw: float, dt_s: float, *, discretionary_cap_kw_per_min: float
) -> LimitResult:
    """G-05/K4 stagger (02a S6.1): the GROSS step sum(|delta p_i|) of every hub moved in one tick is at most
    the discretionary cap's share of that tick (cap / 30 per 2-s tick). Unlike the net fleet ramp, opposite
    moves do not cancel: a step of many hubs at once must be staggered across ticks."""
    if gross_step_kw > discretionary_cap_kw_per_min * (dt_s / 60.0) + 1e-9:
        return LimitResult.failed(reasons.R_SYNC_STEP_LIMIT)
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
