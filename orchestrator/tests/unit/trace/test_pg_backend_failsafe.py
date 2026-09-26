"""Unit tests for `opengrid.trace.pg_backend`'s K11 fail-safe local journal (adversarial review): a
trace row that can't reach Postgres is journaled instead of lost, `last_head` still resolves correctly
while the DB is down, and `replay()` drains the journal in order once it recovers. No real Postgres or
filesystem beyond pytest's own `tmp_path` (BUILD.md S5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from opengrid.trace.pg_backend import PgTraceBackend, TraceJournalUnavailableError

pytestmark = pytest.mark.asyncio


class _TogglePool:
    """A minimal fake `AsyncConnectionPool`: `.connection()` raises (simulating an outage) while `down`
    is True, else hands back a working in-memory connection/cursor pair backed by `rows` (a plain list
    standing in for `og.trace`)."""

    def __init__(self) -> None:
        self.down = False
        self.rows: list[dict] = []

    def connection(self):
        if self.down:
            raise psycopg.OperationalError("simulated outage")
        return _Conn(self)


class _Conn:
    def __init__(self, pool: _TogglePool) -> None:
        self._pool = pool

    def cursor(self):
        return _Cursor(self._pool)

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, pool: _TogglePool) -> None:
        self._pool = pool
        self._result: list[tuple] = []

    async def execute(self, query: str, params=None) -> None:
        params = params or {}
        rows = self._pool.rows
        if "SELECT seq, hash FROM og.trace" in query:
            same = sorted((r for r in rows if r["stream_id"] == params["stream_id"]), key=lambda r: r["seq"])
            self._result = [(same[-1]["seq"], same[-1]["record_hash"])] if same else []
        elif "INSERT INTO og.trace" in query:
            stream_id, seq = params["stream_id"], params["seq"]
            if any(r["stream_id"] == stream_id and r["seq"] == seq for r in rows):
                raise psycopg.errors.UniqueViolation("ux_trace_stream_seq")
            rows.append(dict(params))
        else:  # pragma: no cover
            raise AssertionError(f"fake cursor does not understand query: {query!r}")

    async def fetchone(self):
        return self._result[0] if self._result else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


async def _append(backend: PgTraceBackend, *, stream_id: str, seq: int, prev_hash: str | None) -> str:
    record_hash = f"hash-{stream_id}-{seq}"
    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id=stream_id,
        seq=seq,
        decision_type="ALERT",
        event_class="TEST",
        payload={"seq": seq},
        reason_codes=None,
        prev_hash=prev_hash,
        record_hash=record_hash,
        created_at=NOW,
    )
    return record_hash


async def test_insert_falls_back_to_journal_when_db_unavailable(tmp_path: Path) -> None:
    pool = _TogglePool()
    journal_path = tmp_path / "journal.jsonl"
    backend = PgTraceBackend(pool, journal_path=journal_path)

    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")

    assert [r["seq"] for r in pool.rows] == [0]  # the second row never reached the "database"
    assert backend.pending_count() == 1
    assert journal_path.exists()


async def test_last_head_resolves_from_journal_while_db_is_down(tmp_path: Path) -> None:
    pool = _TogglePool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")

    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")

    seq, head_hash = await backend.last_head("s1")
    assert (seq, head_hash) == (1, "hash-s1-1")


async def test_last_head_refuses_unknown_stream_with_no_journal_or_cache(tmp_path: Path) -> None:
    pool = _TogglePool()
    pool.down = True
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")

    with pytest.raises(TraceJournalUnavailableError):
        await backend.last_head("never-seen-stream")


async def test_replay_drains_the_journal_in_order_once_recovered(tmp_path: Path) -> None:
    pool = _TogglePool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")

    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")
    await _append(backend, stream_id="s1", seq=2, prev_hash="hash-s1-1")
    assert backend.pending_count() == 2

    pool.down = False
    replayed = await backend.replay()

    assert replayed == 2
    assert backend.pending_count() == 0
    assert [r["seq"] for r in pool.rows] == [0, 1, 2]  # order preserved


async def test_replay_treats_already_applied_rows_as_success_and_continues(tmp_path: Path) -> None:
    """A partially-successful earlier replay (row 1 landed, then the DB dropped again before row 2) must
    not re-raise on retrying row 1 -- a UNIQUE violation there means "already applied", not a failure."""
    pool = _TogglePool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")
    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")
    await _append(backend, stream_id="s1", seq=2, prev_hash="hash-s1-1")

    # Simulate row 1 having already landed in a prior partial replay.
    pool.down = False
    pool.rows.append({"stream_id": "s1", "seq": 1})
    pool.down = True
    pool.down = False

    replayed = await backend.replay()

    assert replayed == 2
    assert backend.pending_count() == 0


async def test_replay_stops_and_keeps_remaining_entries_while_still_down(tmp_path: Path) -> None:
    pool = _TogglePool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")
    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")
    await _append(backend, stream_id="s1", seq=2, prev_hash="hash-s1-1")

    replayed = await backend.replay()  # still down

    assert replayed == 0
    assert backend.pending_count() == 2


async def test_insert_self_heals_by_draining_journal_before_its_own_write(tmp_path: Path) -> None:
    """The opportunistic drain at the top of last_head/insert_trace_row: the very next successful call
    after an outage clears the backlog with no separate replay job needed."""
    pool = _TogglePool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")
    await _append(backend, stream_id="s1", seq=0, prev_hash=None)
    pool.down = True
    await _append(backend, stream_id="s1", seq=1, prev_hash="hash-s1-0")
    assert backend.pending_count() == 1

    pool.down = False
    await _append(backend, stream_id="s1", seq=2, prev_hash="hash-s1-1")

    assert backend.pending_count() == 0
    assert [r["seq"] for r in pool.rows] == [0, 1, 2]
