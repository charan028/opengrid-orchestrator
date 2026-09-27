"""Measured delivery of a customer's own calls (D-38), read from `og.delivery_record`
(`opengrid.delivery.store`; og-settle's delivery job writes it). Two scopes, never wider:

- a utility (`/og/api/customer/v1/utility/delivery-records`, role `utility`, `utility.read`): only the
  records whose call is on one of ITS obligations (`utility_id`);
- a customer (`/og/api/customer/delivery-records`, role `customer`): only records on its own contracts.

A record of another utility or customer answers 404 exactly like a missing one. kW is signed
(+charge/-discharge); energy is discharged kWh.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool

from opengrid.api.deps import get_pool
from opengrid.customer_api.identity import CustomerIdentity, require_customer
from opengrid.customer_api.utility_identity import ACTION_READ, UtilityIdentity, require_utility_action
from opengrid.delivery import store
from opengrid.delivery.models import DeliveryRecord

router = APIRouter(tags=["customer-delivery"])

Reader = Annotated[UtilityIdentity, Depends(require_utility_action(ACTION_READ))]
Customer = Annotated[CustomerIdentity, Depends(require_customer)]
Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]

MAX_ROWS = 500
_NOT_FOUND = "not found"


def _own(
    record: DeliveryRecord | None, *, utility_id: str | None = None, customer_id: Any = None
) -> DeliveryRecord:
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if utility_id is not None and record.utility_id != utility_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if customer_id is not None and record.customer_id != customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return record


@router.get("/og/api/customer/v1/utility/delivery-records")
async def utility_delivery_records(
    who: Reader,
    pool: Pool,
    since: datetime | None = None,
    until: datetime | None = None,
    result: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 100,
) -> dict[str, Any]:
    """The utility's own calls' delivery records, newest first (no per-bucket series)."""
    records = await store.list_records(
        pool, utility_id=who.utility_id, since=since, until=until, result=result, limit=limit
    )
    return {"utility_id": who.utility_id, "records": [r.public() for r in records]}


@router.get("/og/api/customer/v1/utility/delivery-records/{call_id}")
async def utility_delivery_record(call_id: str, who: Reader, pool: Pool) -> dict[str, Any]:
    """One of the utility's own calls with its per-bucket series; 404 for any other."""
    record = _own(await store.fetch_record(pool, call_id), utility_id=who.utility_id)
    return record.public(with_series=True)


@router.get("/og/api/customer/delivery-records")
async def customer_delivery_records(
    who: Customer,
    pool: Pool,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 100,
) -> dict[str, Any]:
    """The customer's own calls' delivery records (its contracts), newest first."""
    records = await store.list_records(
        pool, customer_id=who.customer_id, since=since, until=until, limit=limit
    )
    return {"records": [r.public() for r in records]}


@router.get("/og/api/customer/delivery-records/{call_id}")
async def customer_delivery_record(call_id: str, who: Customer, pool: Pool) -> dict[str, Any]:
    record = _own(await store.fetch_record(pool, call_id), customer_id=who.customer_id)
    return record.public(with_series=True)
