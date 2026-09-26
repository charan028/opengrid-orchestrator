"""The shared alerts component's routes (Control room and System Health). Owner: ui.

- `GET /og/alerts/panel`: the panel fragment for a filter/page (HTMX swaps it in place).
- `GET /og/alerts/notifications`: the header bell's popover (every page) plus the one critical line.
- `POST /og/alerts/ack-bulk/propose`: step 1 -- a confirm dialog naming how many alerts will be acked.
- `POST /og/alerts/ack-bulk/confirm?ids=`: step 2 (also each row's own Ack) -- relays to
  `POST /og/api/alerts/ack-bulk {alert_ids}`; until that endpoint exists (404/405), acknowledges one by
  one through `POST /og/api/alerts/{id}/ack` and reports how many went through. Operator only; each ack is
  audited by the API.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from opengrid.ui.alerts import POSTURE_PATH, alerts_panel, notification_centre, parse_ids, posture_strip
from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.role import is_operator, remote_user
from opengrid.ui.routes.fleet import _confirm_dialog_context
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/alerts")

ACK_BULK_PATH = "/og/api/alerts/ack-bulk"
_PANELS = ("control-room", "health", "notify")  # "notify": the header bell's popover


def panel_context(
    request: Request, alerts: list[dict[str, Any]], *, panel_id: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Template context for `_partials/alerts_panel.html` (both screens use exactly this)."""
    p = params or {}
    return {
        "panel_id": panel_id if panel_id in _PANELS else "control-room",
        "panel": alerts_panel(
            alerts,
            severity=p.get("alert_severity"),
            rule=p.get("alert_rule"),
            page=p.get("alert_page", 1),
            size=p.get("alert_size", 20),
        ),
        "is_operator": is_operator(request),
    }


async def posture_context() -> dict[str, Any]:
    """The safety-posture strip, from the guardian's current posture (never from open alerts)."""
    try:
        return {"posture": posture_strip(await get_json(POSTURE_PATH))}
    except ApiUnavailable as exc:
        logger.info("posture unavailable: %s", exc)
        return {"posture": None}


@router.get("/panel", response_class=HTMLResponse)
async def panel(
    request: Request,
    panel_id: str = Query(default="control-room"),
    alert_severity: str | None = Query(default=None),
    alert_rule: str | None = Query(default=None),
    alert_page: int = Query(default=1),
    alert_size: int = Query(default=20),
) -> HTMLResponse:
    alerts: list[dict[str, Any]] = []
    try:
        health = await get_json("/og/api/health")
        alerts = list(health.get("alerts") or []) if isinstance(health, dict) else []
    except ApiUnavailable as exc:
        logger.warning("alerts panel: /og/api/health unavailable: %s", exc)
    params = {
        "alert_severity": alert_severity,
        "alert_rule": alert_rule,
        "alert_page": alert_page,
        "alert_size": alert_size,
    }
    return templates.TemplateResponse(
        request,
        "_partials/alerts_panel.html",
        panel_context(request, alerts, panel_id=panel_id, params=params),
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request) -> HTMLResponse:
    """The header bell + popover (every page) and, out of band, the single critical status line."""
    from opengrid.ui.routes.health import DEGRADED_MODE_LABELS, degraded_modes_of, guardian_attention

    health: dict[str, Any] = {}
    unavailable: str | None = None
    try:
        raw = await get_json("/og/api/health")
        health = raw if isinstance(raw, dict) else {}
    except ApiUnavailable as exc:
        logger.warning("notifications: /og/api/health unavailable: %s", exc)
        unavailable = str(exc)
    alerts = [a for a in (health.get("alerts") or []) if isinstance(a, dict)]
    posture = (await posture_context())["posture"]
    centre = notification_centre(
        guardian_items=guardian_attention(alerts),
        degraded_modes=degraded_modes_of(health),
        mode_labels=DEGRADED_MODE_LABELS,
        posture=posture,
        alerts=alerts,
        base_path=BASE_PATH,
    )
    return templates.TemplateResponse(
        request,
        "_partials/notification_centre.html",
        {"nc": centre, "is_operator": is_operator(request), "unavailable": unavailable},
    )


