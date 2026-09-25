"""Unit tests for `opengrid.trace.pg_backend` (02a S8, K10, K11). No real Postgres here (BUILD.md S5:
local unit tests have no DB) -- `_FakePool` is a minimal in-memory stand-in for
`psycopg_pool.AsyncConnectionPool` that understands exactly the SQL statements `pg_backend` issues
(matched by identity against the module's own `_..._SQL` constants), including the UNIQUE-violation and
checkpoint-coverage semantics a real `og.trace`/`og.trace_checkpoint` schema enforces. Concurrency-safety
under a *real* database (row locks, actual UNIQUE index behaviour) is proven separately by
`tests/integration/trace/test_pg_backend_integration.py` against `og_t_hlth`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from opengrid.trace import pg_backend
from opengrid.trace.pg_backend import DEFAULT_RETENTION_DAYS, PgTraceBackend, TraceAppendConflictError
from opengrid.trace.store import TraceStore

# `asyncio_mode = "auto"` (pyproject.toml) runs every `async def test_*` here without a marker; the
# property test at the bottom is intentionally synchronous (it drives `asyncio.run()` once per Hypothesis
# example itself), so no module-wide `pytestmark` is applied here.


# --------------------------------------------------------------------------------------------------
# Fake pool: understands exactly the queries pg_backend.py issues.
# --------------------------------------------------------------------------------------------------


@dataclass
class _FakeDb:
    trace: list[dict[str, Any]] = field(default_factory=list)
    trace_checkpoint: list[dict[str, Any]] = field(default_factory=list)
    retention_policy: dict[str, tuple[int, bool]] = field(default_factory=dict)


def _unwrap(value: Any) -> Any:
    """Unwrap a `psycopg.types.json.Jsonb`/`Json` parameter to its plain Python object."""
    return value.obj if hasattr(value, "obj") else value


class _FakeCursor:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def _covered_max_seq(self) -> dict[str, int]:
        covered: dict[str, int] = {}
        for cp in self._db.trace_checkpoint:
            for stream_id, head in cp["stream_heads"].items():
                covered[stream_id] = max(covered.get(stream_id, -1), head["seq"])
        return covered

    async def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        # A real network round-trip to Postgres always yields control back to the event loop; force the
        # same here so concurrent callers genuinely interleave instead of each running to completion
        # uninterrupted (which would never exercise the UNIQUE-violation race this fake exists to test).
        await asyncio.sleep(0)
        params = params or {}
        db = self._db

        if query == pg_backend._LAST_HEAD_SQL:
            rows = sorted(
                (r for r in db.trace if r["stream_id"] == params["stream_id"]), key=lambda r: r["seq"]
            )
            self._rows = [(rows[-1]["seq"], rows[-1]["hash"])] if rows else []

        elif query == pg_backend._INSERT_TRACE_SQL:
            stream_id, seq, prev_hash = params["stream_id"], params["seq"], params["prev_hash"]
            same_stream = [r for r in db.trace if r["stream_id"] == stream_id]
            if any(r["seq"] == seq for r in same_stream):
                raise psycopg.errors.UniqueViolation("ux_trace_stream_seq")
            if prev_hash is not None and any(r["prev_hash"] == prev_hash for r in same_stream):
                raise psycopg.errors.UniqueViolation("ux_trace_stream_prev")
            db.trace.append(
                {
                    "trace_id": params["trace_id"],
                    "decision_type": params["decision_type"],
                    "event_class": params["event_class"],
                    "stream_id": stream_id,
                    "seq": seq,
                    "payload": _unwrap(params["payload"]),
                    "reason_codes": params["reason_codes"],
                    "prev_hash": prev_hash,
                    "hash": params["record_hash"],
                    "created_at": params["created_at"],
                }
            )
            self.rowcount = 1

        elif query == pg_backend._EXISTS_PREIMAGE_SQL:
            self._rows = [(any(r["trace_id"] == params["trace_id"] for r in db.trace),)]

        elif query == pg_backend._FETCH_RANGE_SQL:
            rows = sorted(
                (
                    r
                    for r in db.trace
                    if r["stream_id"] == params["stream_id"] and r["seq"] >= params["from_seq"]
                ),
                key=lambda r: r["seq"],
            )
            self._rows = [
                (r["seq"], r["decision_type"], r["event_class"], r["payload"], r["prev_hash"], r["hash"])
                for r in rows
            ]

        elif query == pg_backend._STREAM_IDS_SQL:
            self._rows = [(sid,) for sid in sorted({r["stream_id"] for r in db.trace})]

        elif query == pg_backend._INSERT_CHECKPOINT_SQL:
            db.trace_checkpoint.append(
                {
                    "checkpoint_id": params["checkpoint_id"],
                    "checkpoint_at": params["checkpoint_at"],
                    "stream_heads": _unwrap(params["stream_heads"]),
                    "checkpoint_hash": params["checkpoint_hash"],
                }
            )
            self.rowcount = 1

        elif query == pg_backend._PRUNE_BEFORE_SQL:
            covered = self._covered_max_seq()
            stream_id, keep_from_seq = params["stream_id"], params["keep_from_seq"]
            max_seq = covered.get(stream_id)
            before = len(db.trace)
            db.trace = [
                r
                for r in db.trace
                if not (
                    r["stream_id"] == stream_id
                    and r["seq"] < keep_from_seq
                    and max_seq is not None
                    and r["seq"] <= max_seq
                )
            ]
            self.rowcount = before - len(db.trace)

        elif query == pg_backend._RETENTION_DAYS_SQL:
            policy = db.retention_policy.get(params["event_class"])
            self._rows = [(policy[0],)] if policy else []

        elif query == pg_backend._RETENTION_POLICIES_SQL:
            self._rows = [
                (event_class, days) for event_class, (days, prune) in db.retention_policy.items() if prune
            ]

        elif query == pg_backend._PRUNE_BY_CLASS_SQL:
            covered = self._covered_max_seq()
            event_class, cutoff = params["event_class"], params["cutoff"]
            before = len(db.trace)

            def _keep(r: dict[str, Any]) -> bool:
                if r["event_class"] != event_class or r["created_at"] >= cutoff:
                    return True
                max_seq = covered.get(r["stream_id"])
                return max_seq is None or r["seq"] > max_seq

            db.trace = [r for r in db.trace if _keep(r)]
            self.rowcount = before - len(db.trace)

        else:  # pragma: no cover -- a real Postgres would fail loudly too on an unrecognised statement
            raise AssertionError(f"fake pool does not understand query: {query!r}")

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConnection:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        return False


class _FakePool:
    """Stands in for `psycopg_pool.AsyncConnectionPool`: `.connection()` returns a fresh async
    context manager each call, exactly like the real pool's checkout/checkin."""

    def __init__(self) -> None:
        self.db = _FakeDb()

    def connection(self) -> _FakeConnection:
        return _FakeConnection(self.db)


