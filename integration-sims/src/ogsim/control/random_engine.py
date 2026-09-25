"""Autonomous random-mode engine: the sims inject abnormal events on their
own, continuously, in addition to manual/scenario injection.

Arrivals per anomaly type follow a Poisson process (`rate_per_hour`); each
arrival's duration and numeric params are drawn uniformly from configured
ranges, scaled by the active intensity profile (calm/normal/stressed/chaos).
A global `max_concurrent` cap limits how many random-source anomalies may be
active at once; an arrival that would exceed it is dropped (not queued), so
the system never falls permanently behind.

Design for testability: all randomness goes through one seeded
`numpy.random.Generator`, and the Poisson-arrival math (`plan_arrivals`) and
per-arrival sampling (`sample_duration_s`, `sample_params`) are pure
functions with no wall-clock or asyncio dependency. Only `RandomEngine.run`
(the production scheduler loop) touches real time, via an injectable
`clock`/`sleep` pair.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from ogsim.control.injector import Injector
from ogsim.control.random_config import ParamRange, RandomEngineConfig, TypeRandomConfig

SECONDS_PER_HOUR = 3600.0
MAX_ARRIVALS_PER_PLAN = 100_000  # guards against a pathological rate_per_hour input


def plan_arrivals(rng: np.random.Generator, rate_per_hour: float, window_s: float) -> list[float]:
    """Returns Poisson-process arrival offsets (seconds, ascending) within
    [0, window_s) for the given `rate_per_hour`. Deterministic for a given
    `rng` state, so re-seeding the same generator reproduces the same plan."""
    if rate_per_hour <= 0 or window_s <= 0:
        return []
    rate_per_s = rate_per_hour / SECONDS_PER_HOUR
    mean_interarrival_s = 1.0 / rate_per_s
    arrivals: list[float] = []
    t = 0.0
    while t < window_s and len(arrivals) < MAX_ARRIVALS_PER_PLAN:
        t += float(rng.exponential(mean_interarrival_s))
        if t < window_s:
            arrivals.append(t)
    return arrivals


def sample_duration_s(
    rng: np.random.Generator, type_cfg: TypeRandomConfig, duration_multiplier: float
) -> float:
    low, high = type_cfg.duration_s
    base = low if low == high else float(rng.uniform(low, high))
    return max(1.0, base * duration_multiplier)


def sample_params(rng: np.random.Generator, type_cfg: TypeRandomConfig) -> dict[str, float]:
    return {name: _sample_range(rng, r) for name, r in type_cfg.params.items()}


def _sample_range(rng: np.random.Generator, r: ParamRange) -> float:
    if r.low == r.high:
        return r.low
    return float(rng.uniform(r.low, r.high))


class RandomEngine:
    """Runs one independent Poisson-arrival loop per enabled anomaly type.
    Each loop: sleep for an exponential interarrival time, then (if not
    paused, the type and its owning simulator are enabled, and the
    max-concurrency cap allows it) inject one random-source anomaly."""

    def __init__(
        self,
        injector: Injector,
        config: RandomEngineConfig,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.injector = injector
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self._rng = np.random.default_rng(config.seed)
        self._tasks: list[asyncio.Task[None]] = []

    # ---- runtime controls (REST/CLI/UI all call these) --------------------
    def pause(self) -> None:
        self.config.paused = True

    def resume(self) -> None:
        self.config.paused = False

    def set_profile(self, profile: str) -> None:
        if profile not in self.config.profiles:
            raise ValueError(f"unknown intensity profile '{profile}'")
        self.config.profile = profile

    def set_sim_enabled(self, sim: str, enabled: bool) -> None:
        self.config.sim_enabled[sim] = enabled

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.config.paused,
            "profile": self.config.profile,
            "seed": self.config.seed,
            "max_concurrent": self.config.max_concurrent,
            "sims": dict(self.config.sim_enabled),
            "types": {
                t.type: {"enabled": t.enabled, "rate_per_hour": t.rate_per_hour, "owner": t.owner}
                for t in self.config.types.values()
            },
        }

    # ---- admission control (pure, directly testable) -----------------------
    def is_type_active(self, type_cfg: TypeRandomConfig) -> bool:
        return (
            not self.config.paused and type_cfg.enabled and self.config.sim_enabled.get(type_cfg.owner, True)
        )

    def can_admit_arrival(self) -> bool:
        now = self._clock()
        active_random = self.injector.active(source="random", now=now)
        return len(active_random) < self.config.max_concurrent

    # ---- one arrival ---------------------------------------------------------
    async def fire(self, type_cfg: TypeRandomConfig) -> str | None:
        """Injects one random-source anomaly for `type_cfg` if admission
        control allows it; returns the injected id, or None if dropped."""
        if not self.is_type_active(type_cfg) or not self.can_admit_arrival():
            return None
        multipliers = self.config.active_multipliers()
        duration_s = sample_duration_s(self._rng, type_cfg, multipliers.duration_multiplier)
        params = sample_params(self._rng, type_cfg)
        record = await self.injector.inject(
            type_=type_cfg.type,
            target="*",
            params=params,
            duration=duration_s,
            anomaly_id=str(uuid.uuid4()),
            source="random",
        )
        return record.id

    # ---- production scheduler loop -------------------------------------------
    async def _loop_for_type(self, type_cfg: TypeRandomConfig) -> None:
        while True:
            multipliers = self.config.active_multipliers()
            rate = type_cfg.rate_per_hour * multipliers.rate_multiplier
            if rate <= 0:
                await self._sleep(SECONDS_PER_HOUR)  # nothing to do; recheck hourly for config changes
                continue
            interarrival_s = float(self._rng.exponential(SECONDS_PER_HOUR / rate))
            await self._sleep(interarrival_s)
            await self.fire(type_cfg)

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._loop_for_type(t)) for t in self.config.types.values()]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
