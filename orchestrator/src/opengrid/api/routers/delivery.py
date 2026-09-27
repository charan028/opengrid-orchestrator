"""Operator delivery-verification API (D-38): measured delivery of every discharge call, read from
`og.delivery_record` (`opengrid.delivery.store`; og-settle's delivery job writes it). Viewer role."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_pool
from opengrid.delivery import store

router = APIRouter(prefix="/og/api/delivery", tags=["delivery"])

MAX_LIMIT = 1000
MAX_SUMMARY_DAYS = 92
MAX_CALL_IDS = 200


@router.get("/records")
async def list_delivery_records(
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    service_type: str | None = None,
    contract_id: UUID | None = None,
    call_kind: str | None = None,
    result: str | None = None,
    utility_id: str | None = None,
    call_ids: Annotated[str | None, Query(description="comma-separated call ids")] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
) -> list[dict[str, Any]]:
    """Delivery records newest first (without their per-bucket series), filtered by service, contract,
    call kind, result, utility, call ids and window-start date range."""
    ids = [c.strip() for c in call_ids.split(",") if c.strip()][:MAX_CALL_IDS] if call_ids else None
    records = await store.list_records(
        pool,
        contract_id=contract_id,
        service_type=service_type,
        call_kind=call_kind,
        result=result,
        utility_id=utility_id,
        call_ids=ids,
        since=since,
        until=until,
        limit=limit,
    )
    return [r.public() for r in records]


@router.get("/summary")
async def delivery_summary(
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    days: Annotated[int, Query(ge=1, le=MAX_SUMMARY_DAYS)] = 7,
) -> list[dict[str, Any]]:
    """Per contract per CT day over final records: calls, PASS/PARTIAL/FAIL, compliance % (PASS share),
    mean sustained compliance, meter mismatches, discharged vs committed kWh."""
    until = datetime.now(UTC) + timedelta(days=1)
    return await store.summary(pool, since=until - timedelta(days=days + 1), until=until)


@router.get("/records/{call_id}")
async def get_delivery_record(
    call_id: str,
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """One call's delivery record with its per-bucket series (committed, commanded, delivered, meter)."""
    record = await store.fetch_record(pool, call_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no delivery record for that call")
    return record.public(with_series=True)