@pytest.fixture
def fake_pool() -> _FakePool:
    return _FakePool()


@pytest.fixture
def backend(fake_pool: _FakePool) -> PgTraceBackend:
    return PgTraceBackend(fake_pool)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------
# last_head / insert_trace_row / exists_preimage
# --------------------------------------------------------------------------------------------------


async def test_last_head_empty_stream(backend: PgTraceBackend) -> None:
    assert await backend.last_head("nonexistent") == (-1, None)


async def test_insert_then_last_head(backend: PgTraceBackend) -> None:
    trace_id = uuid4()
    await backend.insert_trace_row(
        trace_id=trace_id,
        stream_id="selector",
        seq=0,
        decision_type="ADMISSION",
        event_class="ADMISSION",
        payload={"a": 1},
        reason_codes=None,
        prev_hash=None,
        record_hash="h0",
        created_at=datetime.now(UTC),
    )
    assert await backend.last_head("selector") == (0, "h0")


async def test_insert_conflict_on_duplicate_seq_raises_trace_append_conflict(
    backend: PgTraceBackend,
) -> None:
    kwargs = {
        "stream_id": "selector",
        "seq": 0,
        "decision_type": "ADMISSION",
        "event_class": "ADMISSION",
        "payload": {"a": 1},
        "reason_codes": None,
        "prev_hash": None,
        "created_at": datetime.now(UTC),
    }
    await backend.insert_trace_row(trace_id=uuid4(), record_hash="h0", **kwargs)
    with pytest.raises(TraceAppendConflictError):
        await backend.insert_trace_row(trace_id=uuid4(), record_hash="h0-again", **kwargs)


async def test_exists_preimage(backend: PgTraceBackend) -> None:
    trace_id = uuid4()
    assert not await backend.exists_preimage(trace_id)
    await backend.insert_trace_row(
        trace_id=trace_id,
        stream_id="guardian",
        seq=0,
        decision_type="GUARDIAN_VERDICT",
        event_class="GUARDIAN_VERDICT",
        payload={"outcome": "PASS"},
        reason_codes=None,
        prev_hash=None,
        record_hash="h0",
        created_at=datetime.now(UTC),
    )
    assert await backend.exists_preimage(trace_id)


