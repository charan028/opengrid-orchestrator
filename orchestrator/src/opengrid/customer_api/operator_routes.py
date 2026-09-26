"""Operator side of the customer API: customer disputes and cancel/renominate requests are visible to
operators and viewers, and an operator records the review outcome. A review only records the decision
(audited in `og.operator_action` and the trace); it never changes a commitment -- a committed
obligation's allocation moves only through the K13 paths owned by `selector`/`ledger`/`contracts`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.store import StoreProtocol
from opengrid.customer_api.models import DisputeReview, RequestReview
from opengrid.customer_api.store import CustomerStore, get_customer_store
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api", tags=["customer-operator"])

Store = Annotated[CustomerStore, Depends(get_customer_store)]
_NOT_OPEN = "not found or already closed"


@router.get("/customer-disputes")
async def list_disputes(
    store: Store, _identity: Annotated[Identity, Depends(require_viewer)], customer_id: UUID | None = None
) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in await store.list_disputes(customer_id=customer_id)]


@router.patch("/customer-disputes/{dispute_id}")
async def review_dispute(
    dispute_id: UUID,
    body: DisputeReview,
    store: Store,
    api_store: Annotated[StoreProtocol, Depends(get_store)],
    trace: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    updated = await store.review_dispute(
        dispute_id, status=body.status, reviewed_by=identity.user, note=body.note
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_OPEN)
    await _record_review(
        api_store, trace, identity, target_ref=str(dispute_id), reason=f"dispute -> {body.status}"
    )
    return updated.model_dump(mode="json")


@router.get("/customer-requests")
async def list_requests(
    store: Store, _identity: Annotated[Identity, Depends(require_viewer)], customer_id: UUID | None = None
) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in await store.list_requests(customer_id=customer_id)]


@router.patch("/customer-requests/{request_id}")
async def review_request(
    request_id: UUID,
    body: RequestReview,
    store: Store,
    api_store: Annotated[StoreProtocol, Depends(get_store)],
    trace: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    updated = await store.review_request(
        request_id, status=body.status, reviewed_by=identity.user, note=body.note
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_OPEN)
    await _record_review(
        api_store, trace, identity, target_ref=str(request_id), reason=f"request -> {body.status}"
    )
    return updated.model_dump(mode="json")


async def _record_review(
    api_store: StoreProtocol, trace: TraceStore, identity: Identity, *, target_ref: str, reason: str
) -> None:
    """The operator audit trail every api write keeps (BUILD.md api row: operator_action + trace)."""
    ref = await trace.append(
        f"operator_action:{identity.user}",
        "OPERATOR_ACTION",
        "OPERATOR_ACTION",
        {"decision_ref": target_ref, "action": reason},
    )
    await api_store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="APPROVAL",
        target_ref=target_ref,
        tier=None,
        reason=reason,
        trace_id=ref.trace_id,
        confirmed_at=None,
    )
