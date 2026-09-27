"""ES01-S05 (traceability-gap closure, 2026-09-26): seed a small `og.trace` chain in one database,
`pg_dump` it, restore the dump into a SECOND database, and confirm `TraceStore.verify()` still passes
against the restored copy -- proof that a backup/restore round trip preserves K10/K11's hash chain, not
just the row bytes.

DB-only (BUILD.md S6): run via
`powershell -File tools\\remote.ps1 -Ws tst -Cmd "cd orchestrator && python -m pytest tests/integration/platform/test_es01_s05_restore_and_verify.py -q"`.
Shells out to `pg_dump`/`psql`/`createdb`/`dropdb` on the SAME host `OG_DB` resolves to (never a second
connection profile) -- no orchestrator/simulator process is started, no MQTT touched (BUILD.md S6).

The `opengrid` role has no `CREATEDB` privilege on 192.168.5.35, so the second database is normally
pre-created by the lead as `<source_db>_restore` (e.g. `og_t_tst_restore`, owner `opengrid`). When that
database already exists, this test reuses it -- dropping its `og` schema (`CASCADE`) first so a
restore-then-restore rerun is idempotent -- instead of trying to create one. It falls back to `createdb`
(for a workstation/role that DOES have the privilege) only when the pre-created database is absent, and
SKIPS with a message naming exactly what's missing if neither path works.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from urllib.parse import urlparse

import psycopg
import pytest

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.trace.pg_backend import PgTraceBackend
from opengrid.trace.store import TraceStore

from ..cluster_guard import require_test_cluster

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        return build_dsn(load_config(_CONFIG_PATH))
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
_SKIP_REASON = "Postgres not reachable locally; run via tools/remote.ps1 -Ws tst (BUILD.md S6)."
requires_db = pytest.mark.skipif(_DSN is None or not _db_reachable(_DSN), reason=_SKIP_REASON)


def _conn_parts(dsn: str) -> dict[str, str]:
    """`psycopg`'s DSN is libpq keyword/value or a URI depending on `make_conninfo`'s output; parse
    either shape into the pieces `pg_dump`/`createdb`/`psql`'s CLI flags need."""
    if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
        parsed = urlparse(dsn)
        return {
            "host": parsed.hostname or "127.0.0.1",
            "port": str(parsed.port or 5432),
            "user": parsed.username or "opengrid",
            "password": parsed.password or "",
            "dbname": (parsed.path or "/og").lstrip("/"),
        }
    parts: dict[str, str] = {}
    for token in dsn.split():
        if "=" in token:
            key, _, value = token.partition("=")
            parts[key] = value.strip("'\"")
    return {
        "host": parts.get("host", "127.0.0.1"),
        "port": parts.get("port", "5432"),
        "user": parts.get("user", "opengrid"),
        "password": parts.get("password", ""),
        "dbname": parts.get("dbname", "og"),
    }


