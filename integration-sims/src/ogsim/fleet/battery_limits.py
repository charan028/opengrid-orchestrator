"""ogsim.fleet.battery_limits -- F1 SoC/temperature discharge-flow derating and cell temperature
(`docs/orchestrator/07-delivery/09-optimizer-dispatcher-update.md` S1.9 F1, owner requirement).

Not enforced anywhere yet (09's S0.2 gap table, G8/G11): the allocator/guardian give every hub its
full rated power regardless of SoC or temperature, and the hub telemetry has no meter net power, PV,
cell temperature, BMS limits or peak budget to check them against. This module is the SIM side of
closing G11 -- it publishes `p_dis_max_kw`/`p_ch_max_kw` (the hub's own reported BMS derating, per
S1.9's "where the hub reports its BMS limit, it binds") so a future allocator/guardian change can
start enforcing F1 against real telemetry instead of the current "no derating" default.

Piecewise-linear curves (S1.9 F1, "confirm with Base; an LFP chemistry is assumed"): SoC is the
fraction of nameplate capacity (floor 0.20, per K1). All four curves are pure, vectorized (numpy or
scalar), and independent of any process state.
"""

from __future__ import annotations

import numpy as np

#: f_dis_SoC (S1.9 F1): 0.30 at SoC 0.20, rising linearly to 1.0 at SoC 0.30, 1.0 above. Below 0.20
#: is never reached in practice (K1 stops discharge at the reserve floor) but clamps to 0.30, not 0,
#: so a momentary reading just under the floor (measurement noise) does not divide by zero or spike.
_DISCHARGE_SOC_BREAKPOINTS: tuple[tuple[float, float], ...] = ((0.20, 0.30), (0.30, 1.0))
#: f_ch_SoC: 1.0 up to 0.90, then linear taper to 0 at 1.00.
_CHARGE_SOC_BREAKPOINTS: tuple[tuple[float, float], ...] = ((0.0, 1.0), (0.90, 1.0), (1.00, 0.0))
#: f_dis_T (cell degC): -10->0, 0->0.3, 10->0.8, 15-35->1.0, 45->0.7, 50->0.4, 55->0.
_DISCHARGE_TEMP_BREAKPOINTS: tuple[tuple[float, float], ...] = (
    (-10.0, 0.0),
    (0.0, 0.3),
    (10.0, 0.8),
    (15.0, 1.0),
    (35.0, 1.0),
    (45.0, 0.7),
    (50.0, 0.4),
    (55.0, 0.0),
)
#: f_ch_T (cell degC): <0->0, 0-10->0.3, 10-15->0.6, 15-40->1.0, 45->0.5, 50->0.
_CHARGE_TEMP_BREAKPOINTS: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (10.0, 0.3),
    (15.0, 0.6),
    (40.0, 1.0),
    (45.0, 0.5),
    (50.0, 0.0),
)


def _piecewise_linear[ArrayOrFloat: (float, np.ndarray)](
    x: ArrayOrFloat, breakpoints: tuple[tuple[float, float], ...]
) -> ArrayOrFloat:
    """Linear interpolation through `breakpoints` (sorted by x), clamped flat outside the range --
    `np.interp` already does exactly this (`left`/`right` default to the first/last y value)."""
    xs = [p[0] for p in breakpoints]
    ys = [p[1] for p in breakpoints]
    return np.interp(x, xs, ys)


def discharge_soc_derate[ArrayOrFloat: (float, np.ndarray)](soc_frac: ArrayOrFloat) -> ArrayOrFloat:
    """f_dis_SoC(SoC): SoC as a fraction of nameplate (0..1)."""
    return _piecewise_linear(np.clip(soc_frac, 0.20, 1.0), _DISCHARGE_SOC_BREAKPOINTS)


