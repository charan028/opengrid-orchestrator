"""Screen 1: Control room (`/og/`, 02b S8 row 1). Owner: ui-a (BUILD.md S4).

Fleet map, live ERCOT price/load/wind/solar, fleet MW/MWh, active commitments, today's net margin,
invariant counters (reserve breaches, kWh sold twice, commitment switches -- all must read 0, A10), open
alerts (with an acknowledge action, mirroring the Health screen's). Server-rendered first paint from
`GET /og/api/health` + `GET /og/api/fleet/hubs`; live updates are the browser's job via
`og.sse("/og/api/stream/control-room", ...)`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def control_room(request: Request) -> HTMLResponse:
    degraded: str | None = None
    health: dict[str, Any] = {}
    hubs: list[dict[str, Any]] = []

    try:
        raw_health = await get_json("/og/api/health")
        health = raw_health if isinstance(raw_health, dict) else {}
    except ApiUnavailable as exc:
        logger.warning("control room: /og/api/health unavailable: %s", exc)
        degraded = str(exc)

    try:
        raw_hubs = await get_json("/og/api/fleet/hubs")
        hubs = raw_hubs.get("items", []) if isinstance(raw_hubs, dict) else []
    except ApiUnavailable as exc:
        logger.warning("control room: /og/api/fleet/hubs unavailable: %s", exc)
        degraded = degraded or str(exc)

    return templates.TemplateResponse(
        request,
        "control_room.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "health": health,
            "hubs": hubs,
            "degraded": degraded,
        },
    )


@router.post("/alerts/ack", response_class=HTMLResponse)
async def ack_alert(request: Request, alert_id: int = Form(...)) -> HTMLResponse:
    """Relay to the real `POST /og/api/alerts/{alert_id}/ack` (path param, operator-only) -- the Control
    room's own copy of the Health screen's ack action (BUILD.md code-review round item 5: control_room.py
    previously had no ack action at all)."""
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")
    try:
        alert = await post_json(f"/og/api/alerts/{alert_id}/ack", {})
    except ApiUnavailable as exc:
        logger.warning("control room alert ack failed for alert_id=%s: %s", alert_id, exc)
        return templates.TemplateResponse(request, "_partials/alert_ack_result.html", {"message": str(exc)})
    return templates.TemplateResponse(request, "_partials/alert_ack_result.html", {"alert": alert})
