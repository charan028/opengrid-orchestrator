"""Screen 3: Health (`/og/health`, 02b S8 row 5). Owner: ui-a (BUILD.md S4).

Process status grid (7 processes), feed freshness table, hub health histogram, cycle latency chart,
alert list with an acknowledge action, current degraded-mode banner (02b S6.4/S6.5). Server-rendered
first paint comes from `GET /og/api/health`; live updates via `og.sse("/og/api/stream/health", ...)` and
`og.sse("/og/api/stream/alerts", ...)`.

The alert-ack action used to `hx-post` a free-text `alert_id` straight to `{base_path}/api/health/alerts/
ack`, which is not a real `opengrid.api` endpoint at all -- the actual endpoint is
`POST /og/api/alerts/{alert_id}/ack` (`alert_id` as an int *path* parameter, not a body field, per
`opengrid.api.routers.health.ack_alert`). This module now relays that correctly: the form posts to a
UI-owned route with `alert_id` as a normal form field, which builds the real path-parameter URL and
calls it server-side via `opengrid.ui.api_client.post_json` (BUILD.md code-review round item 5).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from opengrid.core.timeutil import to_utc
from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.render import render_status_badge
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.templating import BASE_PATH, templates

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
    status_value = "online" if entry.get("status") in ("ok", "online") else "offline"
    return {
        "process": process,
        "status_badge": render_status_badge(status_value, label=entry.get("status", "unknown")),
        "since_display": entry.get("ts", "-"),
    }


def format_age(age_s: float | None) -> str:
    """`5s`, `34m`, `1h 34m`: day-ahead feeds are legitimately hours old and `5675s` reads as a fault."""
    if age_s is None:
        return "unknown"
    total = int(age_s)
    if total < 60:
        return f"{total}s"
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def ack_message(exc: ApiUnavailable, alert_id: int) -> str:
    """Operator-readable outcome of a failed acknowledge (live: a 404 rendered the raw httpx text with a
    Mozilla docs link)."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return f"No open alert with id {alert_id}."
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return "Operator role required to acknowledge alerts."
    if exc.status_code is not None:
        return f"Alert {alert_id} could not be acknowledged (API returned {exc.status_code})."
    return f"Alert {alert_id} could not be acknowledged: the API is unreachable."


#: `GET /og/api/health` `degraded_modes` (02b S6.5, health/rules.py `derive_degraded_modes`) in operator words.
DEGRADED_MODE_LABELS: dict[str, str] = {
    "NO_NEW_COMMITMENTS": "Feed stale",
    "HOLD_LOCAL_AUTONOMY": "Engine down",
    "HOLD": "Guardian down",
    "DIST_DEFERRAL_OPEN_LOOP": "SCADA silent",
}

SAFE_STOP_REQUESTED_RULE = "ALR-SAFE-STOP-REQUESTED"
SCOPE_CONSERVATIVE_RULE = "ALR-SCOPE-CONSERVATIVE"
#: The guardian's escalation alerts open their summary with the scope key: "BANK:bank-007: ... safe stop
#: requested" (ALR-SAFE-STOP-REQUESTED), "ZONE:LZ_NORTH CONSERVATIVE: ..." (ALR-SCOPE-CONSERVATIVE).
_GUARDIAN_SCOPE = re.compile(r"^(BANK|ZONE):([^:\s]+)[:\s]")


def degraded_modes_of(health: dict[str, Any]) -> list[str]:
    """The payload's `degraded_modes`, de-duplicated in order; `[]` when absent (an older API)."""
    raw = health.get("degraded_modes")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(mode) for mode in raw if mode))


def degraded_mode_banner_text(modes: Iterable[str]) -> str | None:
    """ "Feed stale + Guardian down", or None when nothing is degraded. An unknown mode is shown by its code,
    never dropped: a degraded system must not look healthy because the console is behind the API."""
    labels = [DEGRADED_MODE_LABELS.get(mode, mode) for mode in modes]
    return " + ".join(labels) if labels else None


def guardian_attention(alerts: Iterable[Any]) -> list[dict[str, Any]]:
    """The guardian's escalation alerts (ES06-S04), safe-stop requests first. A request links to the Fleet
    screen's two-step safe-stop form prefilled with the requested scope; the operator still proposes and
    confirms there. Nothing on this path engages or confirms a stop (K8)."""
    items: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        rule = alert.get("rule")
        if rule not in (SAFE_STOP_REQUESTED_RULE, SCOPE_CONSERVATIVE_RULE):
            continue
        summary = str(alert.get("summary") or "")
        match = _GUARDIAN_SCOPE.match(summary)
        scope, scope_id = (match.group(1).lower(), match.group(2)) if match else (None, None)
        stop_requested = rule == SAFE_STOP_REQUESTED_RULE
        review_url = None
        if stop_requested:
            query = f"?{urlencode({'safestop_scope': scope, 'safestop_scope_id': scope_id})}" if scope else ""
            review_url = f"{BASE_PATH}/fleet{query}#safestop-propose-form"
        items.append(
            {
                "id": alert.get("id", "-"),
                "rule": rule,
                "stop_requested": stop_requested,
                "title": "Guardian requests a safe stop" if stop_requested else "Scope held conservative",
                "scope_label": f"{scope} {scope_id}" if scope else None,
                "summary": summary,
                "opened_at": alert.get("opened_at", "-"),
                "review_url": review_url,
            }
        )
    items.sort(key=lambda item: not item["stop_requested"])
    return items


def degraded_context(health: dict[str, Any]) -> dict[str, Any]:
    """Template context shared by the Health and Control room screens: the degraded-mode banner, the
    guardian escalations, and the label table the live-stream handler uses (`og.renderDegradedBanner`)."""
    modes = degraded_modes_of(health)
    alerts = health.get("alerts") if isinstance(health.get("alerts"), list) else []
    return {
        "degraded_modes": modes,
        "degraded_mode_banner": degraded_mode_banner_text(modes),
        "degraded_mode_labels": DEGRADED_MODE_LABELS,
        "guardian_items": guardian_attention(alerts or []),
    }


def _feed_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": entry.get("source", "-"),
        "product": entry.get("product", "-"),
        "quality_badge": render_status_badge(entry.get("quality", "unknown")),
        "age_display": format_age(_age_s(entry.get("last_value_at"))),
    }


def _alert_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id", "-"),
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
            "hub_counts": health.get("hub_health_counts") or {},
            "degraded": degraded,
            "rendered_at": datetime.now(UTC).isoformat(),
            **degraded_context(health),
        },
    )


@router.post("/alerts/ack", response_class=HTMLResponse)
async def ack_alert(request: Request, alert_id: int = Form(...)) -> HTMLResponse:
    """Relay to the real `POST /og/api/alerts/{alert_id}/ack` (path param, operator-only). Shared by the
    Health screen's own alert list and, via `opengrid.ui.routes.control_room`, the Control room's."""
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")
    try:
        alert = await post_json(f"/og/api/alerts/{alert_id}/ack", {}, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("health alert ack failed for alert_id=%s: %s", alert_id, exc)
        return templates.TemplateResponse(
            request, "_partials/alert_ack_result.html", {"message": ack_message(exc, alert_id)}
        )
    return templates.TemplateResponse(request, "_partials/alert_ack_result.html", {"alert": alert})
