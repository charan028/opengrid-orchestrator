"""Postgres access: an async connection pool (psycopg 3) and a forward-only migration runner.

Run as a script: `python -m opengrid.platform.db migrate` applies every `.sql` file under
`orchestrator/migrations/` in filename order that has not yet been recorded in `og.schema_migrations`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from opengrid.platform.config import Config, load_config, resolve_secret

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")

# PLAT-005: named constants, no bare hosts/ports in the DSN-building logic.
DEFAULT_POSTGRES_HOST = "127.0.0.1"
DEFAULT_POSTGRES_PORT = 5432
DEFAULT_POSTGRES_USER = "opengrid"
DEFAULT_POOL_MIN_SIZE = 2
DEFAULT_POOL_MAX_SIZE = 8
POOL_OPEN_TIMEOUT_S = 10.0  # PLAT-004: og-* processes must not hang forever waiting for Postgres

_ENV_DB_USER = "OG_DB_USER"
_ENV_DB_PASSWORD = "OG_DB_PASSWORD"  # noqa: S105 -- this is the env VAR NAME, never a secret value


def build_dsn(cfg: Config) -> str:
    """Build the Postgres connection string. PLAT-002: the password comes from
    `resolve_secret(OG_DB_PASSWORD)`, which raises `ConfigError` loudly when unset -- there is no
    silent empty-password fallback. PLAT-003: `psycopg.conninfo.make_conninfo` handles quoting/escaping
    (a password containing a space or `=` previously produced a malformed/misparsed DSN)."""
    host = cfg.get("postgres.host", DEFAULT_POSTGRES_HOST)
    port = cfg.get("postgres.port", DEFAULT_POSTGRES_PORT)
    database = cfg.postgres_database
    user = os.environ.get(_ENV_DB_USER, DEFAULT_POSTGRES_USER)
    password = resolve_secret(_ENV_DB_PASSWORD)
    return make_conninfo(host=str(host), port=port, dbname=database, user=user, password=password)


async def make_pool(cfg: Config) -> AsyncConnectionPool:
    dsn = build_dsn(cfg)
    pool_min = cfg.get("postgres.pool_min", DEFAULT_POOL_MIN_SIZE)
    pool_max = cfg.get("postgres.pool_max", DEFAULT_POOL_MAX_SIZE)
    pool = AsyncConnectionPool(dsn, min_size=pool_min, max_size=pool_max, open=False)
    await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_S)  # PLAT-004
    return pool


def _discover_migrations(migrations_dir: Path) -> list[Path]:
    files = []
    for p in sorted(migrations_dir.glob("*.sql")):
        if _FILENAME_RE.match(p.name):
            files.append(p)
    return files


_ENSURE_TABLE_SQL = """
CREATE SCHEMA IF NOT EXISTS og;
CREATE TABLE IF NOT EXISTS og.schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def migrate_sync(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Synchronous migration runner (used by the CLI and by integration tests' setup). Returns the
    list of filenames applied in this call."""
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(_ENSURE_TABLE_SQL)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM og.schema_migrations")
            already = {row[0] for row in cur.fetchall()}

        for path in _discover_migrations(migrations_dir):
            if path.name in already:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO og.schema_migrations (filename) VALUES (%s)", (path.name,))
            conn.commit()
            applied.append(path.name)
    return applied


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m opengrid.platform.db")
    parser.add_argument("command", choices=["migrate"])
    parser.add_argument("--config", default=os.environ.get("OG_CONFIG"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    dsn = build_dsn(cfg)
    if args.command == "migrate":
        applied = migrate_sync(dsn)
        if applied:
            print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
        else:
            print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
