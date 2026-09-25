"""Integration fixtures: real Postgres (`OG_DB`, default `og_t_api` when run via
`tools/remote.ps1 -Ws api`), migrated schema, real `PgStore`/`PgTraceBackend`/`TestClient` wiring.
Skips the whole module if Postgres is unreachable (BUILD.md S5: these tests belong on the server)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from opengrid.api.app import create_app
from opengrid.api.deps import get_config, get_pool, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalStore
from opengrid.api.store import PgStore
from opengrid.api.trace_backend import PgTraceBackend
from opengrid.platform.config import Config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.trace.store import TraceStore

OPERATOR_HEADERS = {"X-Remote-User": "operator"}
VIEWER_HEADERS = {"X-Remote-User": "viewer"}


def _integration_config() -> Config:
    return Config(
        {
            "postgres": {
                "host": os.environ.get("PGHOST", "127.0.0.1"),
                "port": int(os.environ.get("PGPORT", "5432")),
                "database": os.environ.get("OG_DB", "og_t_api"),
                "pool_min": 1,
                "pool_max": 4,
            },
            "api": {"sse_heartbeat_s": 15},
        }
    )


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    cfg = _integration_config()
    dsn = build_dsn(cfg)
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    migrate_sync(dsn)
    return dsn


@pytest.fixture
def client(pg_dsn: str) -> Iterator[TestClient]:
    import asyncio

    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(pg_dsn, min_size=1, max_size=4, open=False)
    asyncio.run(pool.open(wait=True))

    app = create_app()
    cfg = _integration_config()
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_store] = lambda: PgStore(pool)
    app.dependency_overrides[get_trace_store] = lambda: TraceStore(PgTraceBackend(pool))
    app.dependency_overrides[get_proposals] = lambda: ProposalStore()

    # Not entered as a context manager: `create_app()`'s lifespan calls `load_config()`/`make_pool()`
    # unconditionally, which needs `OG_CONFIG` set. Every dependency it would populate is already
    # overridden above, so lifespan startup/shutdown is not needed for these tests.
    test_client = TestClient(app, client=("127.0.0.1", 51234))
    yield test_client

    asyncio.run(pool.close())
