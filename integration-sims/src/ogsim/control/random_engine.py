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
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

from ogsim.control.injector import Injector
from ogsim.control.random_config import ParamRange, RandomEngineConfig, TypeRandomConfig

logger = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600.0
MAX_ARRIVALS_PER_PLAN = 100_000  # guards against a pathological rate_per_hour input

# Demo gap #16, 2026-09-26: pause/resume was held only in `config.paused` (an in-memory dataclass
# field), so a control-plane restart forgot an operator's pause and random mode silently resumed --
# exactly the "quiet hours" switch BUILD.md S3 promises, undone by a restart. Persisted the same way
# `ogsim.control.log.AnomalyLog` persists its JSONL log: a preferred path under the sim's data dir,
# falling back to the working directory when that isn't writable (local dev), both overridable by env
# var for tests and operators.
PAUSE_STATE_PREFERRED_PATH = "/var/lib/opengrid/sim/random_pause_state.json"
PAUSE_STATE_FALLBACK_PATH = "./random_pause_state.json"
PAUSE_STATE_PATH_ENV_VAR = "OGSIM_RANDOM_PAUSE_STATE_PATH"


def _resolve_pause_state_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_override = os.environ.get(PAUSE_STATE_PATH_ENV_VAR)
    if env_override:
        return Path(env_override)
    preferred = Path(PAUSE_STATE_PREFERRED_PATH)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        probe = preferred.parent / ".ogsim_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred
    except OSError:
        return Path(PAUSE_STATE_FALLBACK_PATH)


def load_persisted_paused(path: Path) -> bool | None:
    """The last explicitly-persisted pause state.

    Returns `None` only when no state file has ever been written (a fresh install / first run) --
    `config/random.yaml`'s own `paused:` default wins, same as before this fix.

    R3.1 LOW-review fix, 2026-09-26: if the state file EXISTS but cannot be read or parsed (disk
    corruption, a truncated write, a permissions change) this used to also return `None`, which
    made the engine fail OPEN -- silently resuming random injection even though an operator may have
    paused it. That is backwards for a "quiet hours" safety switch: it now fails CLOSED (returns
    `True`, paused) and logs a warning, so a corrupt state file can never silently un-pause the sim.
    Never raises."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        value = data.get("paused")
        if isinstance(value, bool):
            return value
        logger.warning(
            "pause state file %s exists but has no boolean 'paused' field; failing closed (paused=True)",
            path,
        )
        return True
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "pause state file %s exists but is unreadable/corrupt; failing closed (paused=True)",
            path,
            exc_info=True,
        )
        return True


def save_persisted_paused(path: Path, paused: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"paused": paused}, f)
    except OSError:
        pass  # best-effort: a write failure here must never crash pause()/resume() itself


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
        pause_state_path: str | None = None,
    ):
        self.injector = injector
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self._rng = np.random.default_rng(config.seed)
        self._tasks: list[asyncio.Task[None]] = []
        self._pause_state_path = _resolve_pause_state_path(pause_state_path)
        # Demo gap #16: a persisted pause/resume from a PRIOR process wins over whatever
        # config/random.yaml's own `paused:` says -- the operator's last explicit action is the
        # source of truth across a restart, not the shipped default.
        persisted = load_persisted_paused(self._pause_state_path)
        if persisted is not None:
            self.config.paused = persisted

    # ---- runtime controls (REST/CLI/UI all call these) --------------------
    def pause(self) -> None:
        self.config.paused = True
        save_persisted_paused(self._pause_state_path, True)

    def resume(self) -> None:
        self.config.paused = False
        save_persisted_paused(self._pause_state_path, False)

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
