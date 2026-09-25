"""Retention policy edit (operator only) -- BUILD.md api row. Backed by `og.retention_policy`
(02a S8.1); `trace.retention`'s pruning cadence reads this table directly, so an edit here takes
effect on the next prune cycle with no code change (02b S1.4: "unknown classes default to 400 days").
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.schemas import RetentionPolicyUpdate
from opengrid.api.store import StoreProtocol
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/retention", tags=["retention"])


@router.get("")
async def get_retention_policy(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> list[dict[str, Any]]:
    return await store.retention_policy()


@router.put("")
async def update_retention_policy(
    body: RetentionPolicyUpdate,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    await store.upsert_retention_policy(body.event_class, body.retention_days)
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="CONFIG_CHANGE",
        payload={
            "decision_ref": body.event_class,
            "action": "retention policy updated",
            "retention_days": body.retention_days,
        },
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="CONFIG_CHANGE",
        target_ref=body.event_class,
        tier=None,
        reason=f"retention_days -> {body.retention_days}",
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    return {"event_class": body.event_class, "retention_days": body.retention_days}
