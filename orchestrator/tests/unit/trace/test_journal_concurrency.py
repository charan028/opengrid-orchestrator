"""The K11 trace journal is shared by every process (engine, guardian, feeds, settle, api, ...): no
append may ever be lost to another process's replay (review finding, R3).

The race being closed: replay() read the journal, inserted into Postgres, then rewrote the file through
a replace -- while another process appended to the old file between the read and the replace, silently
dropping that entry and breaking the hash chain. These tests run real concurrent OS processes against
one journal file on local disk (no Postgres: each process has an in-memory fake pool).
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from opengrid.trace import pg_backend
from opengrid.trace.pg_backend import PgTraceBackend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock and fork are POSIX-only")

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
#: Rows per writer process. Every append is fsynced, so this is kept modest for a slow disk; the race
#: window is widened by the replayer's slow fake database instead.
PER_WRITER = 40


class _Pool:
    """Fake `AsyncConnectionPool`. `down` raises on connect (an outage); otherwise inserts go to `rows`,
    optionally after `delay_s` (a slow DB widens the replay window the race needs), or after `gate` is
    set (to pin a replay mid-flight)."""

    def __init__(self, *, down: bool, delay_s: float = 0.0, gate: asyncio.Event | None = None) -> None:
        self.down = down
        self.delay_s = delay_s
        self.gate = gate
        self.entered = asyncio.Event()
        self.rows: dict[tuple[str, int], str] = {}

    def connection(self) -> _Conn:
        if self.down:
            raise psycopg.OperationalError("simulated outage")
        return _Conn(self)


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    def cursor(self) -> _Cursor:
        return _Cursor(self._pool)

    async def commit(self) -> None:
        pass

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Cursor:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        assert params is not None and "INSERT INTO og.trace" in query
        self._pool.entered.set()
        if self._pool.gate is not None:
            await self._pool.gate.wait()
        if self._pool.delay_s:
            await asyncio.sleep(self._pool.delay_s)
        key = (params["stream_id"], params["seq"])
        if key in self._pool.rows:
            raise psycopg.errors.UniqueViolation("ux_trace_stream_seq")
        self._pool.rows[key] = params["record_hash"]

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _insert(backend: PgTraceBackend, stream_id: str, seq: int) -> None:
    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id=stream_id,
        seq=seq,
        decision_type="ALERT",
        event_class="TEST",
        payload={"seq": seq},
        reason_codes=None,
        prev_hash=None if seq == 0 else f"hash-{stream_id}-{seq - 1}",
        record_hash=f"hash-{stream_id}-{seq}",
        created_at=NOW,
    )


# --- child processes (module level so they can be started by multiprocessing) --------------------


def _writer_process(journal: str, stream_id: str, count: int, start: Any) -> None:
    """A process whose DB is down: every row it writes goes to the shared journal."""

    async def run() -> None:
        backend = PgTraceBackend(_Pool(down=True), journal_path=Path(journal))  # type: ignore[arg-type]
        start.wait()
        for seq in range(count):
            await _insert(backend, stream_id, seq)

    asyncio.run(run())


def _replayer_process(journal: str, result: str, start: Any, writers_done: Any) -> None:
    """A process whose DB has come back (slowly): it keeps replaying the shared journal while the
    writers are still appending, then drains what is left and reports every row it inserted."""

    async def run() -> list[list[Any]]:
        pool = _Pool(down=False, delay_s=0.0005)
        backend = PgTraceBackend(pool, journal_path=Path(journal))  # type: ignore[arg-type]
        start.wait()
        while not writers_done.is_set():
            await backend.replay()
            await asyncio.sleep(0.001)  # like production: replays are triggered by trace calls, not spun
        await backend.replay()
        return sorted([s, q] for s, q in pool.rows)

    Path(result).write_text(json.dumps(asyncio.run(run())), encoding="utf-8")


def _run_writers_and_replayer(tmp_path: Path) -> tuple[set[tuple[str, int]], int]:
    ctx = multiprocessing.get_context("fork")
    journal = str(tmp_path / "trace_journal.jsonl")
    result = str(tmp_path / "replayed.json")
    start, writers_done = ctx.Event(), ctx.Event()
    writers = [
        ctx.Process(target=_writer_process, args=(journal, f"w{i}", PER_WRITER, start)) for i in range(2)
    ]
    replayer = ctx.Process(target=_replayer_process, args=(journal, result, start, writers_done))
    for proc in [*writers, replayer]:
        proc.start()
    start.set()
    for proc in writers:
        proc.join(timeout=120)
        assert proc.exitcode == 0, "writer process failed"
    writers_done.set()
    replayer.join(timeout=120)
    assert replayer.exitcode == 0, "replayer process failed"
    replayed = {(s, q) for s, q in json.loads(Path(result).read_text(encoding="utf-8"))}
    leftover = PgTraceBackend(_Pool(down=True), journal_path=Path(journal)).pending_count()  # type: ignore[arg-type]
    return replayed, leftover


def test_no_entry_is_lost_when_two_writers_append_during_a_replay(tmp_path: Path) -> None:
    """Two writer processes append PER_WRITER rows each while a third replays the same journal in a loop.
    Every row must end up in the (fake) database exactly once, and the journal must end empty."""
    replayed, leftover = _run_writers_and_replayer(tmp_path)

    expected = {(f"w{i}", seq) for i in range(2) for seq in range(PER_WRITER)}
    assert expected - replayed == set(), f"lost {len(expected - replayed)} journaled rows"
    assert leftover == 0
    assert not list(tmp_path.glob("*.tmp")), "a temp rewrite file was left behind"


def test_every_line_in_a_concurrently_appended_journal_is_intact(tmp_path: Path) -> None:
    """Two writer processes only (the DB stays down): no line is torn or interleaved, none is missing."""
    ctx = multiprocessing.get_context("fork")
    journal = tmp_path / "trace_journal.jsonl"
    start = ctx.Event()
    writers = [
        ctx.Process(target=_writer_process, args=(str(journal), f"w{i}", PER_WRITER, start)) for i in range(2)
    ]
    for proc in writers:
        proc.start()
    start.set()
    for proc in writers:
        proc.join(timeout=120)
        assert proc.exitcode == 0

    lines = journal.read_text(encoding="utf-8").splitlines()
    keys = [(json.loads(line)["stream_id"], json.loads(line)["seq"]) for line in lines]
    assert sorted(keys) == sorted((f"w{i}", seq) for i in range(2) for seq in range(PER_WRITER))
    for i in range(2):  # each writer's own rows stay in its write order
        assert [seq for stream, seq in keys if stream == f"w{i}"] == list(range(PER_WRITER))


# --- deterministic, in-process: the exact interleaving the review found ------------------------------


async def test_an_append_between_replay_read_and_rewrite_survives(tmp_path: Path) -> None:
    """Replay has read the journal and is mid-insert when another writer appends. The rewrite must keep
    that append. The append must also not wait on the replay's database round trip."""
    journal = tmp_path / "trace_journal.jsonl"
    writer = PgTraceBackend(_Pool(down=True), journal_path=journal)  # type: ignore[arg-type]
    await _insert(writer, "a", 0)

    gate = asyncio.Event()
    slow_pool = _Pool(down=False, gate=gate)
    replayer = PgTraceBackend(slow_pool, journal_path=journal)  # type: ignore[arg-type]
    replay = asyncio.create_task(replayer.replay())
    await asyncio.wait_for(slow_pool.entered.wait(), timeout=5)  # read done, insert in flight

    await asyncio.wait_for(writer._append_journal(_entry("b", 0)), timeout=1)  # no lock held by replay now

    gate.set()
    assert await asyncio.wait_for(replay, timeout=5) == 1

    assert ("a", 0) in slow_pool.rows
    assert _journal_streams(journal) == ["b"], "the entry appended during the replay was dropped"