# --------------------------------------------------------------------------------------------------
# fetch_range / stream_ids
# --------------------------------------------------------------------------------------------------


async def test_fetch_range_orders_by_seq_and_filters_from_seq(backend: PgTraceBackend) -> None:
    for i in range(5):
        await backend.insert_trace_row(
            trace_id=uuid4(),
            stream_id="allocator-bank-01",
            seq=i,
            decision_type="RT_ALLOCATION",
            event_class="RT_ALLOCATION",
            payload={"i": i},
            reason_codes=None,
            prev_hash=None,
            record_hash=f"h{i}",
            created_at=datetime.now(UTC),
        )
    records = await backend.fetch_range("allocator-bank-01", from_seq=2)
    assert [r.seq for r in records] == [2, 3, 4]


async def test_stream_ids_lists_every_distinct_stream(backend: PgTraceBackend) -> None:
    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id="a",
        seq=0,
        decision_type="ADMISSION",
        event_class="ADMISSION",
        payload={},
        reason_codes=None,
        prev_hash=None,
        record_hash="ha",
        created_at=datetime.now(UTC),
    )
    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id="b",
        seq=0,
        decision_type="ADMISSION",
        event_class="ADMISSION",
        payload={},
        reason_codes=None,
        prev_hash=None,
        record_hash="hb",
        created_at=datetime.now(UTC),
    )
    assert set(await backend.stream_ids()) == {"a", "b"}


# --------------------------------------------------------------------------------------------------
# checkpoint / prune_before -- checkpoint-coverage safety (K11, 02a S8.3)
# --------------------------------------------------------------------------------------------------


async def test_prune_before_deletes_nothing_without_a_checkpoint(backend: PgTraceBackend) -> None:
    for i in range(10):
        await backend.insert_trace_row(
            trace_id=uuid4(),
            stream_id="s",
            seq=i,
            decision_type="ADMISSION",
            event_class="ADMISSION",
            payload={},
            reason_codes=None,
            prev_hash=None,
            record_hash=f"h{i}",
            created_at=datetime.now(UTC),
        )
    deleted = await backend.prune_before("s", keep_from_seq=5)
    assert deleted == 0
    assert len(await backend.fetch_range("s", from_seq=0)) == 10


async def test_prune_before_deletes_only_up_to_checkpoint_coverage(backend: PgTraceBackend) -> None:
    for i in range(10):
        await backend.insert_trace_row(
            trace_id=uuid4(),
            stream_id="s",
            seq=i,
            decision_type="ADMISSION",
            event_class="ADMISSION",
            payload={},
            reason_codes=None,
            prev_hash=None,
            record_hash=f"h{i}",
            created_at=datetime.now(UTC),
        )
    await backend.insert_checkpoint(
        checkpoint_id=uuid4(),
        checkpoint_at=datetime.now(UTC),
        stream_heads={"s": {"seq": 4, "hash": "h4"}},
        checkpoint_hash_hex="ckpt",
    )
    # Ask to keep from seq 8, but only seq<=4 is checkpoint-covered -> only seq 0..4 may be deleted
    # (the checkpointed row itself may go too: its hash is retained as the checkpoint's anchor).
    deleted = await backend.prune_before("s", keep_from_seq=8)
    assert deleted == 5
    remaining_seqs = [r.seq for r in await backend.fetch_range("s", from_seq=0)]
    assert remaining_seqs == [5, 6, 7, 8, 9]


async def test_retention_days_for_defaults_when_unconfigured(backend: PgTraceBackend) -> None:
    assert await backend.retention_days_for("UNKNOWN_CLASS") == DEFAULT_RETENTION_DAYS


async def test_retention_days_for_reads_configured_value(
    backend: PgTraceBackend, fake_pool: _FakePool
) -> None:
    fake_pool.db.retention_policy["SAFE_STOP"] = (3650, True)
    assert await backend.retention_days_for("SAFE_STOP") == 3650


# --------------------------------------------------------------------------------------------------
# run_retention_prune_job (BUILD.md S4 "health" row: the daily/on-demand retention job)
# --------------------------------------------------------------------------------------------------


