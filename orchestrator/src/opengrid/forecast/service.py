"""Orchestration for `forecast` (02b S3): pulls history through a `HistoryProvider` (structurally
`opengrid.feeds`), runs the pure quantile math in `quantiles.py`, and persists through a
`ForecastBackend`. Kept independent of the module-level singleton in `__init__.py` so it is directly
unit-testable with fakes (BUILD.md S5a, task instruction "use fakes for other modules").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from opengrid.core.timeutil import floor_to_interval
from opengrid.forecast.backend import ForecastBackend, HistoryProvider
from opengrid.forecast.models import ForecastKind, ForecastRow, ScenarioPoint
from opengrid.forecast.quantiles import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_RESOLUTION_MIN,
    InsufficientHistoryError,
    compute_slot_quantiles,
)
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

#: Last-resort fallback only, used when neither `[forecast].price_series` nor `[fleet].zones` is
#: configured. `feeds.ercot.PRODUCT_PATHS["np6-905-cd"]` queries `settlementPointType=LZ` (load zones),
#: so the price series keys `feed_obs` actually carries are the same load-zone codes as `[fleet].zones`
#: (e.g. `LZ_NORTH`) -- never a hub code -- which is why `price_series` below falls back to
#: `fleet.zones` first, matching `load_series`'s existing fallback (README.md's canonical list).
DEFAULT_PRICE_SERIES: tuple[str, ...] = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")

#: 02a S3 "P10 = low, P50 = mid, P90 = high", weights per the stub docstring / 02a plan.scenario_set.
SCENARIO_WEIGHTS: dict[str, float] = {"P10": 0.25, "P50": 0.5, "P90": 0.25}

#: Approximate mapping from each `[fleet].zones` **load-zone** code (`LZ_NORTH`/`LZ_SOUTH`/
#: `LZ_HOUSTON`/`LZ_WEST`) to the ERCOT **weather-zone** columns `feeds.normalize.
#: ercot_load_to_feed_obs` actually writes to `og.feed_obs.series` for `np6-345-cd`
#: (`feeds/normalize.py`'s `_LOAD_ZONE_COLUMNS`: `coast`, `east`, `farWest`, `north`, `northC`,
#: `southern`, `southC`, `west`, `total`).
#:
#: ERCOT's weather zones and load zones are two *different* partitions of the grid -- NP6-345-CD
#: reports actual system load by weather zone, while NP6-905-CD prices (and `[fleet].zones`) are by
#: load zone -- so this correspondence is necessarily approximate, not an authoritative ERCOT mapping,
#: and is documented as such rather than presented as exact (BUILD.md S5a "no silent fallbacks"). A
#: load zone's forecast load is the *sum* of its mapped weather zones' actual load. Override via
#: `[forecast].weather_zones_by_load_zone` (a `{load_zone = [weather_zone, ...]}` table) if a
#: deployment needs a different correspondence.
#:
#: This is the fix for the bug where `load_series` defaulted to `[fleet].zones` (`LZ_*`) and queried
#: `history.window("LZ_NORTH", ...)` directly: `feeds.normalize` never writes a `feed_obs` row under
#: `series="LZ_NORTH"` for `np6-345-cd` (only under the weather-zone names below), so every load
#: forecast slot silently found zero history and fell straight to `NOT_FOR_FIRM`/skipped, with no error
#: -- the same "wrong series key -> silently zero data" class of bug this module's `DEFAULT_PRICE_SERIES`
#: docstring already documents for price.
DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE: dict[str, tuple[str, ...]] = {
    "LZ_HOUSTON": ("coast",),
    "LZ_NORTH": ("north", "northC"),
    "LZ_SOUTH": ("southern", "southC"),
    "LZ_WEST": ("west", "farWest"),
}


async def compute_and_persist(
    cfg: Config,
    history: HistoryProvider,
    backend: ForecastBackend,
    *,
    now: datetime | None = None,
) -> list[ForecastRow]:
    """Recompute the full horizon for every configured price/load series and upsert the result
    (02b S3: "recomputed every 15 minutes... and on demand"). Returns the rows written, for callers
    that want to log/trace the cycle."""
    now = now or datetime.now(UTC)
    resolution_min = int(cfg.get("forecast.resolution_min", DEFAULT_RESOLUTION_MIN))
    horizon_hours = int(cfg.get("forecast.horizon_hours", 24))
    horizon_start = floor_to_interval(now, resolution_min)
    steps = (horizon_hours * 60) // resolution_min

    price_series: list[str] = list(
        cfg.get("forecast.price_series") or cfg.get("fleet.zones", list(DEFAULT_PRICE_SERIES))
    )
    load_series: list[str] = list(cfg.get("forecast.load_series") or cfg.get("fleet.zones", []))
    weather_zone_map: dict[str, tuple[str, ...]] = {
        load_zone: tuple(weather_zones)
        for load_zone, weather_zones in cfg.get(
            "forecast.weather_zones_by_load_zone", DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE
        ).items()
    }

    rows: list[ForecastRow] = []
    for series_key in price_series:
        rows.extend(
            await _compute_series(
                history,
                kind="price",
                history_series_keys=(series_key,),
                series_key=series_key,
                horizon_start=horizon_start,
                steps=steps,
                resolution_min=resolution_min,
            )
        )
    for load_zone in load_series:
        weather_zones = weather_zone_map.get(load_zone)
        if weather_zones is None:
            # No documented weather-zone correspondence for this load zone: fall back to reading the
            # load-zone code directly (the pre-fix behaviour), but say so -- this almost certainly
            # yields zero history rather than pretend the mapping is complete (BUILD.md S5a "no silent
            # fallbacks").
            logger.warning(
                "forecast: no weather-zone mapping for load zone, reading it as a feed_obs series"
                " directly (likely yields no history)",
                extra={"load_zone": load_zone},
            )
            weather_zones = (load_zone,)
        rows.extend(
            await _compute_series(
                history,
                kind="load",
                history_series_keys=weather_zones,
                series_key=load_zone,
                horizon_start=horizon_start,
                steps=steps,
                resolution_min=resolution_min,
            )
        )

    if rows:
        await backend.upsert_rows(rows)
    return rows


async def _compute_series(
    history: HistoryProvider,
    *,
    kind: ForecastKind,
    history_series_keys: tuple[str, ...],
    series_key: str,
    horizon_start: datetime,
    steps: int,
    resolution_min: int,
) -> list[ForecastRow]:
    """Compute one forecast series (persisted under `series_key`) from one or more underlying
    `feed_obs` series (`history_series_keys`) -- for `price` these are always the same single key; for
    `load` they are the load zone's mapped weather zones (`DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE`), summed
    per timestamp so a load zone's forecast reflects its whole footprint, not one arbitrary zone."""
    stale = False
    for key in history_series_keys:
        try:
            latest_obs = await history.latest(key)
            if latest_obs.quality == "STALE":
                stale = True
        except LookupError:
            # Never observed: no live-freshness signal exists yet. Treat conservatively as stale so
            # any slot that *can* be computed (via the diurnal fallback) is still flagged NOT_FOR_FIRM.
            stale = True

    lookback_start = horizon_start - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    totals: dict[datetime, float] = {}
    for key in history_series_keys:
        try:
            window_obs = await history.window(key, lookback_start, horizon_start)
        except LookupError:
            window_obs = []
        for obs in window_obs:
            totals[obs.ts] = totals.get(obs.ts, 0.0) + obs.value
    pairs = sorted(totals.items())

    rows: list[ForecastRow] = []
    for step in range(steps):
        target_start = horizon_start + timedelta(minutes=step * resolution_min)
        try:
            slot = compute_slot_quantiles(pairs, target_start, stale=stale, resolution_min=resolution_min)
        except InsufficientHistoryError:
            # No silent fallback (BUILD.md S5a): a slot forecast has to be skipped, log it so it is
            # visible in health/alerts rather than quietly missing from `og.forecast`.
            logger.warning(
                "forecast: no history available for series, skipping slot",
                extra={"series_key": series_key, "kind": kind, "interval_start": target_start.isoformat()},
            )
            continue
        rows.append(
            ForecastRow(
                series_key=series_key,
                kind=kind,
                interval_start_utc=target_start,
                horizon_step=step,
                p10=slot.p10,
                p50=slot.p50,
                p90=slot.p90,
                firm_fitness=slot.firm_fitness,
            )
        )
    return rows


_SCENARIO_NAMES: tuple[Literal["P10", "P50", "P90"], ...] = ("P10", "P50", "P90")


def rows_to_scenario_points(rows: list[ForecastRow]) -> list[ScenarioPoint]:
    """02a S3 3-scenario expansion: each persisted quantile triple becomes three weighted points."""
    points: list[ScenarioPoint] = []
    for row in rows:
        for scenario_name, value in zip(_SCENARIO_NAMES, (row.p10, row.p50, row.p90), strict=True):
            points.append(
                ScenarioPoint(
                    scenario=scenario_name,
                    probability=SCENARIO_WEIGHTS[scenario_name],
                    interval_start=row.interval_start_utc,
                    value=value,
                    series_key=row.series_key,
                    kind=row.kind,
                )
            )
    return points
