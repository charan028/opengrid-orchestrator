"""Bid funnel (Gitea #19): how many opportunities were available, submitted, awarded and rejected, by
product and by hour or day, with the rejection reasons.

- available: every opportunity the intake offered (`og.opportunity`), plus offers rejected at
  admission before they became one (`R-ADMIT-*`, traced by `opengrid.contracts.admission`);
- submitted: selected at a gate (`SELECTED`, or its obligation moved on from there), plus the ERCOT MMS
  submissions table when the INTEGRATIONS agent's table exists (reported separately under `mms`);
- awarded: its obligation reached COMMITTED (or a later state);
- rejected: `REJECTED` / `R-ADMIT-*`, grouped by reason code; expired is counted apart.

Read-only; viewer or operator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.views_ext import ExtViewsProtocol, as_utc, build_funnel, get_ext_views

router = APIRouter(prefix="/og/api/markets", tags=["markets"])

_DEFAULT_WINDOW: Final = timedelta(days=7)
_MAX_WINDOW: Final = timedelta(days=93)
_HOURLY_MAX_WINDOW: Final = timedelta(hours=48)


@router.get("/bid-funnel")
async def bid_funnel(
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    t0: Annotated[datetime | None, Query(alias="from")] = None,
    t1: Annotated[datetime | None, Query(alias="to")] = None,
    bucket: Literal["hour", "day"] | None = None,
) -> dict[str, Any]:
    """`{from, to, bucket, totals, by_product[], series[], rejection_reasons[], mms, sources}`. Stage
    counts everywhere are `available, submitted, awarded, rejected, expired`. Default window: the last
    7 days; default bucket: `hour` up to 48 h, else `day`."""
    end = as_utc(t1) if t1 is not None else datetime.now(UTC)
    start = as_utc(t0) if t0 is not None else end - _DEFAULT_WINDOW
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be after from")
    if end - start > _MAX_WINDOW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window is limited to 93 days")
    chosen = bucket or ("hour" if end - start <= _HOURLY_MAX_WINDOW else "day")
    rows = await views.funnel_rows(t0=start, t1=end, bucket=chosen)
    mms_rows = await views.mms_funnel_rows(t0=start, t1=end, bucket=chosen)
    funnel = build_funnel(rows, mms_rows)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "bucket": chosen,
        **funnel,
        "sources": {
            "available": ["og.opportunity", "og.trace ADMISSION rejections"],
            "submitted": ["gate decisions"] + (["ercot_mms"] if mms_rows is not None else []),
            "awarded": ["og.obligation COMMITTED+"],
            "rejected": ["og.opportunity/og.obligation REJECTED", "R-ADMIT-*"],
        },
    }
