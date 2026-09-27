"""r3.4.3 PERF-OPT: `PgGrantBackend.insert_grants` writes one cycle's grants as ONE batched `executemany` (psycopg
pipelines it) inside the same asynchronous-commit transaction -- the same INSERT, the same rows in the same order,
instead of one awaited round trip per grant."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from opengrid.ledger import GrantRecord
from opengrid.ledger.pg_backend import _INSERT_GRANT_SQL, PgGrantBackend


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.in_transaction = False


class _Cursor:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self._rec.calls.append(("executemany", sql, list(rows), self._rec.in_transaction))

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Transaction:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def __aenter__(self) -> None:
        self._rec.in_transaction = True

    async def __aexit__(self, *exc: object) -> None:
        self._rec.in_transaction = False
        self._rec.calls.append(("commit", "", None))


class _Conn:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    def transaction(self) -> _Transaction:
        return _Transaction(self._rec)

    def cursor(self) -> _Cursor:
        return _Cursor(self._rec)

    async def execute(self, sql: str, params: Any = None) -> None:
        self._rec.calls.append(("execute", sql, params, self._rec.in_transaction))

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    def connection(self) -> _Conn:
        return _Conn(self._rec)


def _record(i: int) -> GrantRecord:
    return GrantRecord(
        grant_id=uuid4(),
        cycle_id="c-1",
        bank_id=f"bank-{i:03d}",
        granted_kw=Decimal(f"{i}.5"),
        ledger_version=7,
        obligation_id=uuid4() if i % 2 else None,
        is_headroom=not i % 2,
    )


async def test_one_batched_insert_in_one_async_commit_transaction() -> None:
    rec = _Recorder()
    records = [_record(i) for i in range(5)]
    await PgGrantBackend(_Pool(rec)).insert_grants(records)  # type: ignore[arg-type]

    (set_local, batch, commit) = rec.calls
    assert set_local[:2] == ("execute", "SET LOCAL synchronous_commit TO OFF") and set_local[3]
    assert batch[0] == "executemany" and batch[1] == _INSERT_GRANT_SQL and batch[3]
    assert batch[2] == [
        (
            r.grant_id,
            r.cycle_id,
            r.obligation_id,
            r.bank_id,
            r.granted_kw,
            r.is_headroom,
            r.ledger_version,
            r.command_batch_id,
        )
        for r in records
    ]
    assert commit[0] == "commit"


async def test_no_grants_touch_nothing() -> None:
    rec = _Recorder()
    await PgGrantBackend(_Pool(rec)).insert_grants([])  # type: ignore[arg-type]
    assert rec.calls == []
