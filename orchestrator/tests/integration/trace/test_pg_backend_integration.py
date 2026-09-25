"""Integration tests for `opengrid.trace.pg_backend` against real Postgres (`og_t_hlth` on the server,
BUILD.md S5). Proves concurrency-safety and the retention pruning job over the real `og.trace` /
`og.trace_checkpoint` / `og.retention_policy` schema, complementing `tests/unit/trace/test_pg_backend.py`'s
fake-pool coverage.

Run via `powershell -File tools\\remote.ps1 -Ws hlth -Cmd "cd orchestrator && bash tools/check.sh"`, or
directly with `OG_CONFIG`/`OG_DB` set and Postgres reachable. Skipped automatically when no database is
reachable (e.g. local Windows dev box, BUILD.md S5's "local: no DB").
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.trace.pg_backend import PgTraceBackend, TraceAppendConflictError, run_retention_prune_job
from opengrid.trace.store import TraceStore

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        cfg = load_config(_CONFIG_PATH)
        return build_dsn(cfg)
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
_SKIP_REASON = "Postgres not reachable locally; run via tools/remote.ps1 -Ws hlth (BUILD.md S5)"
requires_db = pytest.mark.skipif(_DSN is None or not _db_reachable(_DSN), reason=_SKIP_REASON)


@requires_db
async def test_concurrent_appends_leave_a_verifiable_chain() -> None:
    """TS-09-02/09: N concurrent writers to the same stream_id -- exactly one wins per seq, losers get
    a clear `TraceAppendConflictError`, and the surviving chain still verifies (K10, K11)."""
    assert _DSN is not None
    migrate_sync(_DSN)
    stream_id = f"test-stream-{uuid4()}"

    pool = AsyncConnectionPool(_DSN, min_size=4, max_size=8, open=False)
    await pool.open(wait=True)
    try:
        store = TraceStore(PgTraceBackend(pool))
        await store.append(stream_id, "ADMISSION", "ADMISSION", {"seed": True})

        results = await asyncio.gather(
            *(store.append(stream_id, "ADMISSION", "ADMISSION", {"n": n}) for n in range(10)),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert all(isinstance(f, TraceAppendConflictError) for f in failures)
        assert len(successes) >= 1

        verify_result = await store.verify(stream_id)
        assert verify_result.ok
    finally:
        await pool.close()


@requires_db
async def test_retention_prune_job_respects_checkpoint_coverage() -> None:
    """TS-09-06/07: pruning removes only rows older than their event class's own retention window, and
    only once a checkpoint covers them; `verify()` from the surviving prefix still passes (K11)."""
    assert _DSN is not None
    migrate_sync(_DSN)
    stream_id = f"test-stream-{uuid4()}"
    event_class = "RT_ALLOCATION"  # 90-day default retention (02a S8.1)

    pool = AsyncConnectionPool(_DSN, min_size=2, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        backend = PgTraceBackend(pool)
        store = TraceStore(backend)
        for i in range(20):
            await store.append(stream_id, "RT_ALLOCATION", event_class, {"i": i})
        await store.checkpoint()

        # Backdate all but the last two rows so most of the stream is past its class's retention
        # window, while leaving a fresh tail -- pruning a stream down to nothing isn't representative
        # (a live stream always keeps appending) and would leave nothing for verify() to check below.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE og.trace SET created_at = %s WHERE stream_id = %s AND seq < 18",
                (datetime.now(UTC) - timedelta(days=200), stream_id),
            )
            await conn.commit()

        deleted = await run_retention_prune_job(pool)
        assert deleted.get(event_class, 0) > 0

        remaining = await backend.fetch_range(stream_id, from_seq=0)
        assert remaining, "the checkpoint's own head row must survive so verify() has something to check"
        from_seq = remaining[0].seq
        result = await store.verify(stream_id, from_seq=from_seq)
        assert result.ok
    finally:
        await pool.close()
