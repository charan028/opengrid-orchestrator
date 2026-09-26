"""ogsim.fleet.household -- per-hub home load model (02b §4 fleet twin).

Diurnal (time-of-day) load + noise, optional PV generation. Feeds
`net_home_load_kw` (+consuming / -PV surplus) that competes with commanded
setpoints for the battery in `ogsim.fleet.physics`. Vectorized over numpy
arrays of per-hub phase offsets so the whole fleet's home load is one
array op per tick.
"""

from __future__ import annotations

import numpy as np

SECONDS_PER_DAY = 86400.0
BASE_LOAD_KW = 0.6
PEAK_LOAD_KW = 2.4
EVENING_PEAK_HOUR = 19.0
PV_PEAK_KW = 3.5
SOLAR_NOON_HOUR = 13.0
NOISE_STD_KW = 0.15


def diurnal_load_kw(hour_of_day: np.ndarray, noise: np.ndarray) -> np.ndarray:
    """Home electrical load (kW, always >= 0): a base load plus an evening
    peak (double-hump-ish via a squared cosine bump) plus per-hub noise."""
    phase = (hour_of_day - EVENING_PEAK_HOUR) / 24.0 * 2.0 * np.pi
    bump = np.maximum(np.cos(phase), 0.0) ** 2
    load = BASE_LOAD_KW + (PEAK_LOAD_KW - BASE_LOAD_KW) * bump + noise
    return np.maximum(load, 0.0)


def pv_generation_kw(hour_of_day: np.ndarray, pv_capacity_kw: np.ndarray) -> np.ndarray:
    """PV output (kW, >= 0): a daylight-only cosine bump around solar noon."""
    phase = (hour_of_day - SOLAR_NOON_HOUR) / 24.0 * 2.0 * np.pi
    daylight = np.maximum(np.cos(phase), 0.0)
    return pv_capacity_kw * daylight**1.5


def hour_of_day_for(epoch_seconds: float, hub_phase_offsets_s: np.ndarray) -> np.ndarray:
    """Each hub's local hour-of-day (0..24) at `epoch_seconds`, offset by its own small
    `hub_phase_offsets_s` so 2,000 hubs don't peak in perfect lockstep (fleet diversity)."""
    seconds_of_day = np.mod(epoch_seconds + hub_phase_offsets_s, SECONDS_PER_DAY)
    return seconds_of_day / 3600.0


def load_and_pv_kw(
    hour_of_day: np.ndarray, pv_capacity_kw: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """`(home_load_kw, pv_kw)` separately (both >= 0) -- the telemetry fields for F2's discharge-flow
    limits (09-optimizer-dispatcher-update.md S1.9, G11: "Hub telemetry has no meter net power, PV,
    cell temperature..."). `net_home_load_kw` below is `home_load_kw - pv_kw`, kept as its own function
    because `ogsim.fleet.runtime`'s physics step only ever needs the net figure."""
    noise = rng.normal(0.0, NOISE_STD_KW, size=hour_of_day.shape)
    load = diurnal_load_kw(hour_of_day, noise)
    pv = pv_generation_kw(hour_of_day, pv_capacity_kw)
    return load, pv


def net_home_load_kw(
    epoch_seconds: float,
    hub_phase_offsets_s: np.ndarray,
    pv_capacity_kw: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Net home load (+consuming / -PV surplus) for every hub at `epoch_seconds` -- `load_and_pv_kw`'s
    `home_load_kw - pv_kw`, the only figure `ogsim.fleet.physics`'s SoC step needs.

    `hub_phase_offsets_s` gives each hub its own small time offset so 2,000
    hubs don't peak in perfect lockstep (a light source of fleet diversity).
    """
    hour_of_day = hour_of_day_for(epoch_seconds, hub_phase_offsets_s)
    load, pv = load_and_pv_kw(hour_of_day, pv_capacity_kw, rng)
    return load - pv
