"""Configuration for ogsim.market, read entirely from the environment so it
never needs to import orchestrator config (K: no shared code with `opengrid`).

Env vars (all optional, sensible defaults for local/dev use):

  OGSIM_MARKET_HOST                default 0.0.0.0
  OGSIM_MARKET_PORT                default 8090
  OGSIM_MARKET_DATA_MODE           "replay" | "synthetic" (default "synthetic")
  OGSIM_MARKET_SEED                int, default 1234 (synthetic mode)
  OGSIM_MARKET_HISTORY_PATH        default /var/lib/opengrid/import/mariadb_history_signals.tsv
  OGSIM_MARKET_TEST_USERS          comma-separated "email:password" pairs accepted by the
                                    B2C token endpoint; default "test@example.com:test"
  OGSIM_MARKET_KEY_PRIMARY         default "test-primary-key"
  OGSIM_MARKET_KEY_SECONDARY       default "test-secondary-key"
  OGSIM_MARKET_EIA_API_KEY         default "test-eia-key"
  OGSIM_MARKET_NWS_USER_AGENT      if set, NWS requests must supply *a* non-empty
                                    User-Agent (any value) - we don't pin to a specific
                                    string, matching the real NWS behaviour.
  OGSIM_MARKET_ANOMALY_LOG_PATH    default ./anomalies.jsonl (falls back if
                                    /var/lib/opengrid/sim is not writable)
  OGSIM_CONFIG_DIR                 systemd-unit config directory convention, shared with
                                    ogsim.control (default integration-sims/config). If
                                    `<OGSIM_CONFIG_DIR>/market.env` exists, its KEY=VALUE
                                    lines seed the env vars above (a real env var always
                                    wins over the file).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_INTEGRATION_SIMS_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR_ENV_VAR = "OGSIM_CONFIG_DIR"
MARKET_ENV_FILENAME = "market.env"


def config_dir() -> Path:
    """The systemd-unit config directory convention: `OGSIM_CONFIG_DIR`
    (default `integration-sims/config`), read by both `ogsim.market` and
    `ogsim.control`."""
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    return Path(override) if override else REPO_INTEGRATION_SIMS_DIR / "config"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _env(name: str, default: str, file_values: dict[str, str]) -> str:
    """A real environment variable always wins over `market.env`, which in
    turn wins over `default`."""
    if name in os.environ:
        return os.environ[name]
    return file_values.get(name, default)


def _split_users(raw: str) -> dict[str, str]:
    users: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            continue
        user, _, pw = pair.partition(":")
        users[user.strip()] = pw.strip()
    return users


@dataclass(frozen=True)
class MarketConfig:
    host: str = field(default_factory=lambda: os.environ.get("OGSIM_MARKET_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("OGSIM_MARKET_PORT", "8090")))
    data_mode: str = field(default_factory=lambda: os.environ.get("OGSIM_MARKET_DATA_MODE", "synthetic"))
    seed: int = field(default_factory=lambda: int(os.environ.get("OGSIM_MARKET_SEED", "1234")))
    history_path: str = field(
        default_factory=lambda: os.environ.get(
            "OGSIM_MARKET_HISTORY_PATH", "/var/lib/opengrid/import/mariadb_history_signals.tsv"
        )
    )
    test_users: dict[str, str] = field(
        default_factory=lambda: _split_users(
            os.environ.get("OGSIM_MARKET_TEST_USERS", "test@example.com:test")
        )
    )
    key_primary: str = field(
        default_factory=lambda: os.environ.get("OGSIM_MARKET_KEY_PRIMARY", "test-primary-key")
    )
    key_secondary: str = field(
        default_factory=lambda: os.environ.get("OGSIM_MARKET_KEY_SECONDARY", "test-secondary-key")
    )
    eia_api_key: str = field(
        default_factory=lambda: os.environ.get("OGSIM_MARKET_EIA_API_KEY", "test-eia-key")
    )
    anomaly_log_path: str = field(default_factory=lambda: os.environ.get("OGSIM_MARKET_ANOMALY_LOG_PATH", ""))

    def known_keys(self) -> set[str]:
        return {self.key_primary, self.key_secondary}


def load_config() -> MarketConfig:
    """Builds config from real env vars first, falling back to
    `<OGSIM_CONFIG_DIR>/market.env` for anything not set in the environment."""
    file_values = _load_env_file(config_dir() / MARKET_ENV_FILENAME)
    return MarketConfig(
        host=_env("OGSIM_MARKET_HOST", "0.0.0.0", file_values),
        port=int(_env("OGSIM_MARKET_PORT", "8090", file_values)),
        data_mode=_env("OGSIM_MARKET_DATA_MODE", "synthetic", file_values),
        seed=int(_env("OGSIM_MARKET_SEED", "1234", file_values)),
        history_path=_env(
            "OGSIM_MARKET_HISTORY_PATH", "/var/lib/opengrid/import/mariadb_history_signals.tsv", file_values
        ),
        test_users=_split_users(_env("OGSIM_MARKET_TEST_USERS", "test@example.com:test", file_values)),
        key_primary=_env("OGSIM_MARKET_KEY_PRIMARY", "test-primary-key", file_values),
        key_secondary=_env("OGSIM_MARKET_KEY_SECONDARY", "test-secondary-key", file_values),
        eia_api_key=_env("OGSIM_MARKET_EIA_API_KEY", "test-eia-key", file_values),
        anomaly_log_path=_env("OGSIM_MARKET_ANOMALY_LOG_PATH", "", file_values),
    )
