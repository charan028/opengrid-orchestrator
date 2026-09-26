"""opengrid.health.queries.write_hub_health_batch (defect fix, dispatch-live pass): proves the health
evaluator's hub-health write is one batched `UPDATE ... FROM (VALUES ...)` statement under asynchronous
commit, chunked at `_WRITE_HUB_HEALTH_CHUNK_SIZE` rows -- not one single-row `UPDATE`+commit per hub.
Minimal fake pool/cursor, no real DB (BUILD.md S5), mirroring tests/unit/fleet/test_pg_backend.py's
pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.health import queries

NOW = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 26, 0, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.asyncio


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        # Preserve the params' own shape: a dict (named placeholders, e.g. INSERT/DELETE here) stays a
        # dict; a list/tuple (positional placeholders, e.g. the batched hub-health UPDATE) is copied.
        if params is None:
            recorded: object = []
        elif isinstance(params, dict):
            recorded = dict(params)
        else:
            recorded = list(params)
        self.executed.append((str(sql), recorded))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


async def test_write_hub_health_batch_noop_on_empty_changes() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.write_hub_health_batch(pool, [])

    assert cursor.executed == []  # no round trip at all for an unchanged cycle


async def test_write_hub_health_batch_uses_async_commit_and_one_statement() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    changes = [("hub-1", "offline"), ("hub-2", "stale"), ("hub-3", "fault")]

    await queries.write_hub_health_batch(pool, changes)

    assert cursor.executed[0] == ("SET LOCAL synchronous_commit TO OFF", [])
    assert len(cursor.executed) == 2  # SET LOCAL + exactly one UPDATE statement, no per-row round trips
    statement, params = cursor.executed[1]
    assert "UPDATE og.hub_state" in statement
    assert "FROM (VALUES" in statement
    assert params == ["hub-1", "offline", "hub-2", "stale", "hub-3", "fault"]
    assert pool._conn.committed is True


async def test_write_hub_health_batch_chunks_at_500_rows() -> None:
    """Benchmark-style shape check: 1,200 changed hubs (dispatch-live pass's ~2,000/cycle scale) become
    exactly 3 chunked UPDATE statements (500 + 500 + 200), still one commit for the whole batch."""
    cursor = FakeCursor()
    pool = FakePool(cursor)
    changes = [(f"hub-{i}", "offline") for i in range(1_200)]

    await queries.write_hub_health_batch(pool, changes)

    update_statements = [(s, p) for s, p in cursor.executed if "UPDATE og.hub_state" in s]
    assert len(update_statements) == 3
    assert [len(p) // 2 for _s, p in update_statements] == [500, 500, 200]
    assert pool._conn.committed is True


class FetchingFakeCursor(FakeCursor):
    """Like `FakeCursor`, but `execute` on the configured SELECT returns `rows` from `fetchall`, so
    `write_degraded_modes`'s internal `fetch_degraded_modes` read has something to work with."""

    def __init__(self, rows: list[tuple[str, object]]) -> None:
        super().__init__()
        self._rows = rows

    async def fetchall(self):
        return self._rows


async def test_write_degraded_modes_noop_when_nothing_changed() -> None:
    """Defect fix (R2): `evaluate_once` now persists degraded modes every cycle -- when the active set
    already matches what's stored, this must be a read-only no-op (no INSERT/DELETE/commit)."""
    cursor = FetchingFakeCursor(rows=[("HOLD_LOCAL_AUTONOMY", SINCE)])
    pool = FakePool(cursor)

    await queries.write_degraded_modes(pool, frozenset({"HOLD_LOCAL_AUTONOMY"}), now=NOW)

    assert len(cursor.executed) == 1  # only the SELECT from fetch_degraded_modes
    assert pool._conn.committed is False


async def test_write_degraded_modes_inserts_new_and_deletes_resolved() -> None:
    cursor = FetchingFakeCursor(rows=[("HOLD_LOCAL_AUTONOMY", SINCE)])
    pool = FakePool(cursor)

    await queries.write_degraded_modes(pool, frozenset({"NO_NEW_COMMITMENTS"}), now=NOW)

    statements = [s for s, _p in cursor.executed]
    assert "SET LOCAL synchronous_commit TO OFF" in statements
    inserted = [p for s, p in cursor.executed if "INSERT INTO og.degraded_mode_state" in s]
    deleted = [p for s, p in cursor.executed if "DELETE FROM og.degraded_mode_state" in s]
    assert inserted == [{"mode": "NO_NEW_COMMITMENTS", "since": NOW}]
    assert deleted == [{"mode": "HOLD_LOCAL_AUTONOMY"}]
    assert pool._conn.committed is True


async def test_fetch_degraded_modes_returns_mode_since_pairs() -> None:
    cursor = FetchingFakeCursor(rows=[("HOLD", SINCE)])
    pool = FakePool(cursor)

    result = await queries.fetch_degraded_modes(pool)

    assert result == [("HOLD", SINCE)]