def _pg_env(parts: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env["PGPASSWORD"] = parts["password"]
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    # Args are built entirely from this test's own OG_CONFIG-resolved connection parts and fixed
    # pg_dump/pg_restore/createdb/dropdb flags -- never from request/network/untrusted input.
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


def _database_exists(source_dsn: str, name: str) -> bool:
    """Query `pg_catalog.pg_database` over the (already-reachable) SOURCE connection -- world-readable,
    needs no extra privilege -- rather than shelling out, so a missing `psql`/`createdb` on PATH can't
    be confused with "the database doesn't exist"."""
    with psycopg.connect(source_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (name,))
        return cur.fetchone() is not None


@requires_db
async def test_restored_database_still_verifies_the_trace_chain(tmp_path) -> None:
    assert _DSN is not None
    source_parts = _conn_parts(_DSN)
    env = _pg_env(source_parts)
    host_port_user = ["-h", source_parts["host"], "-p", source_parts["port"], "-U", source_parts["user"]]

    # -- 0. Prefer a pre-created "<source_db>_restore" database (the `opengrid` role has no CREATEDB on
    # 192.168.5.35 -- BUILD.md S6/lead notice); fall back to `createdb` only if it doesn't exist, and
    # skip with a precise reason if neither path is usable.
    preexisting_restore_db = f"{source_parts['dbname']}_restore"
    restore_db: str
    we_created_the_db: bool
    if _database_exists(_DSN, preexisting_restore_db):
        restore_db = preexisting_restore_db
        we_created_the_db = False
        # Idempotent rerun: drop the `og` schema (CASCADE takes `og.schema_migrations` and every table
        # with it) so `pg_restore`'s `CREATE SCHEMA og` below never collides with a prior run's rows.
        drop_schema = _run(
            ["psql", *host_port_user, "-d", restore_db, "-c", "DROP SCHEMA IF EXISTS og CASCADE"], env
        )
        assert drop_schema.returncode == 0, drop_schema.stderr
    else:
        restore_db = f"{source_parts['dbname']}_restore_{uuid.uuid4().hex[:8]}"
        require_test_cluster(restore_db, source_parts["port"])
        create = _run(["createdb", *host_port_user, restore_db], env)
        if create.returncode != 0:
            pytest.skip(
                f"neither a pre-created '{preexisting_restore_db}' database nor CREATEDB privilege is "
                f"available for role {source_parts['user']!r} on "
                f"{source_parts['host']}:{source_parts['port']} -- ask the lead to create "
                f"'{preexisting_restore_db}' (owner {source_parts['user']!r}), or grant CREATEDB. "
                f"createdb stderr: {create.stderr.strip()}"
            )
        we_created_the_db = True

    try:
        # -- 1. Seed a small real trace chain in the source database (og_t_<ws>) -----------------------
        migrate_sync(_DSN)
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(_DSN, min_size=1, max_size=2, open=False)
        await pool.open(wait=True)
        stream_id = f"restore-verify-{uuid.uuid4().hex[:8]}"
        try:
            backend = PgTraceBackend(pool)
            trace = TraceStore(backend)
            for i in range(5):
                await trace.append(stream_id, "ADMISSION", "ADMISSION", {"seq_marker": i}, ["R-GATE-SELECT"])
            before = await trace.verify(stream_id)
            assert before.ok, before
            before_seq, before_hash = await backend.last_head(stream_id)
        finally:
            await pool.close()

        # -- 2. pg_dump the source database, restore it into the fresh second database ------------------
        dump_path = tmp_path / "source.dump"
        dump = _run(["pg_dump", *host_port_user, "-Fc", "-f", str(dump_path), source_parts["dbname"]], env)
        assert dump.returncode == 0, dump.stderr

        restore = _run(
            [
                "pg_restore",
                *host_port_user,
                "-d",
                restore_db,
                "--no-owner",
                "--no-privileges",
                str(dump_path),
            ],
            env,
        )
        # pg_restore commonly exits 1 on benign warnings (e.g. "role does not exist" for --no-owner
        # skips); only a stderr mention of a hard FATAL/PANIC means the restore itself failed.
        assert "FATAL" not in restore.stderr and "PANIC" not in restore.stderr, restore.stderr

        # -- 3. Verify the SAME trace chain against the RESTORED database, independently ------------------
        restore_dsn = _DSN.replace(source_parts["dbname"], restore_db, 1)
        restore_pool = AsyncConnectionPool(restore_dsn, min_size=1, max_size=2, open=False)
        await restore_pool.open(wait=True)
        try:
            restored_backend = PgTraceBackend(restore_pool)
            restored_trace = TraceStore(restored_backend)
            after = await restored_trace.verify(stream_id)
            assert after.ok, after
            after_seq, after_hash = await restored_backend.last_head(stream_id)
            assert (after_seq, after_hash) == (before_seq, before_hash)
        finally:
            await restore_pool.close()
    finally:
        if we_created_the_db:
            _run(["dropdb", "--if-exists", *host_port_user, restore_db], env)
        else:
            # Leave the pre-created database itself alone (we don't own it / can't recreate it) --
            # just drop the schema this run restored, so the DB starts clean for the next run too.
            _run(["psql", *host_port_user, "-d", restore_db, "-c", "DROP SCHEMA IF EXISTS og CASCADE"], env)
