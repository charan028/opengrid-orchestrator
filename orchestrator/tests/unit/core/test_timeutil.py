from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.timeutil import (
    check_command_freshness,
    clock_offset_ok,
    floor_to_interval,
    interval_bounds,
    interval_index,
    is_stale,
    to_market_tz,
    to_utc,
)


def test_to_utc_rejects_naive():
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 1, 1))


def test_floor_to_interval():
    dt = datetime(2026, 9, 26, 18, 7, 30, tzinfo=UTC)
    assert floor_to_interval(dt) == datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def test_interval_bounds_width_15_min():
    dt = datetime(2026, 9, 26, 18, 7, tzinfo=UTC)
    start, end = interval_bounds(dt)
    assert end - start == timedelta(minutes=15)


def test_interval_index_zero_at_horizon_start():
    horizon = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    assert interval_index(horizon, horizon) == 0
    assert interval_index(horizon + timedelta(minutes=30), horizon) == 2


def test_to_market_tz_offset():
    dt = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    chicago = to_market_tz(dt)
    assert chicago.utcoffset() is not None


def test_is_stale_none_is_always_stale():
    assert is_stale(None, 100)


def test_is_stale_threshold():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=5)
    stale = now - timedelta(seconds=1000)
    assert not is_stale(fresh, 10, now=now)
    assert is_stale(stale, 10, now=now)


def test_check_command_freshness_accepts_increasing_seq():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=1,
        seq=5,
        last_accepted_epoch=1,
        last_accepted_seq=4,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=10),
        now=now,
    )
    assert result.ok


def test_check_command_freshness_rejects_stale_epoch():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=1,
        seq=5,
        last_accepted_epoch=2,
        last_accepted_seq=0,
        issued_at=now,
        expires_at=now + timedelta(seconds=10),
        now=now,
    )
    assert not result.ok and result.reason == "STALE_EPOCH"


def test_check_command_freshness_rejects_stale_seq_same_epoch():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=1,
        seq=3,
        last_accepted_epoch=1,
        last_accepted_seq=4,
        issued_at=now,
        expires_at=now + timedelta(seconds=10),
        now=now,
    )
    assert not result.ok and result.reason == "STALE_SEQ"


def test_check_command_freshness_rejects_not_yet_valid():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=1,
        seq=5,
        last_accepted_epoch=1,
        last_accepted_seq=4,
        issued_at=now + timedelta(seconds=5),
        expires_at=now + timedelta(seconds=10),
        now=now,
    )
    assert not result.ok and result.reason == "NOT_YET_VALID"


def test_check_command_freshness_rejects_expired():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=1,
        seq=5,
        last_accepted_epoch=1,
        last_accepted_seq=4,
        issued_at=now - timedelta(seconds=20),
        expires_at=now - timedelta(seconds=1),
        now=now,
    )
    assert not result.ok and result.reason == "EXPIRED"


def test_check_command_freshness_new_epoch_resets_seq():
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    result = check_command_freshness(
        epoch=2,
        seq=0,
        last_accepted_epoch=1,
        last_accepted_seq=999,
        issued_at=now,
        expires_at=now + timedelta(seconds=10),
        now=now,
    )
    assert result.ok


def test_clock_offset_ok():
    assert clock_offset_ok(100, 200)
    assert not clock_offset_ok(250, 200)
    assert clock_offset_ok(-150, 200)


@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=0, max_value=1000))
def test_interval_index_monotonic(a, b):
    horizon = datetime(2026, 1, 1, tzinfo=UTC)
    ia = interval_index(horizon + timedelta(minutes=a), horizon)
    ib = interval_index(horizon + timedelta(minutes=b), horizon)
    assert (a <= b) == (ia <= ib) or a // 15 == b // 15
