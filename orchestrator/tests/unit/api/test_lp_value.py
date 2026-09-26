"""`GET /og/api/profitability/lp-value` (ES05-S07 / KPI-22): LP value added over the rule baseline for the
latest 96 plans; viewer role; `available: false` on a database without migration 0030's tables. The
response for the fixed rows below is the UI fixture `fixtures/ui19/lp_value.json`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.deps import get_config
from opengrid.api.routers import lp_value
from opengrid.api.routers.lp_value import LP_VALUE_PLAN_LIMIT, PgLpValueReader, get_lp_value_reader
from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS

FIXTURE = Path(__file__).parent / "fixtures" / "ui19" / "lp_value.json"
T0 = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


def _rows() -> list[dict[str, Any]]:
    """Three plans, newest first, as `PgLpValueReader` returns them (numeric -> Decimal, jsonb -> dict)."""
    return [
        {
            "plan_id": UUID(f"00000000-0000-7000-8000-0000000000{i:02d}"),
            "created_at": T0 - timedelta(minutes=15 * i),
            "lp_net_value": Decimal("1250.5000") - i * Decimal("100"),
            "rule_net_value": Decimal("1100.2500") - i * Decimal("100"),
            "value_added": Decimal("150.2500"),
            "forgone_upside": Decimal(f"{12 * i}.0000"),
            "breakdown": {"energy": 120.25, "as_capacity": 30.0, "degradation": -0.0, "plan_index": i},
        }
        for i in range(3)
    ]


class _FakeReader:
    def __init__(self, rows: list[dict[str, Any]] | None) -> None:
        self.rows = rows
        self.limits: list[int] = []

    async def latest_plan_values(self, *, limit: int) -> list[dict[str, Any]] | None:
        self.limits.append(limit)
        return self.rows


def _client(reader: _FakeReader) -> TestClient:
    # A bare app with only this router: the endpoint's contract does not depend on the rest of
    # `create_app()`'s mounts (and this test stays green while app.py's mount line is pending).
    app = FastAPI()
    app.include_router(lp_value.router)
    app.dependency_overrides[get_config] = lambda: Config({"api": {"roles": {"operator": [], "viewer": []}}})
    app.dependency_overrides[get_lp_value_reader] = lambda: reader
    return TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)


def test_lp_value_returns_latest_plans_and_matches_ui_fixture() -> None:
    reader = _FakeReader(_rows())
    resp = _client(reader).get("/og/api/profitability/lp-value", headers=VIEWER_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert reader.limits == [LP_VALUE_PLAN_LIMIT] and LP_VALUE_PLAN_LIMIT == 96
    assert body["available"] is True
    assert [p["plan_id"] for p in body["plans"]] == [str(r["plan_id"]) for r in _rows()]
    assert body["plans"][0]["value_added"] == "150.2500"  # decimals as strings
    assert body["plans"][0]["breakdown"]["energy"] == 120.25
    assert body == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_lp_value_unavailable_without_migration_0030_tables() -> None:
    resp = _client(_FakeReader(None)).get("/og/api/profitability/lp-value", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "plans": []}


def test_lp_value_empty_tables_are_available_but_empty() -> None:
    resp = _client(_FakeReader([])).get("/og/api/profitability/lp-value", headers=VIEWER_HEADERS)
    assert resp.json() == {"available": True, "plans": []}


def test_lp_value_rejects_unknown_identity() -> None:
    resp = _client(_FakeReader([])).get(
        "/og/api/profitability/lp-value", headers={"X-Remote-User": "stranger"}
    )
    assert resp.status_code == 403


# --- PgLpValueReader against a fake pool ----------------------------------------------------------------


class _Cursor:
    def __init__(self, present: bool, rows: list[dict[str, Any]]) -> None:
        self._present = present
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> dict[str, Any]:
        return {"?column?": self._present}

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def connection(self) -> _Conn:
        return _Conn(self._cursor)


@pytest.mark.asyncio
async def test_pg_reader_returns_none_when_tables_missing() -> None:
    cursor = _Cursor(present=False, rows=_rows())
    assert await PgLpValueReader(_Pool(cursor)).latest_plan_values(limit=96) is None  # type: ignore[arg-type]
    assert len(cursor.executed) == 1  # never queries a table that does not exist


@pytest.mark.asyncio
async def test_pg_reader_runs_the_bounded_query() -> None:
    cursor = _Cursor(present=True, rows=_rows())
    rows = await PgLpValueReader(_Pool(cursor)).latest_plan_values(limit=96)  # type: ignore[arg-type]
    assert rows == _rows()
    sql, params = cursor.executed[1]
    assert "JOIN og.plan p USING (plan_id)" in sql
    assert "ORDER BY p.created_at DESC" in sql
    assert params == {"limit": 96}
