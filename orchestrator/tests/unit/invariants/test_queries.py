"""Unit tests for `opengrid.invariants.queries`' persistence and summary reads, against a minimal
fake pool/cursor (no real Postgres, BUILD.md S5) -- mirrors `tests/unit/health/test_queries.py`'s
pattern."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.invariants import queries
from opengrid.invariants.models import CheckState, Violation

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, fetchall_result: list | None = None, fetchone_result: tuple | None = None) -> None:
        self.executed: list[tuple[str, dict | list | None]] = []
        self._fetchall_result = fetchall_result or []
        self._fetchone_result = fetchone_result

    async def execute(self, sql, params=None):
        self.executed.append((str(sql), params))

    async def executemany(self, sql, params_seq):
        self.executed.append((str(sql), list(params_seq)))

    async def fetchall(self):
        return self._fetchall_result

    async def fetchone(self):
        return self._fetchone_result

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


async def test_get_check_state_returns_empty_when_never_run() -> None:
    cursor = FakeCursor(fetchone_result=None)
    pool = FakePool(cursor)

    state = await queries.get_check_state(pool, "K1_RESERVE_BREACH")

    assert state == CheckState.empty("K1_RESERVE_BREACH")


async def test_get_check_state_parses_existing_row() -> None:
    cursor = FakeCursor(
        fetchone_result=("K1_RESERVE_BREACH", NOW, 120, 2, 7, {"since": "2026-09-26T00:00:00+00:00"})
    )
    pool = FakePool(cursor)

    state = await queries.get_check_state(pool, "K1_RESERVE_BREACH")

    assert state.last_run_at == NOW
    assert state.last_run_ms == 120
    assert state.last_violations == 2
    assert state.total_violations == 7
    assert state.watermark == {"since": "2026-09-26T00:00:00+00:00"}


async def test_upsert_check_state_commits() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.upsert_check_state(
        pool,
        "K1_RESERVE_BREACH",
        ran_at=NOW,
        run_ms=50,
        violation_count=1,
        total_violations=3,
        watermark={"since": NOW.isoformat()},
    )

    assert pool._conn.committed is True
    assert len(cursor.executed) == 1
    _sql, params = cursor.executed[0]
    assert params["check_name"] == "K1_RESERVE_BREACH"
    assert params["last_violations"] == 1
    assert params["total_violations"] == 3


async def test_insert_violations_noop_on_empty_list() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)

    await queries.insert_violations(pool, "K1_RESERVE_BREACH", [])

    assert cursor.executed == []
    assert pool._conn.committed is False


async def test_insert_violations_writes_one_row_per_violation() -> None:
    cursor = FakeCursor()
    pool = FakePool(cursor)
    violations = [
        Violation(scope={"hub_id": "hub-1"}, detail={"soc_kwh": 1.0}),
        Violation(scope={"hub_id": "hub-2"}, detail={"soc_kwh": 1.5}),
    ]

    await queries.insert_violations(pool, "K1_RESERVE_BREACH", violations)

    assert pool._conn.committed is True
    _sql, params_list = cursor.executed[0]
    assert len(params_list) == 2
    assert params_list[0]["check_name"] == "K1_RESERVE_BREACH"


async def test_read_summary_maps_totals_and_last_counts() -> None:
    rows = [
        ("K1_RESERVE_BREACH", NOW, 3, 1),
        ("K2_DOUBLE_SOLD", NOW, 12.5, 1),
        ("K13_LOCK_VIOLATION", NOW, 0, 0),
        ("ORPHAN_RESERVATION", NOW, 9, 2),  # total=9 (ever seen), last_violations=2 (current)
        ("ORPHAN_COMMITMENT", NOW, 4, 0),
    ]
    cursor = FakeCursor(fetchall_result=rows)
    pool = FakePool(cursor)

    summary = await queries.read_summary(pool)

    assert summary.reserve_breaches == 3
    assert summary.double_sold_kwh == 12.5
    assert summary.lock_violations == 0
    assert summary.orphan_reservations == 2  # last run's count, not the cumulative total
    assert summary.orphan_commitments == 0
    assert summary.as_of == NOW


async def test_read_summary_all_zero_and_as_of_none_when_never_run() -> None:
    cursor = FakeCursor(fetchall_result=[])
    pool = FakePool(cursor)

    summary = await queries.read_summary(pool)

    assert summary.reserve_breaches == 0
    assert summary.double_sold_kwh == 0.0
    assert summary.as_of is None