def charge_soc_derate[ArrayOrFloat: (float, np.ndarray)](soc_frac: ArrayOrFloat) -> ArrayOrFloat:
    """f_ch_SoC(SoC): SoC as a fraction of nameplate (0..1)."""
    return _piecewise_linear(np.clip(soc_frac, 0.0, 1.0), _CHARGE_SOC_BREAKPOINTS)


def discharge_temp_derate[ArrayOrFloat: (float, np.ndarray)](cell_temp_c: ArrayOrFloat) -> ArrayOrFloat:
    """f_dis_T(cell °C)."""
    return _piecewise_linear(cell_temp_c, _DISCHARGE_TEMP_BREAKPOINTS)


def charge_temp_derate[ArrayOrFloat: (float, np.ndarray)](cell_temp_c: ArrayOrFloat) -> ArrayOrFloat:
    """f_ch_T(cell °C)."""
    return _piecewise_linear(cell_temp_c, _CHARGE_TEMP_BREAKPOINTS)


def p_dis_max_kw[ArrayOrFloat: (float, np.ndarray)](
    p_kw_limit: ArrayOrFloat, soc_frac: ArrayOrFloat, cell_temp_c: ArrayOrFloat
) -> ArrayOrFloat:
    """S1.9 F1: `P_dis_max,i = P_cont_i * f_dis_SoC(s_i) * f_dis_T(T_i)` (the BMS-limit `min(...)`
    term does not apply here -- this sim IS the BMS, there is no separate reported hardware limit to
    take a minimum against)."""
    return p_kw_limit * discharge_soc_derate(soc_frac) * discharge_temp_derate(cell_temp_c)


def p_ch_max_kw[ArrayOrFloat: (float, np.ndarray)](
    p_kw_limit: ArrayOrFloat, soc_frac: ArrayOrFloat, cell_temp_c: ArrayOrFloat
) -> ArrayOrFloat:
    """S1.9 F1, charge side: `P_ch_max,i = P_cont_i * f_ch_SoC(s_i) * f_ch_T(T_i)`."""
    return p_kw_limit * charge_soc_derate(soc_frac) * charge_temp_derate(cell_temp_c)


#: Ambient diurnal temperature model (illustrative only, not a weather feed): mean/amplitude/peak-hour
#: chosen to give a plausible ERCOT-territory daily swing. A "garage" installation offset (09 S1.2's
#: table: "NWS hourly ambient + garage offset (3 degC, calibrate)") and small self-heating from the
#: battery's own |p_kw| complete the model.
_AMBIENT_MEAN_C = 22.0
_AMBIENT_AMPLITUDE_C = 8.0
_AMBIENT_PEAK_HOUR = 15.0
GARAGE_OFFSET_C = 3.0
#: Self-heating: cell temperature rises with the magnitude of charge/discharge power, capped at this
#: many degrees C above ambient+garage at the hub's full rated power (illustrative I^2R proxy).
_SELF_HEATING_MAX_C = 6.0


def cell_temperature_c(
    hour_of_day: np.ndarray, p_kw_applied: np.ndarray, p_kw_limit: np.ndarray, noise: np.ndarray
) -> np.ndarray:
    """Per-hub cell temperature (°C): ambient diurnal + garage offset + self-heating proportional to
    `|p_kw_applied| / p_kw_limit` + per-hub noise. Illustrative telemetry for F1's derating inputs,
    not a calibrated thermal model -- 09 S1.2 OQ notes the real curves need a BMS datasheet."""
    ambient = _AMBIENT_MEAN_C + _AMBIENT_AMPLITUDE_C * np.cos(
        (hour_of_day - _AMBIENT_PEAK_HOUR) / 24.0 * 2.0 * np.pi
    )
    load_frac = np.where(p_kw_limit > 0, np.abs(p_kw_applied) / p_kw_limit, 0.0)
    self_heating = _SELF_HEATING_MAX_C * np.clip(load_frac, 0.0, 1.0)
    return ambient + GARAGE_OFFSET_C + self_heating + noise
