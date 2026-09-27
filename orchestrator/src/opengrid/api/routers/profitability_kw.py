"""`GET /og/api/profitability/per-kw` (09-optimizer-dispatcher-update.md S11.8): the $/kW economics per
contract, per market and for the fleet.

Reuse only: the per-contract totals come from `opengrid.market.pg_backend.fetch_contract_totals` (through
`ExtViewsProtocol.contract_totals`), and the payload is `opengrid.market.view.profitability_per_kw`'s
`PerKwSummary`. Operator, read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.views_ext import ExtViewsProtocol, as_utc, get_ext_views
from opengrid.core.timeutil import MARKET_TZ
from opengrid.market.view import profitability_per_kw

router = APIRouter(prefix="/og/api/profitability", tags=["profitability"])


def current_settlement_month(now: datetime) -> tuple[datetime, datetime]:
    """[first day of this month, first day of next month), midnight America/Chicago, as UTC instants."""
    local = as_utc(now).astimezone(MARKET_TZ)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    # Re-attach the zone after the calendar arithmetic so a DST change inside the month is respected.
    return as_utc(start.replace(tzinfo=MARKET_TZ)), as_utc(nxt.replace(tzinfo=MARKET_TZ))


@router.get("/per-kw")
async def profitability_per_kw_route(
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """`PerKwSummary` (`method_version, period_hours, hardware_view_usd_per_kw, target_payback_years,
    contracts[], markets{REGULATED, FREE}, fleet, illustrative_home_unit`), decimals as strings. Default
    period: the current settlement month in America/Chicago. 400 when `end <= start`."""
    default_start, default_end = current_settlement_month(datetime.now(MARKET_TZ))
    period_start = as_utc(start) if start is not None else default_start
    period_end = as_utc(end) if end is not None else default_end
    if period_end <= period_start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="end must be after start")
    scopes = await views.contract_totals(start=period_start, end=period_end)
    return profitability_per_kw(scopes).model_dump(mode="json")
