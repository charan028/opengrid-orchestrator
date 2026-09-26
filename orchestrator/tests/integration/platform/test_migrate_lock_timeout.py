"""R3 review finding (2026-09-26): deploy.sh migrates while the previous release still writes
og.telemetry/og.hub_state, so a migration waiting on a lock must fail fast (lock_timeout) and be retried,
instead of queueing every writer behind it and stalling the engine.

Uses a scratch migrations directory with one migration that ALTERs a scratch table, while a second
session holds an ACCESS EXCLUSIVE lock on that table. DB-only; skipped when no database is reachable.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from opengrid.platform.config import load_config
from opengrid.platform.db import MigrationLockTimeoutError, build_dsn, migrate_sync

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        dsn = build_dsn(load_config(_CONFIG_PATH))
        with psycopg.connect(dsn, connect_timeout=2):
            return dsn
    except Exception:
        return None


_DSN = _dsn()
requires_db = pytest.mark.skipif(_DSN is None, reason="Postgres not reachable; run via tools/remote.ps1")


@pytest.fixture()
def scratch(tmp_path: Path):
    assert _DSN is not None
    suffix = uuid.uuid4().hex[:8]
    table = f"og.lock_probe_{suffix}"
    migration = f"9999_lock_probe_{suffix}.sql"
    (tmp_path / migration).write_text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS extra integer;\n")
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {table} (id integer)")
    yield tmp_path, table, migration
    with psycopg.connect(_DSN, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM og.schema_migrations WHERE filename = %s", (migration,))


def _recorded(migration: str) -> bool:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        row = conn.execute("SELECT 1 FROM og.schema_migrations WHERE filename = %s", (migration,)).fetchone()
    return row is not None


@requires_db
def test_a_blocked_migration_fails_fast_and_is_not_recorded(scratch) -> None:
    migrations_dir, table, migration = scratch
    sleeps: list[float] = []
    assert _DSN is not None
    with psycopg.connect(_DSN) as holder:
        holder.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")  # held until this block ends
        started = time.monotonic()
        with pytest.raises(MigrationLockTimeoutError):
            migrate_sync(_DSN, migrations_dir, lock_retries=1, sleep=sleeps.append)
        elapsed = time.monotonic() - started
    assert sleeps == [2.0]  # one retry, with the first backoff step
    assert elapsed < 20  # two 5 s lock timeouts, never an indefinite wait
    assert not _recorded(migration)


@requires_db
def test_a_migration_retried_after_the_lock_clears_is_applied(scratch) -> None:
    migrations_dir, table, migration = scratch
    assert _DSN is not None
    holder = psycopg.connect(_DSN)
    holder.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")

    def release_then_wait(_seconds: float) -> None:
        holder.rollback()  # the blocking session lets go during the backoff

    try:
        applied = migrate_sync(_DSN, migrations_dir, lock_retries=3, sleep=release_then_wait)
    finally:
        holder.close()
    assert migration in applied
    assert _recorded(migration)
