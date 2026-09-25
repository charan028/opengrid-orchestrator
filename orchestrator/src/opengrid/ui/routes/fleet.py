"""Screen 2: Fleet monitoring & control (`/og/fleet`, 02b S8 row 2). Owner: ui-a (BUILD.md S4).

Bank/hub table with drill-down (SoC, P, health, lease, last command); manual command and scoped safe
stop are rendered as forms/buttons that `hx-post` straight to `opengrid.api`'s own two-step endpoints
(`POST /og/api/fleet/command[/confirm]`, `POST /og/api/safestop[/confirm]`) -- this module never proxies
those writes, it only fetches read models for the initial paint and hides the write controls from a
viewer (`is_operator`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json
from opengrid.ui.render import render_status_badge
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fleet")


def _to_table_row(hub: dict[str, Any]) -> dict[str, Any]:
    health = hub.get("health", "unknown")
    return {
        "hub_id": hub.get("hub_id", "-"),
        "bank_id": hub.get("bank_id", "-"),
        "zone": hub.get("zone", "-"),
        "health_badge": render_status_badge(health),
        "soc_kwh": hub.get("soc_kwh", "-"),
        "p_kw": hub.get("p_kw", "-"),
    }


@router.get("", response_class=HTMLResponse)
async def fleet_screen(
    request: Request,
    zone: str | None = Query(default=None),
    bank: str | None = Query(default=None),
    health: str | None = Query(default=None),
) -> HTMLResponse:
    params = {k: v for k, v in {"zone": zone, "bank": bank, "health": health}.items() if v}
    degraded: str | None = None
    hubs: list[dict[str, Any]] = []

    try:
        raw = await get_json("/og/api/fleet/hubs", params=params)
        hubs = raw.get("items", []) if isinstance(raw, dict) else []
    except ApiUnavailable as exc:
        logger.warning("fleet screen: /og/api/fleet/hubs unavailable: %s", exc)
        degraded = str(exc)

    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "table_rows": [_to_table_row(h) for h in hubs],
            "filters": {"zone": zone, "bank": bank, "health": health},
            "degraded": degraded,
            "rendered_at": datetime.now(UTC).isoformat(),
        },
    )


@router.get("/hubs/{hub_id}", response_class=HTMLResponse)
async def hub_drilldown(request: Request, hub_id: str) -> HTMLResponse:
    try:
        raw = await get_json(f"/og/api/fleet/hubs/{hub_id}")
        hub = raw if isinstance(raw, dict) else {"hub_id": hub_id}
    except ApiUnavailable as exc:
        logger.warning("hub drilldown: /og/api/fleet/hubs/%s unavailable: %s", hub_id, exc)
        hub = {"hub_id": hub_id, "error": str(exc)}

    return templates.TemplateResponse(
        request,
        "_partials/hub_drilldown.html",
        {"hub": hub, "role": role_of(request), "is_operator": is_operator(request)},
    )
