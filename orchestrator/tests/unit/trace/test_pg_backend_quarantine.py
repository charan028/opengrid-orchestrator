"""`opengrid.trace.pg_backend` (workstation review R4 / B2, r3.4.1): a row Postgres refuses for its CONTENT
(e.g. a NUL in a jsonb string) is quarantined -- never journaled for replay, where it used to block replay for
every process forever -- and reported once; replay continues past such a row. And the DB-or-nothing mode
(`journal_failed_writes=False`) raises instead of journaling. No real Postgres (fake pool, `tmp_path`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from opengrid.trace import pg_backend
from opengrid.trace.pg_backend import (
    PgTraceBackend,
    TraceNotRecordedError,
    is_non_retryable,
    quarantine_path_for,
)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class _Pool:
    """Fake pool: `down` simulates an outage; an INSERT whose payload holds a NUL raises
    UntranslatableCharacter exactly as Postgres does for jsonb."""

    def __init__(self) -> None:
        self.down = False
        self.rows: list[dict[str, Any]] = []

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
        return None

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Cursor:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool
        self._result: list[tuple[Any, ...]] = []

    async def execute(self, query: str, params: Any = None) -> None:
        params = params or {}
        rows = self._pool.rows
        if "SELECT seq, hash FROM og.trace" in query:
            same = sorted((r for r in rows if r["stream_id"] == params["stream_id"]), key=lambda r: r["seq"])
            self._result = [(same[-1]["seq"], same[-1]["record_hash"])] if same else []
        elif "INSERT INTO og.trace" in query:
            payload = params["payload"].obj
            if "\x00" in json.dumps(payload) or "\\u0000" in json.dumps(payload):
                raise psycopg.errors.UntranslatableCharacter("unsupported Unicode escape sequence")
            if any(r["stream_id"] == params["stream_id"] and r["seq"] == params["seq"] for r in rows):
                raise psycopg.errors.UniqueViolation("ux_trace_stream_seq")
            rows.append(dict(params))
        else:  # og.alert and anything else the quarantine report tries: not modelled here
            raise psycopg.OperationalError("not modelled by this fake")

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def _insert(backend: PgTraceBackend, *, seq: int, payload: dict[str, Any], stream: str = "s1") -> str:
    trace_id = str(uuid4())
    await backend.insert_trace_row(
        trace_id=trace_id,  # type: ignore[arg-type]
        stream_id=stream,
        seq=seq,
        decision_type="ASSET_STATE_TRANSITION",
        event_class="DEVICE_INFO",
        payload=payload,
        reason_codes=None,
        prev_hash=None if seq == 0 else f"h{seq - 1}",
        record_hash=f"h{seq}",
        created_at=NOW,
    )
    return trace_id


def test_error_classification() -> None:
    assert is_non_retryable(psycopg.errors.UntranslatableCharacter("nul"))
    assert is_non_retryable(psycopg.errors.CheckViolation("check"))
    assert not is_non_retryable(psycopg.errors.UniqueViolation("dup"))
    assert not is_non_retryable(psycopg.OperationalError("down"))
    assert not is_non_retryable(OSError("disk"))


@pytest.mark.asyncio
async def test_a_refused_row_is_quarantined_not_journaled(tmp_path: Path) -> None:
    pool = _Pool()
    journal = tmp_path / "journal.jsonl"
    backend = PgTraceBackend(pool, journal_path=journal)

    trace_id = await _insert(backend, seq=0, payload={"manufacturer": "evil\x00corp"})

    assert [r for r in pool.rows if r["stream_id"] != pg_backend.QUARANTINE_STREAM] == []
    assert backend.pending_count() == 0  # never journaled: nothing to block replay
    lines = quarantine_path_for(journal).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["trace_id"] == trace_id
    assert "UntranslatableCharacter" in json.loads(lines[0])["error"]
    # the report traced it once, on its own stream, with identifiers only (never the refused payload)
    (report,) = [r for r in pool.rows if r["stream_id"] == pg_backend.QUARANTINE_STREAM]
    assert report["event_class"] == "TRACE_QUARANTINED" and report["payload"].obj["trace_id"] == trace_id
    assert "evil" not in json.dumps(report["payload"].obj)


@pytest.mark.asyncio
async def test_replay_quarantines_a_poisoned_entry_and_continues(tmp_path: Path) -> None:
    """B2: a poisoned row at the head of the shared journal used to stop replay for every process."""
    pool = _Pool()
    journal = tmp_path / "journal.jsonl"
    backend = PgTraceBackend(pool, journal_path=journal)
    pool.down = True
    # Journaled during an outage (connectivity error), before the content could be checked:
    await _insert(backend, seq=0, payload={"x": "bad\x00"}, stream="poisoned")
    await _insert(backend, seq=0, payload={"x": "fine"}, stream="healthy")
    assert backend.pending_count() == 2

    pool.down = False
    replayed = await backend.replay()

    assert replayed == 1
    assert backend.pending_count() == 0
    assert [r["stream_id"] for r in pool.rows if r["stream_id"] != pg_backend.QUARANTINE_STREAM] == [
        "healthy"
    ]
    assert len(quarantine_path_for(journal).read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_db_or_nothing_mode_raises_and_never_journals(tmp_path: Path) -> None:
    pool = _Pool()
    journal = tmp_path / "journal.jsonl"
    backend = PgTraceBackend(pool, journal_path=journal, journal_failed_writes=False)
    await _insert(backend, seq=0, payload={"ok": True})
    pool.down = True

    with pytest.raises(TraceNotRecordedError):
        await _insert(backend, seq=1, payload={"ok": True})

    assert backend.pending_count() == 0 and not journal.exists()
    pool.down = False
    with pytest.raises(TraceNotRecordedError):  # a refused row is not recorded either
        await _insert(backend, seq=1, payload={"x": "\x00"})
    assert [r["seq"] for r in pool.rows if r["stream_id"] == "s1"] == [0]


@pytest.mark.asyncio
async def test_the_default_mode_still_journals_an_outage(tmp_path: Path) -> None:
    pool = _Pool()
    backend = PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl")
    pool.down = True
    await _insert(backend, seq=0, payload={"ok": True})
    assert backend.pending_count() == 1
