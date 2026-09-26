"""Aggregate-first dispatch ledger (Gitea #19): reserved, committed and uncommitted capacity over time
for the fleet, a zone, a bank or a hub, from `og.reservation` and `og.commitment`, with a "now" row per
child scope for drill-down (fleet -> zones -> banks -> hubs).

"Uncommitted capacity" is the label for what the old per-bank timeline called "free headroom":
available rated kW minus max(reserved, committed). Read-only; viewer or operator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.views_ext import (
    ExtViewsProtocol,
    FleetMapService,
    as_utc,
    build_ledger,
    get_ext_views,
    get_fleet_map_service,
    hub_capacities,
)

router = APIRouter(prefix="/og/api/dispatch", tags=["dispatch"])

_DEFAULT_LOOKBACK: Final = timedelta(hours=2)
_DEFAULT_LOOKAHEAD: Final = timedelta(hours=24)
_MAX_BUCKETS: Final = 1000


@router.get("/ledger")
async def dispatch_ledger(
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    fleet_map: Annotated[FleetMapService, Depends(get_fleet_map_service)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    level: Literal["fleet", "zone", "bank", "hub"] = "fleet",
    scope_id: Annotated[str | None, Query(alias="id")] = None,
    t0: Annotated[datetime | None, Query(alias="from")] = None,
    t1: Annotated[datetime | None, Query(alias="to")] = None,
    bucket_minutes: Annotated[int, Query(ge=5, le=1440)] = 15,
) -> dict[str, Any]:
    """`{level, id, from, to, bucket_minutes, hub_count, available_hub_count, labels, basis, notes, now,
    timeline[], children[]}`. Every point: `t, capacity_kw, reserved_kw, committed_kw,
    uncommitted_capacity_kw, over_committed_kw, committed_by_service{}` (+ `unallocated_committed_kw` at
    fleet level). Default window: 2 h back to 24 h ahead."""
    if level != "fleet" and not scope_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"id is required for level={level}")
    now = datetime.now(UTC)
    start = as_utc(t0) if t0 is not None else now - _DEFAULT_LOOKBACK
    end = as_utc(t1) if t1 is not None else now + _DEFAULT_LOOKAHEAD
    bucket = timedelta(minutes=bucket_minutes)
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be after from")
    if (end - start) / bucket > _MAX_BUCKETS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"window too large: at most {_MAX_BUCKETS} buckets"
        )
    snapshot = await fleet_map.snapshot()
    hubs = hub_capacities(snapshot.hubs)
    if level != "fleet":
        attr = {"zone": "zone", "bank": "bank_id", "hub": "hub_id"}[level]
        if not any(getattr(h, attr) == scope_id for h in hubs):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown {level} {scope_id!r}")
    reservations, commitments = await views.ledger_slices(t0=start, t1=end)
    return build_ledger(
        level=level,
        scope_id=scope_id if level != "fleet" else None,
        hubs=hubs,
        reservations=reservations,
        commitments=commitments,
        t0=start,
        t1=end,
        bucket=bucket,
        now=now,
    )
