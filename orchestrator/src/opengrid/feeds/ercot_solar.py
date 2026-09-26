"""D-28 source 2 read path: ERCOT's published solar production as a share of ERCOT load, for one area and
one hour, read from `og.feed_obs` through feeds' public `window()` interface.

The rule and the ratio are `opengrid.core.solar_share`'s (the ONE owner: `ercot_solar_share_of_load`, and
`solar_share(..., ercot_solar_share=...)` for the D-28 priority); this module only fetches the two feed
values it needs. It is I/O, so it lives in `opengrid.feeds`, not `core`.

Stored feed identifiers (all hourly MW, interval-start `ts`):
- system solar: product `np4-737-cd`, series `solar_actual` (STPPF forecast: `solar_forecast`);
- regional solar: product `np4-745-cd`, series `solar_actual_<Region>` / `solar_forecast_<Region>`,
  Region in `opengrid.feeds.normalize.SOLAR_REGIONS`;
- load: product `np6-345-cd`, series `total` (system) or a weather zone (`coast` ... `west`).

ERCOT's solar regions are a different partition from its load weather zones, so there is no built-in zone
-> region mapping: system-wide (`area="total"`) always works, and a weather zone works only when
`[feeds.ercot.solar_share_zones]` maps it to the solar region(s) that cover it. An unmapped zone returns
`None` rather than a guessed share.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from opengrid.core.models.platform import FeedObs
from opengrid.core.solar_share import ercot_solar_share_of_load
from opengrid.feeds.normalize import (
    SOLAR_ACTUAL_SERIES,
    SOLAR_FORECAST_SERIES,
    SOLAR_REGIONS,
    solar_actual_series,
    solar_forecast_series,
)

#: NP6-345-CD's system-total load series (`opengrid.feeds.normalize._LOAD_ZONE_COLUMNS`).
SYSTEM_AREA = "total"

WindowFn = Callable[[str, datetime, datetime], Awaitable[list[FeedObs]]]
SolarBasis = Literal["actual", "forecast"]


@dataclass(frozen=True, slots=True)
class ErcotSolarReading:
    """ERCOT solar share of load for one area and one hour. `share` is
    `core.solar_share.ercot_solar_share_of_load` (in [0, 1]); `solar_basis` says whether the solar side is
    measured (`actual`) or ERCOT's STPPF forecast (no actual posted for that hour yet)."""

    area: str
    hour_start: datetime
    solar_mw: Decimal
    load_mw: Decimal
    share: Decimal
    solar_basis: SolarBasis


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


def solar_regions_for_area(area: str, zone_regions: Mapping[str, Sequence[str]]) -> tuple[str, ...] | None:
    """The solar regions summed for `area`: none (system-wide series) for `SYSTEM_AREA`, the configured
    list for a mapped weather zone, `None` for an unmapped one."""
    if area == SYSTEM_AREA:
        return ()
    regions = zone_regions.get(area)
    return tuple(regions) if regions else None


async def _hour_value(window: WindowFn, series: str, t0: datetime) -> Decimal | None:
    rows = await window(series, t0, t0 + timedelta(hours=1))
    return Decimal(str(rows[-1].value)) if rows else None


async def _solar_mw(
    window: WindowFn, regions: tuple[str, ...], t0: datetime
) -> tuple[Decimal, SolarBasis] | None:
    """Sum of solar output over `regions` (system-wide when empty) for the hour at `t0`: actuals when
    every region has one, else the STPPF forecast when every region has that; `None` otherwise (a partial
    sum would understate the share)."""
    actual_series = [solar_actual_series(r) for r in regions] or [SOLAR_ACTUAL_SERIES]
    forecast_series = [solar_forecast_series(r) for r in regions] or [SOLAR_FORECAST_SERIES]
    basis: SolarBasis
    for basis, names in (("actual", actual_series), ("forecast", forecast_series)):
        values = [await _hour_value(window, name, t0) for name in names]
        present = [v for v in values if v is not None]
        if len(present) == len(values):
            return sum(present, Decimal(0)), basis
    return None


async def ercot_solar_share(
    area: str,
    at: datetime,
    *,
    zone_regions: Mapping[str, Sequence[str]] | None = None,
    window: WindowFn | None = None,
) -> ErcotSolarReading | None:
    """ERCOT solar share of load for `area` (`"total"` = system-wide, or an NP6-345-CD weather zone mapped
    in `zone_regions`, i.e. `zone_regions_from_config(cfg)`) in the hour containing `at` -- the value to
    pass as `core.solar_share.solar_share(..., ercot_solar_share=reading.share)`.

    `None` when not measurable: an unmapped zone, no solar actual or forecast for that hour, or no posted
    actual load (NP6-345-CD is actuals only, so a future hour has no load yet). `window` defaults to
    `opengrid.feeds.window` (feeds' public read interface, set up in og-feeds); tests pass a fake.
    """
    if window is None:
        from opengrid import feeds  # the package's public read interface; imported lazily (no import cycle)

        window = feeds.window
    regions = solar_regions_for_area(area, zone_regions or {})
    if regions is None:
        return None
    t0 = hour_start(at)
    solar = await _solar_mw(window, regions, t0)
    if solar is None:
        return None
    load_mw = await _hour_value(window, area, t0)
    if load_mw is None:
        return None
    solar_mw, basis = solar
    share = ercot_solar_share_of_load(solar_mw, load_mw)
    if share is None:
        return None
    return ErcotSolarReading(
        area=area, hour_start=t0, solar_mw=solar_mw, load_mw=load_mw, share=share, solar_basis=basis
    )