def _require_operator(request: Request) -> None:
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")


@router.post("/ack-bulk/propose", response_class=HTMLResponse)
async def propose_bulk_ack(
    request: Request,
    panel_id: str = Form(default="control-room"),
    sel: Annotated[list[str] | None, Form()] = None,
    all_matching: str = Form(default=""),
    all_ids: str = Form(default=""),
) -> HTMLResponse:
    """Step 1: count what the selection means and ask once. Nothing is acknowledged yet."""
    _require_operator(request)
    ids = parse_ids(all_ids) if all_matching == "1" else parse_ids(",".join(sel or []))
    pid = panel_id if panel_id in _PANELS else "control-room"
    if not ids:
        return templates.TemplateResponse(
            request, "_partials/propose_error.html", {"message": "Select at least one unacknowledged alert."}
        )
    context = _confirm_dialog_context(
        dialog_id=f"{pid}-ack-bulk",
        title=f"Acknowledge {len(ids)} alert{'' if len(ids) == 1 else 's'}",
        proposal={
            "summary": f"Acknowledge {len(ids)} open alert{'' if len(ids) == 1 else 's'}. Acknowledging does "
            "not clear an alert; it records that an operator has seen it (each ack is audited).",
            "expires_in_s": None,
        },
        confirm_url=f"{BASE_PATH}/alerts/ack-bulk/confirm?panel_id={pid}&ids={','.join(str(i) for i in ids)}",
        confirm_label=f"Acknowledge {len(ids)}",
        variant="primary",
        target=f"#{pid}-ack-result",
    )
    return templates.TemplateResponse(request, "_partials/confirm_dialog.html", context)


def _ok(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    status_text = str(entry.get("status") or entry.get("result") or "").lower()
    return (
        bool(entry.get("ok"))
        or status_text in ("acked", "ok", "already_acked")
        or bool(entry.get("acked_by"))
    )


@router.post("/ack-bulk/confirm", response_class=HTMLResponse)
async def confirm_bulk_ack(
    request: Request, ids: str = Query(default=""), panel_id: str = Query(default="control-room")
) -> HTMLResponse:
    """Step 2 (and a row's own Ack): the bulk API when it exists, else one ack at a time."""
    _require_operator(request)
    wanted = parse_ids(ids)
    pid = panel_id if panel_id in _PANELS else "control-room"
    user = remote_user(request)
    acked: list[int] = []
    failed: list[int] = []
    mode = "bulk"
    try:
        body = await post_json(ACK_BULK_PATH, {"alert_ids": wanted}, remote_user=user)
        results = body.get("results") if isinstance(body, dict) else body
        by_id = {
            int(str(r.get("alert_id", r.get("id")))): r
            for r in results or []
            if isinstance(r, dict) and str(r.get("alert_id", r.get("id", ""))).isdigit()
        }
        for i in wanted:
            (acked if _ok(by_id.get(i)) else failed).append(i)
    except ApiUnavailable as exc:
        if exc.status_code not in (404, 405):
            logger.warning("bulk ack failed: %s", exc)
            return templates.TemplateResponse(
                request,
                "_partials/alerts_ack_result.html",
                {"panel_id": pid, "acked": [], "failed": wanted, "mode": "bulk", "message": str(exc)},
            )
        mode = "sequential"  # the bulk endpoint is not deployed yet: one audited ack per alert
        for i in wanted:
            try:
                await post_json(f"/og/api/alerts/{i}/ack", {}, remote_user=user)
                acked.append(i)
            except ApiUnavailable as single_exc:
                logger.info("ack of alert %s failed: %s", i, single_exc)
                failed.append(i)
    response = templates.TemplateResponse(
        request,
        "_partials/alerts_ack_result.html",
        {"panel_id": pid, "acked": acked, "failed": failed, "mode": mode, "message": None},
    )
    response.headers["HX-Trigger"] = "og-alerts-changed"
    return response
