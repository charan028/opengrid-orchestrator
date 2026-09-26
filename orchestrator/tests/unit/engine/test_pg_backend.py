"""`PgEngineBackend` pieces that need no live Postgres (fake pool/cursor, BUILD.md S5)."""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.engine.pg_backend import PgEngineBackend


class _Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, rows: list[tuple]) -> None:
        self.cursor = _Cursor(rows)

    def connection(self):
        return _Conn(self.cursor)


async def test_k06_next_epoch_is_above_every_epoch_the_guardian_accepted() -> None:
    """Regression (live 2026-09-26): a fixed epoch restarted `seq` at 1 below the guardian's durable
    (epoch, seq) high-water mark, so every batch after an engine restart was VETOED on G-13."""
    pool = _Pool([(8,)])

    assert await PgEngineBackend(pool).next_epoch() == 8  # type: ignore[arg-type]
    assert "MAX(epoch)" in pool.cursor.executed[0][0]
    assert "og.lease_state" in pool.cursor.executed[0][0]


async def test_due_for_close_reports_failed_performance() -> None:
    obligation_id = "3b60e803-dabf-4df9-a284-1386c0426952"
    pool = _Pool([(obligation_id, True)])

    (closing,) = await PgEngineBackend(pool).obligations_due_for_close(datetime(2026, 9, 26, tzinfo=UTC))  # type: ignore[arg-type]

    assert closing.any_interval_failed is True
