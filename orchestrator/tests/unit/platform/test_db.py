"""PLAT-002/003/006: `opengrid.platform.db.build_dsn` fails loudly with no OG_DB_PASSWORD (never a
silent empty-password fallback) and builds its DSN via `psycopg.conninfo.make_conninfo` (correct
quoting for a password containing special characters), plus named-constant defaults (PLAT-005)."""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from opengrid.platform.config import Config, ConfigError
from opengrid.platform.db import (
    DEFAULT_POSTGRES_HOST,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_POSTGRES_USER,
    build_dsn,
)


def _cfg(**postgres: object) -> Config:
    return Config({"postgres": postgres})


def test_build_dsn_raises_when_password_env_unset(monkeypatch):
    monkeypatch.delenv("OG_DB_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        build_dsn(_cfg(database="og"))


def test_build_dsn_uses_defaults_and_env_password(monkeypatch):
    monkeypatch.delenv("OG_DB_USER", raising=False)
    monkeypatch.delenv("OG_DB_PORT", raising=False)  # workspace runs set it (tools/ws_env.sh)
    monkeypatch.setenv("OG_DB_PASSWORD", "s3cret")
    dsn = build_dsn(_cfg(database="og"))
    parsed = conninfo_to_dict(dsn)
    assert parsed["host"] == DEFAULT_POSTGRES_HOST
    assert parsed["port"] == str(DEFAULT_POSTGRES_PORT)
    assert parsed["user"] == DEFAULT_POSTGRES_USER
    assert parsed["password"] == "s3cret"  # noqa: S105 -- asserting the test fixture's own literal
    assert parsed["dbname"] == "og"


def test_og_db_port_overrides_the_configured_port(monkeypatch):
    """Workspace runs go to the disposable test cluster (tools/ws_env.sh sets OG_DB_PORT=5433)."""
    monkeypatch.setenv("OG_DB_PASSWORD", "s3cret")
    monkeypatch.setenv("OG_DB_PORT", "5433")
    assert conninfo_to_dict(build_dsn(_cfg(database="og")))["port"] == "5433"
    monkeypatch.delenv("OG_DB_PORT")
    assert conninfo_to_dict(build_dsn(_cfg(database="og")))["port"] == str(DEFAULT_POSTGRES_PORT)


def test_build_dsn_quotes_special_characters_in_password(monkeypatch):
    """PLAT-003: a hand-rolled f-string DSN mis-parses a password containing a space or '='.
    `make_conninfo` must round-trip it correctly."""
    monkeypatch.setenv("OG_DB_PASSWORD", "pa ss=word'quote")
    dsn = build_dsn(_cfg(database="og"))
    parsed = conninfo_to_dict(dsn)
    assert parsed["password"] == "pa ss=word'quote"  # noqa: S105 -- asserting the test fixture's literal


def test_build_dsn_honors_config_overrides(monkeypatch):
    monkeypatch.setenv("OG_DB_PASSWORD", "pw")
    monkeypatch.setenv("OG_DB_USER", "custom_user")
    dsn = build_dsn(_cfg(host="10.0.0.5", port=5433, database="og_t_ws"))
    parsed = conninfo_to_dict(dsn)
    assert parsed["host"] == "10.0.0.5"
    assert parsed["port"] == "5433"
    assert parsed["user"] == "custom_user"
    assert parsed["dbname"] == "og_t_ws"
