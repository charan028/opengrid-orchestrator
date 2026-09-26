"""`api.pq_capture_limits`: 1 capture per hub per 60 s, 20 per 60 s fleet-wide."""

from __future__ import annotations

import pytest

from opengrid.api.pq_capture_limits import CaptureRateLimitedError, CaptureRateLimiter


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_one_capture_per_hub_per_minute() -> None:
    clock = _Clock()
    limiter = CaptureRateLimiter(clock=clock)
    limiter.acquire("hub-1")
    clock.t += 59.0
    with pytest.raises(CaptureRateLimitedError) as exc:
        limiter.acquire("hub-1")
    assert exc.value.retry_after_s == pytest.approx(1.0)
    limiter.acquire("hub-2")  # another hub is unaffected
    clock.t += 1.0
    limiter.acquire("hub-1")


def test_twenty_per_minute_fleet_wide_and_a_refusal_is_not_recorded() -> None:
    clock = _Clock()
    limiter = CaptureRateLimiter(clock=clock)
    for i in range(20):
        limiter.acquire(f"hub-{i}")
    with pytest.raises(CaptureRateLimitedError):
        limiter.acquire("hub-20")
    clock.t += 60.0
    limiter.acquire("hub-20")  # the window rolled; the refused attempt left no trace
