"""D-24 backtest: quantile-persistence baseline vs. the solar-shaped forecast (`solar.py`), evaluated
walk-forward against real `feed_obs` history -- read-only, no writes to `og.forecast` or anywhere else.

`backtest_series` is pure (plain `(ts, value)`/`{ts: value}` inputs, like `quantiles.py`) so it is fully
unit-testable without a database; `run_backtest` is the thin wrapper that pulls the last N days of real
history for one series through a `HistoryProvider` (structurally `opengrid.feeds`) and calls it. For
each sampled target timestamp within the backtest window, both methods are computed using *only* the
history strictly before that timestamp (a fair walk-forward comparison, not hindsight), then compared
against the real observed value at that timestamp.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from opengrid.forecast.backend import HistoryProvider
from opengrid.forecast.quantiles import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_RESOLUTION_MIN,
    InsufficientHistoryError,
    compute_slot_quantiles,
)
from opengrid.forecast.solar import apply_solar_shape, resolve_solar_shape_input

#: Default backtest window (task instruction: "last 7 days of feed_obs").
DEFAULT_BACKTEST_DAYS = 7

#: How often to sample a target timestamp within the backtest window. Every interval (15 min) would be
#: thorough but re-filters the whole history window per point; every 3h is ample to characterize
#: accuracy over a week without a slow O(points x history) sweep.
DEFAULT_SAMPLE_INTERVAL_MIN = 180


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Mean absolute error (MAE) of each method's P50 against the real observed value, over every
    target timestamp that had enough history to forecast at all. `improvement_pct > 0` means the
    solar-shaped forecast was more accurate than plain quantile persistence; `n_points == 0` means
    nothing in the window had enough history to score (report this, don't silently show 0% error)."""

    series_key: str
    n_points: int
    mae_persistence: float
    mae_shaped: float
    improvement_pct: float


def backtest_series(
    price_history: list[tuple[datetime, float]],
    target_timestamps: list[datetime],
    *,
    series_key: str,
    solar_mw_by_ts: dict[datetime, float] | None = None,
    cloud_cover_by_ts: dict[datetime, float] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    resolution_min: int = DEFAULT_RESOLUTION_MIN,
) -> BacktestResult:
    """Score both methods' P50 at each of `target_timestamps` against `price_history`'s own value at
    that instant (so `target_timestamps` should be a subset of `price_history`'s own timestamps -- the
    caller samples which ones from the real feed). A target with no matching observation, or with too
    little *prior* history to forecast at all (`InsufficientHistoryError`), is skipped."""
    actual_by_ts = dict(price_history)
    errors_persistence: list[float] = []
    errors_shaped: list[float] = []

    for target in target_timestamps:
        actual = actual_by_ts.get(target)
        if actual is None:
            continue
        history_before = [(ts, value) for ts, value in price_history if ts < target]
        try:
            slot = compute_slot_quantiles(
                history_before,
                target,
                stale=False,
                lookback_days=lookback_days,
                resolution_min=resolution_min,
            )
        except InsufficientHistoryError:
            continue

        shape = resolve_solar_shape_input(
            target,
            solar_mw_by_ts=solar_mw_by_ts,
            cloud_cover_pct=(cloud_cover_by_ts or {}).get(target),
        )
        _, shaped_p50, _ = apply_solar_shape((slot.p10, slot.p50, slot.p90), shape)

        errors_persistence.append(abs(slot.p50 - actual))
        errors_shaped.append(abs(shaped_p50 - actual))

    if not errors_persistence:
        return BacktestResult(
            series_key=series_key, n_points=0, mae_persistence=0.0, mae_shaped=0.0, improvement_pct=0.0
        )

    mae_persistence = statistics.fmean(errors_persistence)
    mae_shaped = statistics.fmean(errors_shaped)
    improvement_pct = (
        0.0 if mae_persistence == 0 else (mae_persistence - mae_shaped) / mae_persistence * 100.0
    )
    return BacktestResult(
        series_key=series_key,
        n_points=len(errors_persistence),
        mae_persistence=mae_persistence,
        mae_shaped=mae_shaped,
        improvement_pct=improvement_pct,
    )


def sample_targets(
    window_start: datetime,
    window_end: datetime,
    *,
    sample_interval_min: int = DEFAULT_SAMPLE_INTERVAL_MIN,
) -> list[datetime]:
    """Evenly spaced timestamps in `[window_start, window_end)`, `sample_interval_min` apart from
    `window_start` -- a synthetic grid, useful when the caller wants round-number sample points
    regardless of what the real feed happens to contain. `run_backtest` does NOT use this: real MVP-S
    history can be far short of a full `backtest_days` window (a live build only days old), and a
    from-`window_start` grid landing on instants the feed simply has no row for would silently score
    nothing (`backtest_series` skips any target with no matching observation) -- see
    `_series_native_targets` for the sampler `run_backtest` actually uses."""
    targets = []
    t = window_start
    while t < window_end:
        targets.append(t)
        t += timedelta(minutes=sample_interval_min)
    return targets


def _series_native_targets(
    price_history: list[tuple[datetime, float]],
    window_start: datetime,
    window_end: datetime,
    *,
    sample_interval_min: int,
) -> list[datetime]:
    """Thin `price_history`'s own timestamps within `[window_start, window_end)` to roughly one every
    `sample_interval_min`, so `backtest_series` always has a real "actual" value to score every sampled
    target against -- unlike a synthetic grid (`sample_targets`), which only lines up with the feed's
    own timestamps by coincidence. Thinning by index (not by re-walking a time grid) works regardless of
    the feed's native resolution (15 min for ERCOT price products)."""
    in_window = sorted(ts for ts, _ in price_history if window_start <= ts < window_end)
    if not in_window:
        return []
    native_resolution_min = DEFAULT_RESOLUTION_MIN
    step = max(1, sample_interval_min // native_resolution_min)
    return in_window[::step]


async def run_backtest(
    history: HistoryProvider,
    series_key: str,
    *,
    now: datetime,
    backtest_days: int = DEFAULT_BACKTEST_DAYS,
    sample_interval_min: int = DEFAULT_SAMPLE_INTERVAL_MIN,
    solar_series: str = "solar_forecast",
    cloud_cover_series: str = "sky_cover",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    resolution_min: int = DEFAULT_RESOLUTION_MIN,
) -> BacktestResult:
    """Pull `series_key`'s last `backtest_days` (plus `lookback_days` more, so the earliest backtest
    target still has a full same-slot pool to draw on) of real `feed_obs` history through `history`
    (read-only: only `window()` calls, never a write), and score both forecast methods against it."""
    window_end = now
    window_start = now - timedelta(days=backtest_days)
    fetch_start = window_start - timedelta(days=lookback_days)

    price_obs = await history.window(series_key, fetch_start, window_end)
    price_history = [(row.ts, row.value) for row in price_obs]

    solar_obs = await history.window(solar_series, fetch_start, window_end)
    solar_mw_by_ts = {row.ts: row.value for row in solar_obs}

    cloud_obs = await history.window(cloud_cover_series, fetch_start, window_end)
    cloud_cover_by_ts = {row.ts: row.value for row in cloud_obs}

    targets = _series_native_targets(
        price_history, window_start, window_end, sample_interval_min=sample_interval_min
    )
    return backtest_series(
        price_history,
        targets,
        series_key=series_key,
        solar_mw_by_ts=solar_mw_by_ts,
        cloud_cover_by_ts=cloud_cover_by_ts,
        lookback_days=lookback_days,
        resolution_min=resolution_min,
    )
