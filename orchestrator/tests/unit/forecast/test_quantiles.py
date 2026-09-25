"""Unit + property tests for the pure quantile math (02b S3). Maps to TS-02-06 "Forecast quantiles
(P10/P50/P90) computed and monotonic" plus the task's explicit property requirements: quantile
ordering, 96-step shape (covered in test_service.py), and widening on stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.forecast.quantiles import (
    InsufficientHistoryError,
    compute_slot_quantiles,
    diurnal_fallback_quantiles,
    same_slot_pool,
    sample_quantiles,
    widen,
)

# A Monday and a Saturday, both in July so there is no DST transition inside a 14-day lookback.
_MONDAY = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
_SATURDAY = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)


def _weekday_type_samples(target: datetime, count: int, value_fn) -> list[tuple[datetime, float]]:
    """`count` synthetic points at `target`'s exact time-of-day, on the nearest days sharing its
    weekday/weekend day-type (so every point falls in the same same-slot/day-type pool as `target`).
    Stays within a 14-day lookback (offsets 1..14) -- `count` must be small enough to fit."""
    result: list[tuple[datetime, float]] = []
    offset = 1
    is_weekend = target.weekday() >= 5
    while len(result) < count:
        if offset > 14:
            raise AssertionError("not enough same-day-type offsets within a 14-day lookback")
        candidate = target - timedelta(days=offset)
        if (candidate.weekday() >= 5) == is_weekend:
            result.append((candidate, value_fn(len(result))))
        offset += 1
    return result


def test_ts_02_06_sample_quantiles_monotonic():
    pool = [10.0, 12.0, 15.0, 20.0, 22.0, 30.0, 5.0, 18.0]
    p10, p50, p90 = sample_quantiles(pool)
    assert p10 <= p50 <= p90


def test_sample_quantiles_empty_pool_raises():
    with pytest.raises(InsufficientHistoryError):
        sample_quantiles([])


def test_same_slot_pool_matches_time_of_day_and_day_type():
    history = [
        (_MONDAY - timedelta(days=7), 40.0),  # previous Monday, same time -- matches
        (_MONDAY - timedelta(days=14), 42.0),  # two Mondays back -- matches
        (_MONDAY - timedelta(hours=6), 999.0),  # same day, different time-of-day -- excluded
        (_SATURDAY - timedelta(days=7), 5.0),  # a Saturday -- wrong day-type, excluded
    ]
    pool = same_slot_pool(history, _MONDAY, lookback_days=14)
    assert sorted(pool) == [40.0, 42.0]


def test_same_slot_pool_respects_lookback_window():
    history = [(_MONDAY - timedelta(days=7), 1.0), (_MONDAY - timedelta(days=21), 2.0)]
    pool = same_slot_pool(history, _MONDAY, lookback_days=14)
    assert pool == [1.0]  # the 21-day-old point falls outside a 14-day lookback


def test_diurnal_fallback_used_below_min_samples_via_compute_slot_quantiles():
    # Only 2 same-slot/day-type samples (below MIN_SLOT_SAMPLES=3), but plenty of other-slot history
    # to fit a diurnal profile from -- the short-history (2.5-day import) degrade path.
    history = _weekday_type_samples(_MONDAY, count=2, value_fn=lambda i: 50.0)
    other_slot_history = [
        (_MONDAY - timedelta(days=d, hours=h), 50.0 + h) for d in range(3) for h in range(1, 20)
    ]
    slot = compute_slot_quantiles(history + other_slot_history, _MONDAY, stale=False)
    assert slot.sample_count < 3
    assert slot.firm_fitness == "NOT_FOR_FIRM"
    assert slot.p10 <= slot.p50 <= slot.p90


def test_diurnal_fallback_quantiles_raises_on_empty_history():
    with pytest.raises(InsufficientHistoryError):
        diurnal_fallback_quantiles([], _MONDAY)


def test_diurnal_fallback_falls_back_to_overall_mean_for_unseen_slot():
    # All history is at a different slot than the target -- profile.get(target_slot) misses, so the
    # overall mean is used rather than raising.
    history = [(_MONDAY - timedelta(days=d, hours=3), 10.0) for d in range(5)]
    base, mid, _ = diurnal_fallback_quantiles(history, _MONDAY)
    assert base == mid == pytest.approx(10.0)


def test_compute_slot_quantiles_full_pool_is_firm_ok_when_not_stale():
    history = _weekday_type_samples(_MONDAY, count=10, value_fn=lambda i: 40.0 + i)
    slot = compute_slot_quantiles(history, _MONDAY, stale=False)
    assert slot.firm_fitness == "FIRM_OK"
    assert slot.sample_count >= 3


def test_widening_widens_band_and_sets_not_for_firm():
    history = _weekday_type_samples(_MONDAY, count=10, value_fn=lambda i: 40.0 + i)
    fresh = compute_slot_quantiles(history, _MONDAY, stale=False)
    stale = compute_slot_quantiles(history, _MONDAY, stale=True)

    assert stale.firm_fitness == "NOT_FOR_FIRM"
    assert stale.p50 == pytest.approx(fresh.p50)
    assert (stale.p90 - stale.p10) > (fresh.p90 - fresh.p10)
    assert stale.p10 <= stale.p50 <= stale.p90


def test_widen_factor_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        widen((1.0, 2.0, 3.0), factor=0)


@given(
    p10=st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
    spread_lo=st.floats(min_value=0, max_value=500, allow_nan=False, allow_infinity=False),
    spread_hi=st.floats(min_value=0, max_value=500, allow_nan=False, allow_infinity=False),
    factor=st.floats(min_value=1.0, max_value=5.0, allow_nan=False, allow_infinity=False),
)
def test_widen_preserves_ordering_property(p10, spread_lo, spread_hi, factor):
    p50 = p10 + spread_lo
    p90 = p50 + spread_hi
    wp10, wp50, wp90 = widen((p10, p50, p90), factor=factor)
    assert wp10 <= wp50 <= wp90
    assert wp50 == pytest.approx(p50)


@given(
    st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=200,
    )
)
def test_sample_quantiles_ordering_property(pool):
    p10, p50, p90 = sample_quantiles(pool)
    assert p10 <= p50 <= p90
