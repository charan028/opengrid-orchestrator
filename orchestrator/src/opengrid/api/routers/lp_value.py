"""`GET /og/api/profitability/lp-value` (ES05-S07 / KPI-22): the value the LP plan added over the rule
baseline, per plan, for the latest `LP_VALUE_PLAN_LIMIT` plans (a day of 15-minute plans).

Read-only over migration 0030's `og.plan_value` (written by the selector's F2 shadow run) joined to
`og.plan` for the plan's creation time. Viewer role. A database that predates 0030 answers
`{"available": false, "plans": []}` rather than a 500, so the UI can say "not measured yet".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from opengrid.api.auth import Identity, require_viewer

router = APIRouter(prefix="/og/api/profitability", tags=["profitability"])

#: 96 x 15-minute plans = one day of planning cycles.
LP_VALUE_PLAN_LIMIT = 96

_TABLES_PRESENT_SQL = "SELECT to_regclass('og.plan_value') IS NOT NULL AND to_regclass('og.plan') IS NOT NULL"

_LATEST_PLAN_VALUES_SQL = """
    SELECT p.plan_id, p.created_at, v.lp_net_value, v.rule_net_value, v.value_added, v.forgone_upside,
           v.breakdown
    FROM og.plan_value v
    JOIN og.plan p USING (plan_id)
    ORDER BY p.created_at DESC
    LIMIT %(limit)s
"""


class LpValuePlan(BaseModel):
    """One plan's LP-vs-rule value (USD, decimals serialized as strings like the other profitability
    endpoints). `breakdown` is the selector's own per-component jsonb, passed through unchanged."""

    plan_id: UUID
    created_at: datetime
    lp_net_value: Decimal
    rule_net_value: Decimal
    value_added: Decimal
    forgone_upside: Decimal
    breakdown: dict[str, Any]


class LpValueResponse(BaseModel):
    """`available` is false only when the 0030 tables do not exist yet; newest plan first."""

    available: bool
    plans: list[LpValuePlan]


class LpValueReader(Protocol):
    async def latest_plan_values(self, *, limit: int) -> list[dict[str, Any]] | None:
        """The latest `limit` plan-value rows, newest first; `None` when the tables are missing."""
        ...


class PgLpValueReader:
    """`LpValueReader` over the api pool: a schema-presence probe, then one bounded read."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def latest_plan_values(self, *, limit: int) -> list[dict[str, Any]] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_TABLES_PRESENT_SQL)
            present = await cur.fetchone()
            if present is None or not next(iter(present.values())):
                return None
            await cur.execute(_LATEST_PLAN_VALUES_SQL, {"limit": limit})
            return list(await cur.fetchall())


def get_lp_value_reader(request: Request) -> LpValueReader:
    """One `PgLpValueReader` per app, built lazily off `app.state.pool` (tests override this)."""
    reader: LpValueReader | None = getattr(request.app.state, "lp_value_reader", None)
    if reader is None:
        reader = PgLpValueReader(request.app.state.pool)
        request.app.state.lp_value_reader = reader
    return reader


@router.get("/lp-value", response_model=LpValueResponse)
async def lp_value_route(
    reader: Annotated[LpValueReader, Depends(get_lp_value_reader)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> LpValueResponse:
    rows = await reader.latest_plan_values(limit=LP_VALUE_PLAN_LIMIT)
    if rows is None:
        return LpValueResponse(available=False, plans=[])
    return LpValueResponse(available=True, plans=[LpValuePlan.model_validate(row) for row in rows])
