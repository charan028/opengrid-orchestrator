"""Synthetic (b) data mode: realistic diurnal price/load/wind/solar curves
with noise, seeded for reproducibility.

All functions are pure given (timestamp, seed) so repeated calls for the same
instant return the same base value (before anomaly overrides are applied).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np

HUBS = ["HB_HOUSTON", "HB_HUBAVG", "HB_NORTH", "HB_PAN", "HB_SOUTH", "HB_WEST"]
# Load-zone settlement points (settlementPointType=LZ), matching
# 02b-mvp-s-spec-platform.md's `fleet.zones`.
LOAD_ZONES = ["LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST"]
WEATHER_ZONES = ["coast", "east", "farWest", "north", "northC", "southern", "southC", "west"]
# interfaces/http/market-api.md ancillaryType codes for NP4-188-CD.
AS_SERVICES = ["REGUP", "REGDN", "RRS", "NSPIN", "ECRS"]

_BASE_PRICE = 28.0  # $/MWh baseline
_BASE_LOAD_ZONE = 4200.0  # MW baseline per weather zone
_BASE_WIND = 9000.0  # MW system-wide
_BASE_SOLAR = 6500.0  # MW system-wide (daytime peak)


def _rng_for(seed: int, key: str, ts: datetime) -> np.random.Generator:
    """A generator whose state depends only on (seed, key, minute bucket) so
    the same instant always yields the same noise sample."""
    bucket = int(ts.replace(tzinfo=UTC).timestamp() // 60)
    return np.random.default_rng(abs(hash((seed, key, bucket))) % (2**32))


def _hour_frac(ts: datetime) -> float:
    return ts.hour + ts.minute / 60.0 + ts.second / 3600.0


def price_usd_per_mwh(ts: datetime, seed: int, hub: str = "HB_HUBAVG") -> float:
    """Diurnal price curve: morning/evening peaks, midday trough (solar), plus
    small hub-to-hub spread and gaussian noise."""
    h = _hour_frac(ts)
    diurnal = (
        1.0
        + 0.55 * math.exp(-((h - 8.0) ** 2) / 6.0)
        + 0.85 * math.exp(-((h - 19.0) ** 2) / 5.0)
        - 0.25 * math.exp(-((h - 13.0) ** 2) / 10.0)
    )
    hub_spread = {
        "HB_WEST": -2.0,
        "HB_HOUSTON": 3.0,
        "HB_NORTH": -0.5,
        "HB_SOUTH": 1.5,
        "HB_PAN": -3.0,
        "LZ_WEST": -2.0,
        "LZ_HOUSTON": 3.0,
        "LZ_NORTH": -0.5,
        "LZ_SOUTH": 1.5,
    }.get(hub, 0.0)
    rng = _rng_for(seed, f"price:{hub}", ts)
    noise = rng.normal(0.0, 2.0)
    return round(max(0.5, _BASE_PRICE * diurnal + hub_spread + noise), 2)


def load_mw(ts: datetime, seed: int, zone: str) -> float:
    h = _hour_frac(ts)
    diurnal = 1.0 + 0.30 * math.exp(-((h - 17.5) ** 2) / 8.0) - 0.12 * math.exp(-((h - 4.0) ** 2) / 6.0)
    zone_scale = {
        "coast": 1.6,
        "east": 0.5,
        "farWest": 0.4,
        "north": 0.6,
        "northC": 1.8,
        "southern": 0.7,
        "southC": 1.1,
        "west": 0.5,
    }.get(zone, 1.0)
    rng = _rng_for(seed, f"load:{zone}", ts)
    noise = rng.normal(0.0, 60.0)
    return round(max(50.0, _BASE_LOAD_ZONE * zone_scale * diurnal + noise), 1)


def wind_mw(ts: datetime, seed: int) -> tuple[float, float]:
    """Returns (actual, forecast) system-wide wind, MW. Wind is stronger overnight."""
    h = _hour_frac(ts)
    diurnal = 1.0 + 0.35 * math.exp(-((h - 3.0) ** 2) / 10.0) - 0.25 * math.exp(-((h - 15.0) ** 2) / 12.0)
    rng = _rng_for(seed, "wind", ts)
    noise = rng.normal(0.0, 350.0)
    actual = max(0.0, _BASE_WIND * diurnal + noise)
    forecast = max(0.0, actual + rng.normal(0.0, 250.0))
    return round(actual, 1), round(forecast, 1)


def solar_mw(ts: datetime, seed: int) -> tuple[float, float]:
    """Returns (actual, forecast) system-wide solar, MW. Zero outside daylight."""
    h = _hour_frac(ts)
    if h < 6.5 or h > 20.0:
        return 0.0, 0.0
    diurnal = math.exp(-((h - 13.5) ** 2) / 8.0)
    rng = _rng_for(seed, "solar", ts)
    noise = rng.normal(0.0, 200.0)
    actual = max(0.0, _BASE_SOLAR * diurnal + noise)
    forecast = max(0.0, actual + rng.normal(0.0, 150.0))
    return round(actual, 1), round(forecast, 1)


def as_clearing_price(ts: datetime, seed: int, service: str) -> float:
    base = {"REGUP": 12.0, "REGDN": 8.0, "RRS": 10.0, "NSPIN": 4.0, "ECRS": 15.0}.get(service, 8.0)
    rng = _rng_for(seed, f"as:{service}:{ts.date().isoformat()}", ts)
    return round(max(0.1, base + rng.normal(0.0, base * 0.3)), 2)


def nws_forecast_period(ts: datetime, seed: int, hour_offset: int) -> dict:
    h = (_hour_frac(ts) + hour_offset) % 24
    base_temp_c = 22.0 + 8.0 * math.sin((h - 6.0) / 24.0 * 2 * math.pi)
    rng = _rng_for(seed, "nws", ts.replace(minute=0, second=0) if hasattr(ts, "replace") else ts)
    temp_c = base_temp_c + rng.normal(0.0, 1.5) + hour_offset * 0.01
    dewpoint_c = temp_c - 6.0 + rng.normal(0.0, 1.0)
    sky_cover_pct = max(0, min(100, int(30 + rng.normal(0, 20))))
    return {
        "temperature_c": round(temp_c, 1),
        "dewpoint_c": round(dewpoint_c, 1),
        "sky_cover_pct": sky_cover_pct,
        "wind_kph": round(max(0.0, 12.0 + rng.normal(0, 6.0)), 1),
        "short_forecast": "Mostly Sunny"
        if sky_cover_pct < 40
        else ("Partly Cloudy" if sky_cover_pct < 75 else "Cloudy"),
    }
