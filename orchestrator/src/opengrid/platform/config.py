"""Configuration loading: TOML file named by OG_CONFIG, plus env-var overrides.

BUILD.md S5: on the server, tools/remote.ps1 sets OG_DB=og_t_<ws> and OG_MQTT_ROOT=ogtest/<ws> to give
each agent workspace its own database and MQTT topic root on the shared broker/Postgres instance. These
two env vars always win over whatever `orchestrator.toml` says, so a workspace never has to fork the
config file. Secrets (02b S1.5) are never read from this module directly by name -- callers resolve
`*_env` keys (e.g. `subscription_key_env`) against `os.environ` themselves.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

_ENV_CONFIG_PATH = "OG_CONFIG"
_ENV_DB_OVERRIDE = "OG_DB"
_ENV_MQTT_ROOT_OVERRIDE = "OG_MQTT_ROOT"


class ConfigError(Exception):
    pass


def _deep_get(d: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _deep_set(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class Config:
    """Thin wrapper over the parsed TOML dict with dotted-path lookup and env overrides applied."""

    def __init__(self, data: dict[str, Any], *, source_path: Path | None = None) -> None:
        self._data = data
        self.source_path = source_path

    def get(self, dotted_path: str, default: Any = None) -> Any:
        return _deep_get(self._data, dotted_path, default)

    def __getitem__(self, dotted_path: str) -> Any:
        sentinel = object()
        value = self.get(dotted_path, sentinel)
        if value is sentinel:
            raise KeyError(dotted_path)
        return value

    def as_dict(self) -> dict[str, Any]:
        return self._data

    # --- convenience accessors for the handful of fields every process reads --------------------
    @property
    def postgres_database(self) -> str:
        return str(self.get("postgres.database", "og"))

    @property
    def mqtt_topic_root(self) -> str:
        return str(self.get("mqtt.topic_root", "og/v1"))


def load_config(path: str | Path | None = None) -> Config:
    """Load `orchestrator.toml` from `path`, or from the `OG_CONFIG` env var, and apply the two
    workspace overrides (`OG_DB` -> postgres.database, `OG_MQTT_ROOT` -> mqtt.topic_root)."""
    # PLAT-001: `Path("")` normalises to `Path(".")`, which is truthy and exists (the cwd) -- so the
    # old `not resolved or not str(resolved)` guard never actually caught an unset/empty OG_CONFIG. Test
    # the raw string BEFORE building a Path, so an unset/blank env var fails loudly and specifically.
    raw_path = str(path) if path is not None else os.environ.get(_ENV_CONFIG_PATH, "")
    if not raw_path:
        raise ConfigError(f"No config path given and {_ENV_CONFIG_PATH} is not set")
    resolved = Path(raw_path)
    if not resolved.exists():
        raise ConfigError(f"Config file not found: {resolved}")

    with resolved.open("rb") as fh:
        data = tomllib.load(fh)

    db_override = os.environ.get(_ENV_DB_OVERRIDE)
    if db_override:
        _deep_set(data, "postgres.database", db_override)

    mqtt_root_override = os.environ.get(_ENV_MQTT_ROOT_OVERRIDE)
    if mqtt_root_override:
        _deep_set(data, "mqtt.topic_root", mqtt_root_override)

    return Config(data, source_path=resolved)


def resolve_secret(env_var_name: str) -> str:
    """Resolve a secret by env-var NAME (never by value) -- e.g. config says
    `subscription_key_env = "ERCOT_PUBLIC_API_KEY_PRIMARY"`; call `resolve_secret(cfg.get(...))`.
    Raises ConfigError (never logs the missing var's would-be value) if unset.
    """
    value = os.environ.get(env_var_name)
    if value is None:
        raise ConfigError(f"Required secret env var is not set: {env_var_name}")
    return value
