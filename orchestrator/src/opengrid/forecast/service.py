"""Orchestration for `forecast` (02b S3): pulls history through a `HistoryProvider` (structurally
`opengrid.feeds`), runs the pure quantile math in `quantiles.py`, and persists through a
`ForecastBackend`. Kept independent of the module-level singleton in `__init__.py` so it is directly
unit-testable with fakes (BUILD.md S5a, task instruction "use fakes for other modules").
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from opengrid.core.timeutil import floor_to_interval
from opengrid.forecast.backend import ForecastBackend, HistoryProvider
from opengrid.forecast.models import ForecastKind, ForecastRow, ScenarioPoint
from opengrid.forecast.quantiles import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_RESOLUTION_MIN,
    FIRM_POOLED,
    MIN_SLOT_SAMPLES,
    InsufficientHistoryError,
    compute_slot_quantiles,
)
from opengrid.forecast.solar import apply_solar_shape, resolve_solar_shape_input
from opengrid.platform.config import Config

#: `og.feed_obs` series names `feeds.normalize` writes D-24's two solar-shape inputs under. Named here
#: as plain strings rather than imported from `opengrid.feeds` -- `forecast` deliberately has no import
#: dependency on `feeds` (see `backend.HistoryProvider`'s docstring: the two packages meet only through
#: that structural Protocol, wired together by `feeds/main.py`, so either can move to its own process
#: without a redesign). Must match `feeds.normalize.SOLAR_FORECAST_SERIES` / the NWS sky-cover series
#: name exactly; `test_service.py` pins both against the real constants to catch drift.
SOLAR_FORECAST_SERIES = "solar_forecast"
NWS_SKY_COVER_SERIES = "sky_cover"

logger = logging.getLogger(__name__)

#: Last-resort fallback only, used when neither `[forecast].price_series` nor `[fleet].zones` is
#: configured. `feeds.ercot.PRODUCT_PATHS["np6-905-cd"]` queries `settlementPointType=LZ` (load zones),
#: so the price series keys `feed_obs` actually carries are the same load-zone codes as `[fleet].zones`
#: (e.g. `LZ_NORTH`) -- never a hub code -- which is why `price_series` below falls back to
#: `fleet.zones` first, matching `load_series`'s existing fallback (README.md's canonical list).
DEFAULT_PRICE_SERIES: tuple[str, ...] = (
    "LZ_NORTH",
    "LZ_SOUTH",
    "LZ_HOUSTON",
    "LZ_WEST",
    "LZ_AEN",
    "LZ_CPS",
    "LZ_LCRA",
    "LZ_RAYBN",
)

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
    # Build phase, 2026-09-26 (market-model-two-markets.md S3a): Austin Energy and CPS Energy are
    # regulated municipal utilities, not ERCOT competitive-area load zones, so they have no ERCOT
    # weather-zone definition of their own -- this correspondence is APPROXIMATE (per the docstring
    # above, doubly so here), based on rough geography only, pending an authoritative mapping from
    # ERCOT/the utilities/public GIS (tracked in `integrations/regulated-utilities-austin-cps-2026-09.md`).
    # Only takes effect once "LZ_AEN"/"LZ_CPS" are added to `[fleet].zones`/`[forecast].load_series`.
    "LZ_AEN": ("southC",),
    "LZ_CPS": ("southern", "southC"),
    # D-24 build phase: LCRA (Lower Colorado River Authority) serves Central Texas around Austin, same
    # rough geography as AEN/CPS above -> "southC". Rayburn Country EC serves North Texas around
    # McKinney/Sherman (Collin/Fannin counties) -> "northC" rather than the Panhandle "north" zone.
    # Equally approximate, same caveat as AEN/CPS: no authoritative ERCOT mapping for either, pending
    # `integrations/regulated-utilities-austin-cps-2026-09.md`-style documentation for these two.
    "LZ_LCRA": ("southC",),
    "LZ_RAYBN": ("northC",),
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
    firm_rule = FirmRule(
        min_samples=int(cfg.get("forecast.min_samples_firm", MIN_SLOT_SAMPLES)),
        pool_day_types_when_short=bool(cfg.get("forecast.pool_day_types_when_short", True)),
        pooled_max_spread=_optional_float(cfg.get("forecast.pooled_max_spread")),
    )

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

    # D-24 solar-shape inputs, fetched once (ERCOT solar and NWS cloud cover are both system-wide, not
    # per-zone -- see forecast/solar.py's module docstring) and passed to every price series below,
    # rather than re-querying the same window once per zone.
    horizon_end = horizon_start + timedelta(minutes=steps * resolution_min)
    solar_mw_by_ts = await _window_by_ts(history, SOLAR_FORECAST_SERIES, horizon_start, horizon_end)
    cloud_cover_by_ts = await _window_by_ts(history, NWS_SKY_COVER_SERIES, horizon_start, horizon_end)

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
                firm_rule=firm_rule,
                solar_mw_by_ts=solar_mw_by_ts,
                cloud_cover_by_ts=cloud_cover_by_ts,
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
                firm_rule=firm_rule,
            )
        )

    if rows:
        await backend.upsert_rows(rows)
    return rows


async def _window_by_ts(
    history: HistoryProvider, series: str, t0: datetime, t1: datetime
) -> dict[datetime, float]:
    """`{ts: value}` for one `feed_obs` series over `[t0, t1)` -- the shape `forecast.solar` wants for
    both the ERCOT solar signal and the NWS cloud-cover signal. Empty on `LookupError` or no rows: both
    are optional D-24 inputs (BUILD.md K7 "degrade, don't trip" -- missing solar/cloud data must not
    stop price forecasting, only fall it back to plain quantile persistence)."""
    try:
        obs = await history.window(series, t0, t1)
    except LookupError:
        return {}
    return {row.ts: row.value for row in obs}


async def _compute_series(
    history: HistoryProvider,
    *,
    kind: ForecastKind,
    history_series_keys: tuple[str, ...],
    series_key: str,
    horizon_start: datetime,
    steps: int,
    resolution_min: int,
    firm_rule: FirmRule | None = None,
    solar_mw_by_ts: dict[datetime, float] | None = None,
    cloud_cover_by_ts: dict[datetime, float] | None = None,
) -> list[ForecastRow]:
    """Compute one forecast series (persisted under `series_key`) from one or more underlying
    `feed_obs` series (`history_series_keys`) -- for `price` these are always the same single key; for
    `load` they are the load zone's mapped weather zones (`DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE`), summed
    per timestamp so a load zone's forecast reflects its whole footprint, not one arbitrary zone.

    `solar_mw_by_ts`/`cloud_cover_by_ts` (only meaningful for `kind="price"`; D-24) reshape each price
    slot's quantile-persistence baseline for that interval's solar strength -- see `forecast.solar`.
    Left as plain persistence (untouched) for any interval neither signal covers."""
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
    rule = firm_rule or FirmRule()

    rows: list[ForecastRow] = []
    basis_counts: Counter[str] = Counter()
    for step in range(steps):
        target_start = horizon_start + timedelta(minutes=step * resolution_min)
        try:
            slot = compute_slot_quantiles(
                pairs,
                target_start,
                stale=stale,
                resolution_min=resolution_min,
                min_slot_samples=rule.min_samples,
                pool_day_types_when_short=rule.pool_day_types_when_short,
                pooled_max_spread=rule.pooled_max_spread,
            )
        except InsufficientHistoryError:
            # No silent fallback (BUILD.md S5a): a slot forecast has to be skipped, log it so it is
            # visible in health/alerts rather than quietly missing from `og.forecast`.
            logger.warning(
                "forecast: no history available for series, skipping slot",
                extra={"series_key": series_key, "kind": kind, "interval_start": target_start.isoformat()},
            )
            continue
        basis_counts[slot.basis] += 1
        p10, p50, p90 = slot.p10, slot.p50, slot.p90
        if kind == "price":
            shape = resolve_solar_shape_input(
                target_start,
                solar_mw_by_ts=solar_mw_by_ts,
                cloud_cover_pct=(cloud_cover_by_ts or {}).get(target_start),
            )
            p10, p50, p90 = apply_solar_shape((p10, p50, p90), shape)
        rows.append(
            ForecastRow(
                series_key=series_key,
                kind=kind,
                interval_start_utc=target_start,
                horizon_step=step,
                p10=p10,
                p50=p50,
                p90=p90,
                firm_fitness=slot.firm_fitness,
            )
        )
    if basis_counts["POOLED"]:
        # Audit trail for the short-history relaxation: these slots are FIRM_OK in `og.forecast` on
        # weekday+weekend pooled samples, not the strict same-day-type rule.
        logger.warning(
            "forecast: slots firm on pooled day types (short history)",
            extra={
                "reason_code": FIRM_POOLED,
                "series_key": series_key,
                "kind": kind,
                "pooled_slots": basis_counts["POOLED"],
                "strict_slots": basis_counts["STRICT"],
                "fallback_slots": basis_counts["FALLBACK"],
                "stale": stale,
            },
        )
    return rows


@dataclass(frozen=True, slots=True)
class FirmRule:
    """`[forecast]` firm-fitness knobs: `min_samples_firm` (default 3, 02b S3), the short-history
    `pool_day_types_when_short` relaxation (default on) and its optional `pooled_max_spread` cap on a
    pooled slot's P90-P10 spread (series units; unset = no cap -- 02b S3 defines no dispersion
    threshold for the strict rule either)."""

    min_samples: int = MIN_SLOT_SAMPLES
    pool_day_types_when_short: bool = True
    pooled_max_spread: float | None = None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"[forecast].pooled_max_spread must be a number, got {value!r}")


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
