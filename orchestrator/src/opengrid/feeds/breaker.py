"""Per-source circuit breaker (02b S2.6), simplified from the full 4.6 table.

Opens after 5 consecutive failures or >=50% failures in the last 10 calls. Open duration starts at 60s
and doubles on each re-open up to a 15-minute cap, then allows one half-open probe. While open, callers
keep serving the last-good value (LGV) with growing age -- the breaker never blocks a read.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

BreakerState = Literal["CLOSED", "OPEN", "HALF_OPEN"]

CONSECUTIVE_FAILURE_THRESHOLD = 5
FAILURE_RATE_WINDOW = 10
FAILURE_RATE_THRESHOLD = 0.5
INITIAL_OPEN_DURATION_S = 60.0
MAX_OPEN_DURATION_S = 900.0  # 15 min


@dataclass
class CircuitBreaker:
    """One instance per feed source (ERCOT, EIA, NWS)."""

    source: str
    _state: BreakerState = "CLOSED"
    _consecutive_failures: int = 0
    _recent_outcomes: deque[bool] = field(default_factory=lambda: deque(maxlen=FAILURE_RATE_WINDOW))
    _opened_at: datetime | None = None
    _current_open_duration_s: float = INITIAL_OPEN_DURATION_S
    _half_open_probe_in_flight: bool = False

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == "OPEN"

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow_request(self, *, now: datetime | None = None) -> bool:
        """Whether a new call may be attempted right now. OPEN blocks until the open window elapses,
        at which point exactly one HALF_OPEN probe is allowed through."""
        now = now or datetime.now(UTC)
        if self._state == "CLOSED":
            return True
        if self._state == "HALF_OPEN":
            return not self._half_open_probe_in_flight
        if self._opened_at is None:  # defensive: OPEN always sets _opened_at via _open()
            raise RuntimeError(f"breaker for {self.source!r} is OPEN with no _opened_at recorded")
        elapsed = (now - self._opened_at).total_seconds()
        if elapsed >= self._current_open_duration_s:
            self._state = "HALF_OPEN"
            self._half_open_probe_in_flight = False
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._recent_outcomes.append(True)
        self._state = "CLOSED"
        self._opened_at = None
        self._half_open_probe_in_flight = False
        self._current_open_duration_s = INITIAL_OPEN_DURATION_S

    def record_failure(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._consecutive_failures += 1
        self._recent_outcomes.append(False)
        if self._state == "HALF_OPEN":
            self._open(now, escalate=True)
            return
        failure_rate = self._failure_rate()
        if self._consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD or (
            len(self._recent_outcomes) >= FAILURE_RATE_WINDOW and failure_rate >= FAILURE_RATE_THRESHOLD
        ):
            self._open(now, escalate=False)

    def _failure_rate(self) -> float:
        if not self._recent_outcomes:
            return 0.0
        failures = sum(1 for ok in self._recent_outcomes if not ok)
        return failures / len(self._recent_outcomes)

    def _open(self, now: datetime, *, escalate: bool) -> None:
        was_open_before = self._state != "CLOSED"
        self._state = "OPEN"
        self._opened_at = now
        self._half_open_probe_in_flight = False
        if escalate or was_open_before:
            self._current_open_duration_s = min(self._current_open_duration_s * 2, MAX_OPEN_DURATION_S)
        else:
            self._current_open_duration_s = INITIAL_OPEN_DURATION_S

    def begin_half_open_probe(self) -> None:
        if self._state == "HALF_OPEN":
            self._half_open_probe_in_flight = True
