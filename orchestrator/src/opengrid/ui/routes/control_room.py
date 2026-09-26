"""Screen 1: Control room (`/og/`, 02b S8 row 1). Owner: ui-a (BUILD.md S4).

Fleet map, live ERCOT price/load/wind/solar, fleet MW/MWh, active commitments, today's net margin,
invariant counters (reserve breaches, kWh sold twice, commitment switches -- all must read 0, A10), open
alerts (with an acknowledge action, mirroring the Health screen's). Server-rendered first paint from
`GET /og/api/health` + `GET /og/api/fleet/hubs`; live updates are the browser's job via
`og.sse("/og/api/stream/control-room", ...)`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.render import render_status_badge
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.routes.health import ack_message
from opengrid.ui.routes.markets import _SERIES_QUERY, series_chart_view
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

    # First paint of the market ticker from the same wholesale-price series the Markets screen plots
    # (found live: the control-room stream carries no price series, so the ticker stayed empty forever).
    # A missing series is logged and leaves the ticker empty; it never degrades the whole screen.
    ticker_rows: list[dict[str, Any]] = []
    try:
        raw_ticker = await get_json("/og/api/markets/series", params=_SERIES_QUERY["price"])
        ticker_rows = raw_ticker if isinstance(raw_ticker, list) else []
    except ApiUnavailable as exc:
        logger.warning("control room: market ticker series unavailable: %s", exc)

    story = None
    try:
        raw_obl = await get_json("/og/api/dispatch/opportunities")
        story = story_view(health, raw_obl if isinstance(raw_obl, list) else [])
    except ApiUnavailable as exc:
        logger.warning("control room: obligations unavailable for the headline: %s", exc)

    return templates.TemplateResponse(
        request,
        "control_room.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "health": health,
            "story": story,
            "alert_rows": [_alert_row(entry) for entry in health.get("alerts", []) or []],
            "hubs": hubs,
            "ticker": series_chart_view(ticker_rows, series_key="price"),
            "degraded": degraded,
        },
    )


def story_view(health: dict[str, Any], obligations: list[dict[str, Any]]) -> dict[str, Any]:
    """The control room's one-line answer to "what is the fleet doing right now": hubs online, what is
    promised to whom and when, and whether any promise was broken today. Everything else on the screen
    is evidence for this sentence."""
    counts = health.get("hub_health_counts") or {}
    live = [o for o in obligations if o.get("state") in ("SELECTED", "COMMITTED", "DELIVERING")]
    promised_kw = 0.0
    for o in live:
        with contextlib.suppress(TypeError, ValueError):
            promised_kw += float(o.get("committed_qty_kw") or 0)
    next_window = min((str(o.get("window_start")) for o in live if o.get("window_start")), default=None)
    buyers = len({o.get("contract_id") for o in live})
    broken = (
        int(health.get("reserve_breaches") or 0)
        + int(health.get("double_sold_kwh") or 0)
        + int(health.get("commitment_switches") or 0)
    )
    return {
        "hubs_online": int(counts.get("online") or 0),
        "hubs_total": sum(int(v or 0) for v in counts.values()),
        "live_obligations": len(live),
        "buyers": buyers,
        "promised_kw": promised_kw,
        "next_window": next_window[11:16] if next_window and len(next_window) >= 16 else next_window,
        "delivering": sum(1 for o in live if o.get("state") == "DELIVERING"),
        "promises_broken": broken,
    }


def _alert_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Same shape as the Health screen's alert rows: the severity is a badge, not raw text."""
    return {
        "id": entry.get("id", "-"),
        "severity_badge": render_status_badge(str(entry.get("severity", "unknown"))),
        "summary": entry.get("summary", "-"),
        "opened_at": entry.get("opened_at", "-"),
    }


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
        return templates.TemplateResponse(
            request, "_partials/alert_ack_result.html", {"message": ack_message(exc, alert_id)}
        )
    return templates.TemplateResponse(request, "_partials/alert_ack_result.html", {"alert": alert})
