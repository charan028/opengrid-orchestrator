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

    rows: list[ForecastRow] = []
    for kind, series_keys in (("price", price_series), ("load", load_series)):
        for series_key in series_keys:
            rows.extend(
                await _compute_series(
                    history,
                    kind=kind,  # type: ignore[arg-type]
                    series_key=series_key,
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
    series_key: str,
    horizon_start: datetime,
    steps: int,
    resolution_min: int,
) -> list[ForecastRow]:
    try:
        latest_obs = await history.latest(series_key)
        stale = latest_obs.quality == "STALE"
    except LookupError:
        # Never observed: no live-freshness signal exists yet. Treat conservatively as stale so any
        # slot that *can* be computed (via the diurnal fallback) is still flagged NOT_FOR_FIRM.
        stale = True

    lookback_start = horizon_start - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    try:
        window_obs = await history.window(series_key, lookback_start, horizon_start)
    except LookupError:
        window_obs = []
    pairs = [(obs.ts, obs.value) for obs in window_obs]

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