async def test_two_replayers_racing_apply_each_entry_once_and_drop_nothing(tmp_path: Path) -> None:
    journal = tmp_path / "trace_journal.jsonl"
    writer = PgTraceBackend(_Pool(down=True), journal_path=journal)  # type: ignore[arg-type]
    for seq in range(5):
        await _insert(writer, "s", seq)
    gate = asyncio.Event()
    shared = _Pool(down=False, gate=gate)  # one "database" both replayers write to

    first = PgTraceBackend(shared, journal_path=journal)  # type: ignore[arg-type]
    second = PgTraceBackend(shared, journal_path=journal)  # type: ignore[arg-type]
    replays = asyncio.gather(first.replay(), second.replay())
    await asyncio.wait_for(shared.entered.wait(), timeout=5)
    await asyncio.sleep(0.01)  # both have read their snapshot
    await writer._append_journal(_entry("late", 0))
    gate.set()
    await asyncio.wait_for(replays, timeout=5)

    assert sorted(shared.rows) == [("s", seq) for seq in range(5)]
    assert _journal_streams(journal) == ["late"]


async def test_a_cancelled_lock_wait_releases_nothing_it_does_not_hold(tmp_path: Path) -> None:
    """Acquisition is non-blocking polling, so a cancelled waiter leaves no stray lock behind."""
    journal = tmp_path / "trace_journal.jsonl"
    async with pg_backend._journal_lock(journal, exclusive=True):
        waiter = asyncio.create_task(PgTraceBackend(_Pool(down=True), journal_path=journal).replay())  # type: ignore[arg-type]
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    # The lock is free again: a fresh exclusive acquisition succeeds promptly.
    async with asyncio.timeout(1):
        async with pg_backend._journal_lock(journal, exclusive=True):
            pass


def test_the_lock_is_a_stable_sidecar_and_the_rewrite_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "trace_journal.jsonl"
    backend = PgTraceBackend(_Pool(down=True), journal_path=journal)  # type: ignore[arg-type]
    asyncio.run(_insert(backend, "s", 0))
    lock = tmp_path / "trace_journal.jsonl.lock"
    lock_inode = os.stat(lock).st_ino

    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(pg_backend.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    backend._rewrite_journal_locked([])

    assert os.stat(lock).st_ino == lock_inode, "replace must never touch the lock file"
    assert len(synced) == 2, "the temp file and the directory are both fsynced"
    assert journal.read_text(encoding="utf-8") == ""


def _journal_streams(journal: Path) -> list[str]:
    return [json.loads(line)["stream_id"] for line in journal.read_text(encoding="utf-8").splitlines()]


def _entry(stream_id: str, seq: int) -> pg_backend._JournalEntry:
    return pg_backend._JournalEntry(
        trace_id=str(uuid4()),
        stream_id=stream_id,
        seq=seq,
        decision_type="ALERT",
        event_class="TEST",
        payload={"seq": seq},
        reason_codes=None,
        prev_hash=None,
        record_hash=f"hash-{stream_id}-{seq}",
        created_at=NOW.isoformat(),
    )
