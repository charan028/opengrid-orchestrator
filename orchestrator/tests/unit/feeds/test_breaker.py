"""02b S2.6 circuit breaker: 5 consecutive failures OR >=50% of the last 10 calls opens it; open
duration starts at 60s and doubles (capped at 15 min) on repeated failure; one half-open probe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.feeds.breaker import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    INITIAL_OPEN_DURATION_S,
    MAX_OPEN_DURATION_S,
    CircuitBreaker,
)

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_stays_closed_below_thresholds() -> None:
    breaker = CircuitBreaker("ERCOT")
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD - 1):
        breaker.record_failure(now=T0)
    assert breaker.state == "CLOSED"
    assert breaker.allow_request(now=T0)


def test_opens_after_consecutive_failure_threshold() -> None:
    breaker = CircuitBreaker("ERCOT")
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        breaker.record_failure(now=T0)
    assert breaker.is_open
    assert not breaker.allow_request(now=T0)


def test_opens_on_failure_rate_even_without_consecutive_run() -> None:
    breaker = CircuitBreaker("ERCOT")
    # Alternate success/failure (starting with success, ending with failure) so the consecutive-failure
    # count never reaches the threshold, but the 10-call window's failure rate hits exactly 50% right
    # as the window fills -- the rate check runs on `record_failure`, so the window-completing call
    # must itself be a failure to observe the breaker open.
    for i in range(10):
        if i % 2 == 0:
            breaker.record_success()
        else:
            breaker.record_failure(now=T0)
    assert breaker.is_open


def test_stays_open_until_duration_elapses_then_half_opens() -> None:
    breaker = CircuitBreaker("ERCOT")
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        breaker.record_failure(now=T0)
    assert not breaker.allow_request(now=T0 + timedelta(seconds=INITIAL_OPEN_DURATION_S - 1))
    assert breaker.allow_request(now=T0 + timedelta(seconds=INITIAL_OPEN_DURATION_S))
    assert breaker.state == "HALF_OPEN"


def test_half_open_probe_failure_escalates_open_duration() -> None:
    breaker = CircuitBreaker("ERCOT")
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        breaker.record_failure(now=T0)
    t_half_open = T0 + timedelta(seconds=INITIAL_OPEN_DURATION_S)
    assert breaker.allow_request(now=t_half_open)
    breaker.begin_half_open_probe()
    breaker.record_failure(now=t_half_open)
    assert breaker.is_open
    # doubled duration means the initial window alone is not enough to reopen it
    assert not breaker.allow_request(now=t_half_open + timedelta(seconds=INITIAL_OPEN_DURATION_S))
    assert breaker.allow_request(now=t_half_open + timedelta(seconds=INITIAL_OPEN_DURATION_S * 2))


def test_open_duration_caps_at_max() -> None:
    breaker = CircuitBreaker("ERCOT")
    now = T0
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        breaker.record_failure(now=now)
    # repeatedly half-open-probe-fail until the doubling would exceed the cap
    for _ in range(10):
        now = now + timedelta(seconds=breaker._current_open_duration_s)
        if not breaker.allow_request(now=now):
            continue
        breaker.begin_half_open_probe()
        breaker.record_failure(now=now)
    assert breaker._current_open_duration_s <= MAX_OPEN_DURATION_S


def test_success_resets_to_closed() -> None:
    breaker = CircuitBreaker("ERCOT")
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        breaker.record_failure(now=T0)
    assert breaker.is_open
    t_half_open = T0 + timedelta(seconds=INITIAL_OPEN_DURATION_S)
    assert breaker.allow_request(now=t_half_open)
    breaker.begin_half_open_probe()
    breaker.record_success()
    assert breaker.state == "CLOSED"
    assert breaker.consecutive_failures == 0
