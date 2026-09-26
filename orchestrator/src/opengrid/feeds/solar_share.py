"""Solar share of load (owner decision D-28, "solar share measured"): the ONE read helper the optimizer and
settle use to ask "what fraction of load did (or will) solar cover in this zone, this hour?".

It lives in `opengrid.feeds` because it is a read over `og.feed_obs` through feeds' public `window()`
interface -- the same place every other consumer reads market data from (`opengrid.feeds` docstring);
`opengrid.core` holds no I/O. The ratio itself is `solar_share_ratio`, a pure function.

Inputs (all hourly, MW, `og.feed_obs`):
- solar: NP4-737-CD's system-wide `solar_actual`/`solar_forecast`, or NP4-745-CD's per-region
  `solar_actual_<Region>`/`solar_forecast_<Region>` (`opengrid.feeds.normalize.solar_actual_series`);
- load: NP6-345-CD's actual load per weather zone (`coast` ... `west`) and its `total` series.

ERCOT's solar regions (CenterWest, FarWest, ...) are a different partition from its load weather zones,
so there is no built-in zone -> region mapping: system-wide (`zone="total"`) always works, and a weather
zone works only when `[feeds.ercot.solar_share_zones]` maps it to the solar region(s) that cover it. An
unmapped zone returns `None` rather than a guessed share.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.normalize import (
    SOLAR_ACTUAL_SERIES,
    SOLAR_FORECAST_SERIES,
    SOLAR_REGIONS,
    solar_actual_series,
    solar_forecast_series,
)

#: NP6-345-CD's system-total load series (`opengrid.feeds.normalize._LOAD_ZONE_COLUMNS`).
SYSTEM_ZONE = "total"

WindowFn = Callable[[str, datetime, datetime], Awaitable[list[FeedObs]]]
SolarBasis = Literal["actual", "forecast"]


@dataclass(frozen=True, slots=True)
class SolarShare:
    """Solar share of load for one zone and one hour. `share` is `solar_mw / load_mw` and may exceed 1.0
    for a zone whose solar regions export into the rest of the system; `solar_basis` says whether the
    solar side is measured (`actual`) or ERCOT's STPPF forecast (the hour had no actual posted yet)."""

    zone: str
    hour_start: datetime
    solar_mw: float
    load_mw: float
    share: float
    solar_basis: SolarBasis


def solar_share_ratio(solar_mw: float, load_mw: float) -> float | None:
    """`solar_mw / load_mw`, with solar floored at 0 (ERCOT posts small negative night-time values for
    auxiliary draw). `None` when load is not positive -- there is no share of zero load."""
    if load_mw <= 0.0:
        return None
    return max(solar_mw, 0.0) / load_mw


def hour_start(at: datetime) -> datetime:
    """The start of the feed hour containing `at` (ERCOT hourly rows are stamped at their interval start,
    and America/Chicago's offsets are whole hours, so a UTC floor is the same hour boundary)."""
    return at.replace(minute=0, second=0, microsecond=0)


def zone_regions_from_config(cfg: object) -> dict[str, tuple[str, ...]]:
    """`[feeds.ercot.solar_share_zones]` (`weather_zone = ["SolarRegion", ...]`) as a mapping. Takes `cfg`
    duck-typed (anything with `.get(dotted_key, default)`, i.e. `opengrid.platform.config.Config`). An
    unknown solar region name is a config error (`ValueError`), never silently dropped."""
    raw = cfg.get("feeds.ercot.solar_share_zones", {}) if hasattr(cfg, "get") else {}
    if not isinstance(raw, Mapping):
        raise ValueError("[feeds.ercot.solar_share_zones] must be a table of zone = [regions]")
    mapping: dict[str, tuple[str, ...]] = {}
    for zone, regions in raw.items():
        names = tuple(str(r) for r in regions)
        unknown = [r for r in names if r not in SOLAR_REGIONS]
        if unknown:
            raise ValueError(f"[feeds.ercot.solar_share_zones].{zone}: unknown solar region(s) {unknown}")
        mapping[str(zone)] = names
    return mapping


def solar_regions_for_zone(zone: str, zone_regions: Mapping[str, Sequence[str]]) -> tuple[str, ...] | None:
    """The solar regions summed for `zone`: none (system-wide series) for `SYSTEM_ZONE`, the configured
    list for a mapped weather zone, `None` for an unmapped one."""
    if zone == SYSTEM_ZONE:
        return ()
    regions = zone_regions.get(zone)
    return tuple(regions) if regions else None


async def _hour_value(window: WindowFn, series: str, t0: datetime) -> float | None:
    rows = await window(series, t0, t0 + timedelta(hours=1))
    return rows[-1].value if rows else None


async def _solar_mw(
    window: WindowFn, regions: tuple[str, ...], t0: datetime
) -> tuple[float, SolarBasis] | None:
    """Sum of solar output over `regions` (system-wide when empty) for the hour at `t0`: actuals when
    every region has one, else the STPPF forecast when every region has that; `None` otherwise (a partial
    sum would understate the share)."""
    actual_series = [solar_actual_series(r) for r in regions] or [SOLAR_ACTUAL_SERIES]
    forecast_series = [solar_forecast_series(r) for r in regions] or [SOLAR_FORECAST_SERIES]
    basis: SolarBasis
    for basis, names in (("actual", actual_series), ("forecast", forecast_series)):
        values = [await _hour_value(window, name, t0) for name in names]
        if all(v is not None for v in values):
            return sum(v for v in values if v is not None), basis
    return None


async def solar_share_of_load(
    zone: str,
    at: datetime,
    *,
    zone_regions: Mapping[str, Sequence[str]] | None = None,
    window: WindowFn | None = None,
) -> SolarShare | None:
    """Solar share of load for `zone` (`"total"` = system-wide, or an NP6-345-CD weather zone mapped in
    `zone_regions`, i.e. `[feeds.ercot.solar_share_zones]`) in the hour containing `at`.

    Returns `None` when the answer is not measurable: an unmapped zone, no solar actual or forecast for
    that hour, or no posted actual load (NP6-345-CD is actuals only, so a future hour has no load yet).
    `window` defaults to `opengrid.feeds.window` (feeds' public read interface); tests pass a fake.
    """
    if window is None:
        from opengrid import feeds  # the package's public read interface; imported lazily (no import cycle)

        window = feeds.window
    regions = solar_regions_for_zone(zone, zone_regions or {})
    if regions is None:
        return None
    t0 = hour_start(at)
    solar = await _solar_mw(window, regions, t0)
    if solar is None:
        return None
    load_mw = await _hour_value(window, zone, t0)
    if load_mw is None:
        return None
    solar_mw, basis = solar
    share = solar_share_ratio(solar_mw, load_mw)
    if share is None:
        return None
    return SolarShare(
        zone=zone, hour_start=t0, solar_mw=solar_mw, load_mw=load_mw, share=share, solar_basis=basis
    )
