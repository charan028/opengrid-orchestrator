"""Rate limit for on-demand waveform captures (WP-J, `routers.pq`). Owner: integration (MERGE).

A confirmed capture publishes a request that makes a hub stream a raw waveform (S6.4b), so the API caps
them: at most one per hub per `per_hub_s` (60 s) and at most `fleet_per_window` (20) across the fleet per
`window_s` (60 s). In-process and in-memory, like the proposal store: `og-api` is one process, and a restart
only forgets recent history (the caps then start fresh, which errs towards allowing a capture).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_PER_HUB_S = 60.0
DEFAULT_FLEET_PER_WINDOW = 20
DEFAULT_WINDOW_S = 60.0


class CaptureRateLimitedError(Exception):
    """Refused; `retry_after_s` is when the earliest blocking cap frees up."""

    def __init__(self, message: str, *, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class CaptureRateLimiter:
    per_hub_s: float = DEFAULT_PER_HUB_S
    fleet_per_window: int = DEFAULT_FLEET_PER_WINDOW
    window_s: float = DEFAULT_WINDOW_S
    clock: Callable[[], float] = time.monotonic
    _last_by_hub: dict[str, float] = field(default_factory=dict)
    _fleet: deque[float] = field(default_factory=deque)

    def acquire(self, hub_id: str) -> None:
        """Record a capture for `hub_id` now, or raise `CaptureRateLimitedError` without recording it."""
        now = self.clock()
        while self._fleet and now - self._fleet[0] >= self.window_s:
            self._fleet.popleft()
        last = self._last_by_hub.get(hub_id)
        if last is not None and now - last < self.per_hub_s:
            raise CaptureRateLimitedError(
                f"at most one capture per hub per {self.per_hub_s:.0f} s ({hub_id})",
                retry_after_s=self.per_hub_s - (now - last),
            )
        if len(self._fleet) >= self.fleet_per_window:
            raise CaptureRateLimitedError(
                f"at most {self.fleet_per_window} captures per {self.window_s:.0f} s across the fleet",
                retry_after_s=self.window_s - (now - self._fleet[0]),
            )
        self._last_by_hub[hub_id] = now
        self._fleet.append(now)


_LIMITER = CaptureRateLimiter()


def get_capture_limiter() -> CaptureRateLimiter:
    """FastAPI dependency: the process-wide limiter (overridable in tests)."""
    return _LIMITER
