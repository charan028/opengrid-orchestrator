"""ogsim.fleet.physics -- vectorized hub SoC physics.

Implements the canonical SoC step from
`docs/orchestrator/07-delivery/02b-mvp-s-spec-platform.md` §4.2:

    e[t+1] = e[t] + eta_c * p_c * dt - (dt / eta_d) * p_d - l * dt

with p_c/p_d the charge/discharge components (kW) of a single net power
`p_kw` (+charge / -discharge), `l` the self-discharge rate (kWh/h), and a
reserve/power/energy-aware setpoint clip so a commanded (market) setpoint
never pushes SoC below the homeowner reserve floor or above usable
capacity, while unconditioned home load (self-consumption) may still draw
the battery down toward (not below) zero.

All functions accept numpy arrays (vectorized for the 2,000/10,000-hub
tick) or plain floats (used directly by unit tests); numpy broadcasts
scalars the same way.
"""

from __future__ import annotations

import numpy as np


def soc_step[ArrayOrFloat: (float, np.ndarray)](
    soc_kwh: ArrayOrFloat,
    p_kw: ArrayOrFloat,
    eta_c: ArrayOrFloat,
    eta_d: ArrayOrFloat,
    dt_s: float,
    self_discharge_kwh_per_h: ArrayOrFloat,
) -> ArrayOrFloat:
    """One SoC tick for net power `p_kw` (+charge / -discharge) over `dt_s`."""
    dt_h = dt_s / 3600.0
    p_charge = np.maximum(p_kw, 0.0)
    p_discharge = np.maximum(-p_kw, 0.0)
    delta = eta_c * p_charge * dt_h - (dt_h / eta_d) * p_discharge - self_discharge_kwh_per_h * dt_h
    return soc_kwh + delta


def clip_commanded_setpoint[ArrayOrFloat: (float, np.ndarray)](
    soc_kwh: ArrayOrFloat,
    p_kw_setpoint: ArrayOrFloat,
    home_net_kw: ArrayOrFloat,
    p_kw_limit: ArrayOrFloat,
    e_kwh: ArrayOrFloat,
    r_kwh: ArrayOrFloat,
    eta_c: ArrayOrFloat,
    eta_d: ArrayOrFloat,
    dt_s: float,
) -> ArrayOrFloat:
    """Clips a market-commanded setpoint to the hub's own P/E/reserve limits.

    Home load (`home_net_kw`, +consuming) is served unconditionally up to
    the inverter's physical headroom and is not itself reserve-limited
    (the reserve exists *for* home use); only the commanded setpoint is
    clipped so it cannot discharge the battery below `r_kwh` or charge it
    above `e_kwh`. Returns the applied commanded power (kW, same sign
    convention as `p_kw_setpoint`).
    """
    dt_h = dt_s / 3600.0
    home_charge = np.maximum(-home_net_kw, 0.0)  # PV surplus charges the battery
    home_discharge = np.maximum(home_net_kw, 0.0)  # home load discharges it
    p_headroom_charge = np.maximum(p_kw_limit - home_charge, 0.0)
    p_headroom_discharge = np.maximum(p_kw_limit - home_discharge, 0.0)

    setpoint = np.clip(p_kw_setpoint, -p_headroom_discharge, p_headroom_charge)

    available_above_reserve_kwh = np.maximum(soc_kwh - r_kwh, 0.0)
    max_discharge_kw = np.where(dt_h > 0, available_above_reserve_kwh * eta_d / np.maximum(dt_h, 1e-9), 0.0)
    headroom_to_full_kwh = np.maximum(e_kwh - soc_kwh, 0.0)
    max_charge_kw = np.where(dt_h > 0, headroom_to_full_kwh / (eta_c * np.maximum(dt_h, 1e-9)), 0.0)

    return np.clip(setpoint, -max_discharge_kw, max_charge_kw)


def tick[ArrayOrFloat: (float, np.ndarray)](
    soc_kwh: ArrayOrFloat,
    p_kw_commanded: ArrayOrFloat,
    home_net_kw: ArrayOrFloat,
    p_kw_limit: ArrayOrFloat,
    e_kwh: ArrayOrFloat,
    r_kwh: ArrayOrFloat,
    eta_c: ArrayOrFloat,
    eta_d: ArrayOrFloat,
    dt_s: float,
    self_discharge_kwh_per_h: ArrayOrFloat,
) -> tuple[ArrayOrFloat, ArrayOrFloat]:
    """Full hub tick: clips the commanded setpoint, combines it with home
    load, and steps SoC. Returns `(new_soc_kwh, applied_commanded_p_kw)`."""
    applied = clip_commanded_setpoint(
        soc_kwh, p_kw_commanded, home_net_kw, p_kw_limit, e_kwh, r_kwh, eta_c, eta_d, dt_s
    )
    home_power = -home_net_kw  # +consuming home load -> negative (discharging) battery power
    total_p_kw = applied + home_power
    new_soc = soc_step(soc_kwh, total_p_kw, eta_c, eta_d, dt_s, self_discharge_kwh_per_h)
    new_soc = np.clip(new_soc, 0.0, e_kwh)
    return new_soc, applied
