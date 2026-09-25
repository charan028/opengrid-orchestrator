"""Loads and validates the autonomous random-mode configuration
(`integration-sims/config/random.yaml` by default).

Shape::

    seed: 42
    profile: normal
    paused: false
    max_concurrent: 5
    sims:
      market: {enabled: true}
      scada:  {enabled: true}
      fleet:  {enabled: true}
    profiles:
      calm:     {rate_multiplier: 0.25, duration_multiplier: 0.75}
      normal:   {rate_multiplier: 1.0,  duration_multiplier: 1.0}
      stressed: {rate_multiplier: 2.5,  duration_multiplier: 1.25}
      chaos:    {rate_multiplier: 6.0,  duration_multiplier: 1.5}
    types:
      price_spike:
        enabled: true
        rate_per_hour: 0.5
        duration_s: [60, 300]
        params:
          value_usd_per_mwh: [1000, 5000]
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ogsim.control import catalogue

REPO_INTEGRATION_SIMS_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR_ENV_VAR = "OGSIM_CONFIG_DIR"
RANDOM_CONFIG_FILENAME = "random.yaml"


def config_dir() -> Path:
    """The systemd-unit config directory convention: `OGSIM_CONFIG_DIR`
    (default `integration-sims/config`), read by both `ogsim.market` and
    `ogsim.control`."""
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    return Path(override) if override else REPO_INTEGRATION_SIMS_DIR / "config"


def default_config_path() -> Path:
    return config_dir() / RANDOM_CONFIG_FILENAME


INTENSITY_PROFILES = ("calm", "normal", "stressed", "chaos")
DEFAULT_PROFILE = "normal"
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_RATE_PER_HOUR = 0.1
DEFAULT_DURATION_RANGE_S = (60.0, 180.0)


class RandomConfigError(ValueError):
    """Raised when config/random.yaml is malformed."""


@dataclass(frozen=True)
class ProfileMultipliers:
    rate_multiplier: float = 1.0
    duration_multiplier: float = 1.0


@dataclass(frozen=True)
class ParamRange:
    """A closed numeric range [low, high] a param is sampled from uniformly.
    `low == high` for a fixed (non-randomized) param value."""

    low: float
    high: float

    @classmethod
    def parse(cls, raw: Any) -> ParamRange:
        if isinstance(raw, list | tuple) and len(raw) == 2:
            return cls(float(raw[0]), float(raw[1]))
        return cls(float(raw), float(raw))


@dataclass(frozen=True)
class TypeRandomConfig:
    type: str
    owner: str
    enabled: bool = True
    rate_per_hour: float = DEFAULT_RATE_PER_HOUR
    duration_s: tuple[float, float] = DEFAULT_DURATION_RANGE_S
    params: dict[str, ParamRange] = field(default_factory=dict)


@dataclass
class RandomEngineConfig:
    seed: int = 0
    profile: str = DEFAULT_PROFILE
    paused: bool = False
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    sim_enabled: dict[str, bool] = field(
        default_factory=lambda: {"market": True, "scada": True, "fleet": True}
    )
    profiles: dict[str, ProfileMultipliers] = field(
        default_factory=lambda: {
            "calm": ProfileMultipliers(0.25, 0.75),
            "normal": ProfileMultipliers(1.0, 1.0),
            "stressed": ProfileMultipliers(2.5, 1.25),
            "chaos": ProfileMultipliers(6.0, 1.5),
        }
    )
    types: dict[str, TypeRandomConfig] = field(default_factory=dict)

    def active_multipliers(self) -> ProfileMultipliers:
        return self.profiles.get(self.profile, ProfileMultipliers())


def _parse_type_entry(type_id: str, raw: dict[str, Any]) -> TypeRandomConfig:
    owner = catalogue.owner_of(type_id)
    if owner is None:
        raise RandomConfigError(f"config/random.yaml: unknown anomaly type '{type_id}'")
    duration_raw = raw.get("duration_s", list(DEFAULT_DURATION_RANGE_S))
    duration_s = (float(duration_raw[0]), float(duration_raw[1]))
    params = {name: ParamRange.parse(value) for name, value in (raw.get("params") or {}).items()}
    return TypeRandomConfig(
        type=type_id,
        owner=owner,
        enabled=bool(raw.get("enabled", True)),
        rate_per_hour=float(raw.get("rate_per_hour", DEFAULT_RATE_PER_HOUR)),
        duration_s=duration_s,
        params=params,
    )


def load_random_config(path: str | Path | None = None) -> RandomEngineConfig:
    """Loads config/random.yaml. Missing file -> defaults (engine paused-off
    behaviourally equivalent, since `types` is empty and nothing fires)."""
    path = Path(path) if path is not None else default_config_path()
    if not path.exists():
        return RandomEngineConfig()
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = RandomEngineConfig(
        seed=int(raw.get("seed", 0)),
        profile=str(raw.get("profile", DEFAULT_PROFILE)),
        paused=bool(raw.get("paused", False)),
        max_concurrent=int(raw.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
    )
    if "sims" in raw:
        cfg.sim_enabled = {name: bool(v.get("enabled", True)) for name, v in raw["sims"].items()}
    if "profiles" in raw:
        cfg.profiles = {
            name: ProfileMultipliers(
                rate_multiplier=float(v.get("rate_multiplier", 1.0)),
                duration_multiplier=float(v.get("duration_multiplier", 1.0)),
            )
            for name, v in raw["profiles"].items()
        }
    cfg.types = {
        type_id: _parse_type_entry(type_id, raw_entry or {})
        for type_id, raw_entry in (raw.get("types") or {}).items()
    }
    return cfg
