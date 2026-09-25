"""Screen 3: Health (`/og/health`, 02b S8 row 5). Owner: ui-a (BUILD.md S4).

Process status grid (7 processes), feed freshness table, hub health histogram, cycle latency chart,
alert list, current degraded-mode banner (02b S6.4/S6.5). Server-rendered first paint from
`GET /og/api/health`; live updates via `og.sse("/og/api/stream/health", ...)` and
`og.sse("/og/api/stream/alerts", ...)`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opengrid.core.timeutil import to_utc
from opengrid.ui.api_client import ApiUnavailable, get_json
from opengrid.ui.render import render_status_badge
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health")


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (datetime.now(UTC) - to_utc(parsed)).total_seconds()


def _process_row(process: str, entry: dict[str, Any]) -> dict[str, Any]:
    status = "online" if entry.get("status") in ("ok", "online") else "offline"
    return {
        "process": process,
        "status_badge": render_status_badge(status, label=entry.get("status", "unknown")),
        "since_display": entry.get("ts", "-"),
    }


def _feed_row(entry: dict[str, Any]) -> dict[str, Any]:
    age = _age_s(entry.get("last_value_at"))
    return {
        "source": entry.get("source", "-"),
        "product": entry.get("product", "-"),
        "quality_badge": render_status_badge(entry.get("quality", "unknown")),
        "age_display": f"{age:.0f}s" if age is not None else "unknown",
    }


def _alert_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity_badge": render_status_badge(entry.get("severity", "unknown")),
        "summary": entry.get("summary", "-"),
        "opened_at": entry.get("opened_at", "-"),
    }


@router.get("", response_class=HTMLResponse)
async def health_screen(request: Request) -> HTMLResponse:
    degraded: str | None = None
    health: dict[str, Any] = {}

    try:
        raw = await get_json("/og/api/health")
        health = raw if isinstance(raw, dict) else {}
    except ApiUnavailable as exc:
        logger.warning("health screen: /og/api/health unavailable: %s", exc)
        degraded = str(exc)

    processes = health.get("processes", {}) if isinstance(health.get("processes"), dict) else {}
    feeds = health.get("feeds", []) if isinstance(health.get("feeds"), list) else []
    alerts = health.get("alerts", []) if isinstance(health.get("alerts"), list) else []

    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "health": health,
            "process_rows": [_process_row(name, entry) for name, entry in processes.items()],
            "feed_rows": [_feed_row(entry) for entry in feeds],
            "alert_rows": [_alert_row(entry) for entry in alerts],
            "degraded": degraded,
            "rendered_at": datetime.now(UTC).isoformat(),
        },
    )
