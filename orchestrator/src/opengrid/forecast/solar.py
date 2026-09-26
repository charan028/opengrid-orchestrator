"""Solar-driven intraday price shape (decision log D-24, no I/O): a pure adjustment layered on top of
the quantile-persistence baseline in `quantiles.py`, so a load zone's price forecast reflects the
midday dip and evening ramp a growing solar fleet drives into intraday prices, not only that zone's own
price history's shape.

Two sources of solar-intensity signal, preferred in this order -- both optional; `apply_solar_shape`
returns its input unchanged when neither is available for a target interval (BUILD.md S5a "no silent
fallbacks": callers can tell shaping happened by comparing output to input, not by trusting a hidden
default):

1. ERCOT's own NP4-737-CD solar forecast/actual (`feeds.normalize.SOLAR_FORECAST_SERIES`/
   `SOLAR_ACTUAL_SERIES`), normalized against that calendar day's own peak so no nameplate-capacity
   constant is needed -- the more accurate signal where it's fresh, since it is ERCOT's own published
   forecast.
2. NWS cloud cover (0-100%) combined with a clear-sky diurnal shape (a simplified, non-seasonal
   sunrise/sunset window, not a solar-position/ephemeris calculation) as a proxy for irradiance, for
   when/where ERCOT solar data is missing or stale.

ERCOT publishes solar actual/forecast system-wide only in the MVP-S Public API product set
(`05-integrations-guide.md` lists no per-load-zone solar product), so the same system-wide signal is
shared across every zone -- a documented simplification. A zone's own weather/price history still
drives its quantile-persistence baseline; solar only reshapes that baseline's intraday profile.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from opengrid.core.timeutil import to_market_tz

#: Simplified, non-seasonal daylight window (market-local hours) the clear-sky shape spans. Real
#: sunrise/sunset times shift by roughly two hours across the year at ERCOT's latitudes; a fixed window
#: is a documented approximation, not a solar-position calculation (out of scope for MVP-S).
SUNRISE_HOUR = 6.5
SUNSET_HOUR = 19.5

#: How many hours after sunset the evening-ramp weight decays back to 0.
EVENING_RAMP_WINDOW_H = 3.0

#: How much the midday solar peak can pull the price band down / the evening ramp can push it up, as a
#: fraction of that slot's own quantile-persistence band width (p90 - p10) -- not a fraction of price
#: level, since ERCOT prices go negative during high-renewable periods and a level-based multiplier
#: would push a negative price the wrong way. Named constants, not magic numbers (BUILD.md S5a).
DEFAULT_MIDDAY_DIP_FRACTION = 0.60
DEFAULT_EVENING_RAMP_FRACTION = 0.80


def clear_sky_shape(target_start_utc: datetime) -> float:
    """[0, 1] clear-sky solar shape for `target_start_utc`'s local time of day: 0 outside
    `[SUNRISE_HOUR, SUNSET_HOUR]`, a half-sine peaking at solar noon inside it. A simplified stand-in
    for a full solar-position irradiance model -- see module docstring."""
    local = to_market_tz(target_start_utc)
    hour = local.hour + local.minute / 60.0
    if hour <= SUNRISE_HOUR or hour >= SUNSET_HOUR:
        return 0.0
    daylight_span = SUNSET_HOUR - SUNRISE_HOUR
    return math.sin(math.pi * (hour - SUNRISE_HOUR) / daylight_span)


def evening_ramp_weight(target_start_utc: datetime) -> float:
    """[0, 1] weight peaking right at sunset and decaying linearly to 0 over `EVENING_RAMP_WINDOW_H`
    hours after it; 0 before sunset or once that window has passed."""
    local = to_market_tz(target_start_utc)
    hour = local.hour + local.minute / 60.0
    hours_after_sunset = hour - SUNSET_HOUR
    if hours_after_sunset < 0 or hours_after_sunset > EVENING_RAMP_WINDOW_H:
        return 0.0
    return 1.0 - hours_after_sunset / EVENING_RAMP_WINDOW_H


def solar_intensity_from_ercot(
    solar_mw_by_ts: dict[datetime, float], target_start_utc: datetime
) -> float | None:
    """[0, 1] fraction of that **local calendar day's own peak** ERCOT system-wide solar output/forecast
    (`solar_mw_by_ts`, already restricted by the caller to one series -- actual or forecast) at
    `target_start_utc`. Scaling to the day's own peak avoids needing a nameplate-capacity constant that
    would otherwise go stale as the fleet grows. Returns `None` (never `0.0`) when no value covers that
    calendar day at all, so callers can tell "no ERCOT signal" apart from "ERCOT says zero solar" (a
    real night hour, or an outage)."""
    day = to_market_tz(target_start_utc).date()
    same_day_values = [value for ts, value in solar_mw_by_ts.items() if to_market_tz(ts).date() == day]
    if not same_day_values:
        return None
    peak = max(same_day_values)
    if peak <= 0:
        return 0.0
    value_at_target = solar_mw_by_ts.get(target_start_utc)
    if value_at_target is None:
        return None
    return max(0.0, min(1.0, value_at_target / peak))


def solar_intensity_from_cloud_cover(target_start_utc: datetime, cloud_cover_pct: float | None) -> float:
    """[0, 1] clear-sky shape scaled down by cloud cover (0-100%; `None` is treated as clear, since the
    diurnal shape alone is still a better daylight estimate than no signal at all)."""
    shape = clear_sky_shape(target_start_utc)
    if cloud_cover_pct is None:
        return shape
    clear_fraction = 1.0 - max(0.0, min(100.0, cloud_cover_pct)) / 100.0
    return shape * clear_fraction


@dataclass(frozen=True, slots=True)
class SolarShapeInput:
    """One target interval's resolved solar signal, however it was obtained. `apply_solar_shape` only
    reads `intensity`/`ramp_weight`; `source` is carried through purely for tracing/debugging."""

    intensity: float  # [0, 1], midday solar strength
    ramp_weight: float  # [0, 1], evening-ramp strength
    source: str  # "ercot" | "nws_cloud_cover"


def resolve_solar_shape_input(
    target_start_utc: datetime,
    *,
    solar_mw_by_ts: dict[datetime, float] | None = None,
    cloud_cover_pct: float | None = None,
) -> SolarShapeInput | None:
    """Pick the best available intensity signal for `target_start_utc`: ERCOT's own solar
    forecast/actual, then NWS cloud cover -- per the module docstring's preference order. Returns `None`
    when neither is available for this interval, so `apply_solar_shape` leaves the quantile-persistence
    baseline untouched rather than shaping every interval off a geometric assumption with no real
    weather/solar data behind it (BUILD.md S5a "no silent fallbacks": shaping only happens where the
    task's "where data exists" condition is actually met). The evening-ramp weight is always the
    clear-sky sunset-based one: ERCOT's product publishes no ramp-specific signal, and cloud cover has
    no bearing on when the sun sets."""
    ramp = evening_ramp_weight(target_start_utc)
    if solar_mw_by_ts:
        ercot_intensity = solar_intensity_from_ercot(solar_mw_by_ts, target_start_utc)
        if ercot_intensity is not None:
            return SolarShapeInput(intensity=ercot_intensity, ramp_weight=ramp, source="ercot")
    if cloud_cover_pct is not None:
        return SolarShapeInput(
            intensity=solar_intensity_from_cloud_cover(target_start_utc, cloud_cover_pct),
            ramp_weight=ramp,
            source="nws_cloud_cover",
        )
    return None


def apply_solar_shape(
    quantiles: tuple[float, float, float],
    shape: SolarShapeInput | None,
    *,
    midday_dip_fraction: float = DEFAULT_MIDDAY_DIP_FRACTION,
    evening_ramp_fraction: float = DEFAULT_EVENING_RAMP_FRACTION,
) -> tuple[float, float, float]:
    """Reshape a quantile-persistence triple for D-24's solar-driven intraday shape: shift the whole
    band down during midday solar strength and up during the post-sunset evening ramp, by an amount
    proportional to that slot's own band width (`p90 - p10`) -- shifting rather than scaling by price
    level, since ERCOT prices go negative during high-renewable periods and a level-based multiplier
    would push a negative price the wrong way. Shifting by a constant preserves `p10 <= p50 <= p90`.

    `shape=None` (no solar/cloud-cover signal at all for this interval) returns `quantiles` unchanged --
    the plain same-slot/day-type persistence baseline (`quantiles.py`) stands on its own where no D-24
    input is available, per the task's "replace where data exists, fall back otherwise" (BUILD.md S5a
    "no silent fallbacks": this is an explicit, visible identity, not an accidental one)."""
    if shape is None:
        return quantiles
    p10, p50, p90 = quantiles
    band_width = max(0.0, p90 - p10)
    shift = band_width * (evening_ramp_fraction * shape.ramp_weight - midday_dip_fraction * shape.intensity)
    return p10 + shift, p50 + shift, p90 + shift
