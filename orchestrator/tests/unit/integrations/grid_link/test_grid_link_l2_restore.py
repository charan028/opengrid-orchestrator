"""L2 levels across og-engine restarts (grid-link.md S5.5): the traced GRID_LINK_COMMAND payloads of one
run are replayed into a fresh service, which ends with the same levels and re-delivers the instructions."""

from __future__ import annotations

from typing import Any

from opengrid.integrations.grid_link.model import Heartbeat, L2Block, L2Limit, L2LimitValue, TollCall
from opengrid.integrations.grid_link.pg_history import L2_COMMANDS, load_l2_commands

from .fakes import make_harness, make_settings

PEER = "127.0.0.1"


async def test_ts_gl_l2_levels_survive_a_restart_by_replaying_the_trace() -> None:
    before = make_harness()
    before.service.offer(Heartbeat(), PEER)
    for command in (
        L2LimitValue("LZ_AEN", 400.0),
        L2Limit("LZ_AEN", True),
        L2Block("B041", True),
        L2Block("B041", False),
        L2LimitValue("B041", 90.0),
        L2Limit("B041", True),
        TollCall(5, 100.0, 30),
    ):
        before.service.offer(command, PEER)
    await before.service.drain()
    traced = [p for p in before.trace.events("GRID_LINK_COMMAND") if p["command"] in L2_COMMANDS]
    assert len(traced) == 6

    after = make_harness()  # og-engine restarted: empty memory
    assert await after.service.restore_l2(traced) == 6
    assert after.service.l2.target_status() == before.service.l2.target_status()
    assert {b: after.service.l2.ceiling_kw(b) for b in ("bank-040", "bank-041")} == {
        "bank-040": 400.0,
        "bank-041": 90.0,
    }
    delivered = {(i.bank_id, i.kind, i.limit_kw) for i in after.sink.instructions}
    assert delivered == {("bank-040", "LIMIT", 400.0), ("bank-041", "LIMIT", 90.0)}
    assert after.trace.events("GRID_LINK_COMMAND") == []  # replay is not traced again
    assert after.trace.events("GRID_LINK_STATE")[-1] == {
        "origin": "GRID_LINK",
        "utility_id": "AUSTIN_ENERGY",
        "state": "L2_RESTORED",
        "commands": 6,
    }


async def test_restore_skips_unknown_targets_and_malformed_rows() -> None:
    h = make_harness(make_settings(l2_targets=[]))
    rows = [
        {"command": "L2Block", "target": "B041", "active": True},  # target no longer configured
        {"command": "L2Limit", "active": True},  # malformed
    ]
    assert await h.service.restore_l2(rows) == 0
    assert h.sink.instructions == [] and h.trace.rows == []


class _Cursor:
    def __init__(self, rows: list[tuple[Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any]]:
        return self._rows


class _Conn:
    def __init__(self, rows: list[tuple[Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        self.calls.append((sql, params))
        return _Cursor(self.rows)

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def connection(self) -> _Conn:
        return self._conn


async def test_history_query_is_parameterised_and_ordered() -> None:
    conn = _Conn([({"command": "L2Block", "target": "B041", "active": True},), ("not-a-dict",)])
    rows = await load_l2_commands(_Pool(conn), "grid_link:AUSTIN_ENERGY")  # type: ignore[arg-type]
    assert rows == [{"command": "L2Block", "target": "B041", "active": True}]
    sql, params = conn.calls[0]
    assert "ORDER BY seq" in sql and "%s" in sql and "AUSTIN_ENERGY" not in sql
    assert params == ("grid_link:AUSTIN_ENERGY", list(L2_COMMANDS))
