"""ogsim.utility_aen.config -- YAML + env configuration for the simulated utility EMS.

`utility` picks which utility this process speaks for (AUSTIN_ENERGY by default; env `OGSIM_UTILITY`
wins). Each utility in `utilities:` has an `env_code` (credential env-var suffix, e.g. AEN) and `enabled`.
A disabled utility (LCRA, RAYBURN until a real contract exists, D-37) stays idle: no channel is built, no
credential is read, nothing is called. The production MQTT topic root and credentials are resolved by
`ogsim.common.config` exactly as every other sim does (never re-implemented here).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Literal

from ogsim.common.config import MqttSettings, load_yaml_file, resolve_mqtt_credentials, resolve_topic_root

INSTALL_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = INSTALL_DIR / "config" / "utility_aen.yaml"
CONFIG_ENV_VAR = "OGSIM_UTILITY_CONFIG"
UTILITY_ENV_VAR = "OGSIM_UTILITY"
DEFAULT_UTILITY = "AUSTIN_ENERGY"
MQTT_USER = "og_sim_utility"
MQTT_PASSWORD_ENV = "OG_MQTT_UTILITY_PASSWORD"
#: D-29: the tolling product rule caps a call at 90 minutes.
PRODUCT_CAP_MIN = 90

WeatherMode = Literal["random", "hot", "mild"]


class UtilityConfigError(ValueError):
    """The YAML is malformed (never a silent default for a malformed value)."""


@dataclass(frozen=True)
class UtilitySpec:
    utility_id: str
    env_code: str
    enabled: bool


@dataclass(frozen=True)
class WeatherConfig:
    """`random`: a daily high drawn per date from `[min_f, max_f]` (seeded, reproducible); `hot` / `mild`
    force every day hot / not hot. A day is hot when its high >= `hot_threshold_f`."""

    mode: WeatherMode = "random"
    seed: int = 0
    min_f: float = 88.0
    max_f: float = 108.0
    hot_threshold_f: float = 100.0


@dataclass(frozen=True)
class ScheduleConfig:
    """The daily peak call: inside `[window_start, window_end)` America/Chicago on hot days, `call_kw`
    (magnitude; sent as discharge, negative) for `duration_min` (capped at the 90 min product)."""

    window_start: time = time(16, 30)
    window_end: time = time(18, 0)
    call_kw: float = 20_000.0
    duration_min: int = 60
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.window_end <= self.window_start:
            raise UtilityConfigError("schedule.window_end must be after window_start")
        if self.call_kw <= 0 or self.duration_min < 1:
            raise UtilityConfigError("schedule.call_kw must be > 0 and duration_min >= 1")


@dataclass(frozen=True)
class UtilitySimConfig:
    utility: UtilitySpec
    channel: str
    channel_settings: dict[str, Any]
    schedule: ScheduleConfig
    weather: WeatherConfig
    tick_s: float = 5.0
    status_poll_s: float = 30.0
    mqtt: MqttSettings | None = None
    known_utilities: tuple[str, ...] = field(default_factory=tuple)


def _hhmm(value: object, key: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise UtilityConfigError(f"{key}: expected HH:MM, got {value!r}") from exc


def _utility(raw: dict[str, Any]) -> tuple[UtilitySpec, tuple[str, ...]]:
    table = raw.get("utilities") or {}
    if not isinstance(table, dict) or not table:
        raise UtilityConfigError("utilities: must map utility_id -> {env_code, enabled}")
    chosen = str(os.environ.get(UTILITY_ENV_VAR) or raw.get("utility", DEFAULT_UTILITY))
    if chosen not in table:
        raise UtilityConfigError(f"utility {chosen!r} is not in utilities: {sorted(table)}")
    entry = table[chosen] or {}
    spec = UtilitySpec(chosen, str(entry.get("env_code", chosen)), bool(entry.get("enabled", False)))
    return spec, tuple(sorted(table))


def _schedule(raw: dict[str, Any]) -> ScheduleConfig:
    default = ScheduleConfig()
    return ScheduleConfig(
        window_start=_hhmm(raw.get("window_start", "16:30"), "schedule.window_start"),
        window_end=_hhmm(raw.get("window_end", "18:00"), "schedule.window_end"),
        call_kw=float(raw.get("call_kw", default.call_kw)),
        duration_min=min(int(raw.get("duration_min", default.duration_min)), PRODUCT_CAP_MIN),
        enabled=bool(raw.get("enabled", True)),
    )


def _weather(raw: dict[str, Any]) -> WeatherConfig:
    mode = str(raw.get("mode", "random"))
    if mode not in ("random", "hot", "mild"):
        raise UtilityConfigError(f"weather.mode: expected random|hot|mild, got {mode!r}")
    default = WeatherConfig()
    return WeatherConfig(
        mode=mode,  # type: ignore[arg-type]  # checked above
        seed=int(raw.get("seed", default.seed)),
        min_f=float(raw.get("min_f", default.min_f)),
        max_f=float(raw.get("max_f", default.max_f)),
        hot_threshold_f=float(raw.get("hot_threshold_f", default.hot_threshold_f)),
    )


def mqtt_settings(raw: dict[str, Any]) -> MqttSettings | None:
    """None when `mqtt.enabled: false` (no /ogsim/ triggers; the schedule still runs)."""
    if not bool(raw.get("enabled", True)):
        return None
    username, password = resolve_mqtt_credentials(MQTT_USER, MQTT_PASSWORD_ENV)
    return MqttSettings(
        host=str(os.environ.get("OG_MQTT_HOST") or raw.get("host", "127.0.0.1")),
        port=int(os.environ.get("OG_MQTT_PORT") or raw.get("port", 1883)),
        username=username,
        password=password,
        topic_root=resolve_topic_root(raw),
    )


def load_config(path: str | None = None, *, with_mqtt: bool = True) -> UtilitySimConfig:
    raw = load_yaml_file(path or os.environ.get(CONFIG_ENV_VAR) or str(DEFAULT_CONFIG_PATH))
    utility, known = _utility(raw)
    channel = str(raw.get("channel", "customer_api"))
    channels = raw.get("channels") or {}
    settings = dict(channels.get(channel) or {}) if isinstance(channels, dict) else {}
    settings.setdefault("env_code", utility.env_code)
    settings.setdefault("utility_id", utility.utility_id)
    return UtilitySimConfig(
        utility=utility,
        channel=channel,
        channel_settings=settings,
        schedule=_schedule(raw.get("schedule") or {}),
        weather=_weather(raw.get("weather") or {}),
        tick_s=float(raw.get("tick_s", 5.0)),
        status_poll_s=float(raw.get("status_poll_s", 30.0)),
        mqtt=mqtt_settings(raw.get("mqtt") or {}) if with_mqtt and utility.enabled else None,
        known_utilities=known,
    )