async def test_retention_prune_job_only_deletes_old_and_checkpoint_covered_rows(
    fake_pool: _FakePool, backend: PgTraceBackend
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    fake_pool.db.retention_policy["RT_ALLOCATION"] = (90, True)

    # seq 0: old + covered -> prunable. seq 1: old but NOT covered -> kept. seq 2: covered but fresh -> kept.
    for seq, created_at in ((0, old), (1, old), (2, now)):
        await backend.insert_trace_row(
            trace_id=uuid4(),
            stream_id="s",
            seq=seq,
            decision_type="RT_ALLOCATION",
            event_class="RT_ALLOCATION",
            payload={},
            reason_codes=None,
            prev_hash=None,
            record_hash=f"h{seq}",
            created_at=created_at,
        )
    await backend.insert_checkpoint(
        checkpoint_id=uuid4(),
        checkpoint_at=now,
        stream_heads={"s": {"seq": 0, "hash": "h0"}},
        checkpoint_hash_hex="ckpt",
    )

    deleted = await pg_backend.run_retention_prune_job(fake_pool, now=now)  # type: ignore[arg-type]
    assert deleted == {"RT_ALLOCATION": 1}
    remaining_seqs = {r.seq for r in await backend.fetch_range("s", from_seq=0)}
    assert remaining_seqs == {1, 2}


async def test_retention_prune_job_skips_classes_with_prune_after_checkpoint_false(
    fake_pool: _FakePool, backend: PgTraceBackend
) -> None:
    now = datetime.now(UTC)
    fake_pool.db.retention_policy["SAFE_STOP"] = (1, False)
    await backend.insert_trace_row(
        trace_id=uuid4(),
        stream_id="s",
        seq=0,
        decision_type="SAFE_STOP",
        event_class="SAFE_STOP",
        payload={},
        reason_codes=None,
        prev_hash=None,
        record_hash="h0",
        created_at=now - timedelta(days=10),
    )
    await backend.insert_checkpoint(
        checkpoint_id=uuid4(),
        checkpoint_at=now,
        stream_heads={"s": {"seq": 0, "hash": "h0"}},
        checkpoint_hash_hex="ckpt",
    )
    deleted = await pg_backend.run_retention_prune_job(fake_pool, now=now)  # type: ignore[arg-type]
    assert deleted == {}
    assert len(await backend.fetch_range("s", from_seq=0)) == 1


# --------------------------------------------------------------------------------------------------
# End-to-end through TraceStore, and concurrency
# --------------------------------------------------------------------------------------------------


async def test_trace_store_end_to_end_over_pg_backend(fake_pool: _FakePool) -> None:
    store = TraceStore(PgTraceBackend(fake_pool))  # type: ignore[arg-type]
    for i in range(5):
        await store.append("selector", "ADMISSION", "ADMISSION", {"i": i})
    result = await store.verify("selector")
    assert result.ok


async def test_concurrent_appends_to_same_stream_never_corrupt_the_chain(fake_pool: _FakePool) -> None:
    """TS-09-*: concurrent writers to the same stream_id -- one must win, the loser gets a clear
    conflict (no silent fork), and the surviving chain still verifies (K11)."""
    store = TraceStore(PgTraceBackend(fake_pool))  # type: ignore[arg-type]
    await store.append("selector", "ADMISSION", "ADMISSION", {"seed": True})

    results = await asyncio.gather(
        *(store.append("selector", "ADMISSION", "ADMISSION", {"n": n}) for n in range(8)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert all(isinstance(f, TraceAppendConflictError) for f in failures)
    assert len(successes) + len(failures) == 8
    assert len(failures) > 0  # the forced interleaving above must produce at least one real race

    verify_result = await store.verify("selector")
    assert verify_result.ok


# --------------------------------------------------------------------------------------------------
# Property test: chain verifies after random checkpoint + prune (K11, mirrors TS-09-01)
# --------------------------------------------------------------------------------------------------


@given(
    n_records=st.integers(min_value=5, max_value=200),
    checkpoint_at=st.integers(min_value=0, max_value=199),
)
@settings(
    max_examples=40,
    deadline=None,  # each example drives a fake async DB in-process; wall-clock time is not the point
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_chain_verifies_after_random_checkpoint_and_prune(n_records: int, checkpoint_at: int) -> None:
    async def _run() -> None:
        pool = _FakePool()
        store = TraceStore(PgTraceBackend(pool))  # type: ignore[arg-type]
        for i in range(n_records):
            await store.append("allocator-bank-07", "RT_ALLOCATION", "RT_ALLOCATION", {"i": i})
            if i == min(checkpoint_at, n_records - 1):
                await store.checkpoint()
        await store.checkpoint()
        await store.prune()

        remaining = await store._backend.fetch_range("allocator-bank-07", from_seq=0)
        if not remaining:
            return
        from_seq = remaining[0].seq
        result = await store.verify("allocator-bank-07", from_seq=from_seq)
        assert result.ok, result.reason

    asyncio.run(_run())
