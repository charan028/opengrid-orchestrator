"""PLAT-006: `opengrid.platform.heartbeat.write_heartbeat` -- normal write, a raising pool, and a
timed-out write are all swallowed (never raised to the caller's control loop, K7)."""

from __future__ import annotations

import asyncio

from opengrid.platform.heartbeat import write_heartbeat


class _FakeCursor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append((sql, params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._conn = _FakeConn(cursor)

    def connection(self):
        return self._conn


class _HangingPool:
    def connection(self):
        class _Ctx:
            async def __aenter__(self):
                await asyncio.sleep(10)
                return self

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def test_write_heartbeat_upserts_expected_row():
    cursor = _FakeCursor()
    await write_heartbeat(_FakePool(cursor), "guardian")
    assert len(cursor.executed) == 1
    params = cursor.executed[0][1]
    assert params["process"] == "guardian"
    assert params["status"] == "ok"


async def test_write_heartbeat_swallows_db_error():
    cursor = _FakeCursor(fail=True)
    await write_heartbeat(_FakePool(cursor), "guardian")  # must not raise


async def test_write_heartbeat_swallows_timeout(monkeypatch):
    from opengrid.platform import heartbeat as heartbeat_module

    monkeypatch.setattr(heartbeat_module, "HEARTBEAT_WRITE_TIMEOUT_S", 0.01)
    await write_heartbeat(_HangingPool(), "guardian")  # must not raise or hang
