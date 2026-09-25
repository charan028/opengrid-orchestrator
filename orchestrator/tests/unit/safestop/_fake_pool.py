"""Fake psycopg-pool-shaped connection/cursor for unit-testing `pg_backend.py`/`trace_backend.py`
without a real Postgres connection. Records every executed statement's params for assertions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class _FakeCursor:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool
        self._last_params: dict[str, Any] = {}
        self.rowcount = 0

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._last_params = params or {}
        self._pool.executed.append((sql.strip().split()[0], self._last_params))
        if sql.strip().upper().startswith("DELETE"):
            self.rowcount = self._pool.delete_rowcount

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._pool.fetchone_result

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._pool.fetchall_result


class _FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._pool)


@dataclass
class FakePool:
    """Minimal stand-in for `psycopg_pool.AsyncConnectionPool` -- only `.connection()` is used by
    `pg_backend.py`/`trace_backend.py`."""

    fetchone_result: tuple[Any, ...] | None = None
    fetchall_result: list[tuple[Any, ...]] = field(default_factory=list)
    delete_rowcount: int = 0
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def connection(self) -> _FakeConnection:
        return _FakeConnection(self)
