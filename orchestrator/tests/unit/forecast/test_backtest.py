"""D-24 backtest (`forecast/backtest.py`): walk-forward MAE of quantile persistence vs. the
solar-shaped forecast, against synthetic (pure, no-DB) history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.core.models.platform import FeedObs
from opengrid.forecast.backtest import backtest_series, run_backtest, sample_targets
from opengrid.forecast.solar import clear_sky_shape

_TARGET_DAY = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)  # 00:00 local (America/Chicago, UTC-5 in July)
_MIDDAY_TARGET = _TARGET_DAY + timedelta(hours=13)  # 13:00 local == solar noon (SUNRISE/SUNSET midpoint)


def _pool_history(days: int = 14) -> list[tuple[datetime, float]]:
    """Same slot/day-type pool for `_MIDDAY_TARGET`: values 40..53 (spread 13) across 14 prior days, so
    `compute_slot_quantiles` has a nonzero band width to shift."""
    return [(_MIDDAY_TARGET - timedelta(days=d), 40.0 + d) for d in range(1, days + 1)]


def test_sample_targets_evenly_spaced() -> None:
    start = _TARGET_DAY
    end = _TARGET_DAY + timedelta(hours=6)
    targets = sample_targets(start, end, sample_interval_min=180)
    assert targets == [start, start + timedelta(hours=3)]


def test_backtest_series_skips_target_without_an_actual_value() -> None:
    history = _pool_history()  # no entry AT _MIDDAY_TARGET itself
    result = backtest_series(history, [_MIDDAY_TARGET], series_key="HB_TEST")
    assert result.n_points == 0
    assert result.mae_persistence == 0.0
    assert result.mae_shaped == 0.0


def test_backtest_series_skips_target_with_no_prior_history() -> None:
    too_early_target = _TARGET_DAY - timedelta(days=365)
    history = [*_pool_history(), (too_early_target, 42.0)]
    result = backtest_series(history, [too_early_target], series_key="HB_TEST")
    assert result.n_points == 0


def test_backtest_series_shaped_forecast_beats_persistence_on_a_deep_solar_dip() -> None:
    """The historical same-slot pool has no solar signal baked in (its own spread is just day-to-day
    noise), so plain persistence predicts near the pool's own P50. On the backtest day, an unusually
    deep midday dip actually occurred (as a strong ERCOT solar-forecast signal for that same day would
    have predicted) -- the solar-shaped forecast, which pulls the midday band down, must land closer to
    that real outcome than the untouched persistence baseline."""
    pool = _pool_history()
    pool_values = [v for _, v in pool]
    pool_p50 = sorted(pool_values)[len(pool_values) // 2]

    actual_dip = pool_p50 - 20.0  # a much deeper dip than the historical pool alone would suggest
    history = [*pool, (_MIDDAY_TARGET, actual_dip)]

    # A strong, unambiguous midday solar signal for the backtest day only.
    solar_mw_by_ts = {_MIDDAY_TARGET: 1000.0 * clear_sky_shape(_MIDDAY_TARGET)}

    result = backtest_series(history, [_MIDDAY_TARGET], series_key="HB_TEST", solar_mw_by_ts=solar_mw_by_ts)

    assert result.n_points == 1
    assert result.mae_shaped < result.mae_persistence
    assert result.improvement_pct > 0.0


def test_backtest_series_reports_zero_points_on_empty_history() -> None:
    result = backtest_series([], [_MIDDAY_TARGET], series_key="HB_TEST")
    assert result.n_points == 0
    assert result.improvement_pct == 0.0


class _FakeHistory:
    """Structurally satisfies `HistoryProvider` -- only `window()` is needed by `run_backtest`."""

    def __init__(self, observations: dict[str, list[FeedObs]]) -> None:
        self._observations = observations

    async def latest(self, series: str) -> FeedObs:  # pragma: no cover - unused by run_backtest
        raise LookupError(series)

    async def window(self, series: str, t0: datetime, t1: datetime) -> list[FeedObs]:
        return [o for o in self._observations.get(series, []) if t0 <= o.ts < t1]


def _price_obs(series: str, ts: datetime, value: float) -> FeedObs:
    return FeedObs(
        source="ERCOT",
        product="np6-905-cd",
        series=series,
        ts=ts,
        value=value,
        unit="usd_per_mwh",
        quality="GOOD",
        recorded_at=ts,
    )


async def test_run_backtest_scores_real_points_even_with_much_less_than_backtest_days_of_history() -> None:
    """Live incident: a fresh MVP-S build only has ~2.5 days of real `feed_obs` history, far short of
    `DEFAULT_BACKTEST_DAYS` (7) -- a synthetic sampling grid anchored to `now - 7d` mostly lands on
    instants the feed has no row for at all yet, and `backtest_series` skips every one of those,
    silently reporting `n_points=0` even though real, scoreable history did exist. Targets must be
    drawn from the series' own real timestamps, not a grid that assumes a full week of data."""
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    only_two_and_a_half_days_of_history = [
        _price_obs("HB_TEST", now - timedelta(days=2, hours=12) + timedelta(minutes=15 * i), 40.0 + i % 10)
        for i in range(int(2.5 * 24 * 4))
    ]
    history = _FakeHistory({"HB_TEST": only_two_and_a_half_days_of_history})

    result = await run_backtest(history, "HB_TEST", now=now, backtest_days=7)

    assert result.n_points > 0
