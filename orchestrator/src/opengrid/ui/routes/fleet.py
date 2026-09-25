"""Screen 2: Fleet monitoring & control (`/og/fleet`, 02b S8 row 2). Owner: ui-a (BUILD.md S4).

Bank/hub table with drill-down (SoC, P, health, lease, last command). Manual command and scoped safe
stop are two-step confirmations *mediated* by this module (BUILD.md code-review round items 1-2): a
`<form>` posts to a UI-owned `.../propose` route below, which relays the proposal to `opengrid.api`
server-side (`opengrid.ui.api_client.post_json`) and renders `_partials/confirm_dialog.html` populated
with the real `proposal_id`/`summary`/`expires_in_s` from the API's response; only the dialog's own
confirm button then posts to a second UI-owned `.../{proposal_id}/confirm` route, which relays the
confirmation and renders a pass/veto/timeout/expired result fragment. The template never talks to
`opengrid.api` directly any more -- see `templates/fleet.html` and the base contract README.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from opengrid.core.timeutil import to_utc
from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.render import render_stale_badge, render_status_badge
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fleet")

_SAFESTOP_PROPOSE_PATH = "/og/api/safestop"
_COMMAND_PROPOSE_PATH = "/og/api/fleet/command"
_HUB_STALE_AFTER_S = 10.0


def _require_operator(request: Request) -> None:
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (datetime.now(UTC) - to_utc(parsed)).total_seconds()


def _to_table_row(hub: dict[str, Any]) -> dict[str, Any]:
    health = hub.get("health", "unknown")
    last_seen_at = hub.get("last_seen_at")
    return {
        "hub_id": hub.get("hub_id", "-"),
        "bank_id": hub.get("bank_id", "-"),
        "zone": hub.get("zone", "-"),
        "health_badge": render_status_badge(health),
        "soc_kwh": hub.get("soc_kwh", "-"),
        "p_kw": hub.get("p_kw", "-"),
        "age_badge": render_stale_badge(
            _age_s(last_seen_at), since_iso=last_seen_at, stale_after_s=_HUB_STALE_AFTER_S
        ),
    }


def _confirm_dialog_context(
    *,
    dialog_id: str,
    title: str,
    proposal: dict[str, Any],
    confirm_url: str,
    confirm_label: str,
    variant: str,
    target: str,
) -> dict[str, Any]:
    """Shared shape for a step-1 `ProposalAccepted` response rendered as an already-open
    `_partials/confirm_dialog.html` fragment (BUILD.md code-review round item 3: real proposal data, not
    a static hand-built summary)."""
    return {
        "dialog_id": dialog_id,
        "open_default": True,
        "show_trigger": False,
        "title": title,
        "summary": proposal.get("summary", ""),
        "confirm_url": confirm_url,
        "confirm_label": confirm_label,
        "variant": variant,
        "target": target,
        "expires_in_s": proposal.get("expires_in_s"),
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


# -- scoped safe stop, two-step confirmation (BUILD.md code-review round item 1) ----------------------


@router.post("/safestop/propose", response_class=HTMLResponse)
async def propose_safestop(
    request: Request,
    scope: str = Form(...),
    scope_id: str = Form(default=""),
    reason: str = Form(...),
) -> HTMLResponse:
    """Step 1 of 2: relays the operator's scope/reason to `POST /og/api/safestop` and renders the real
    proposal as an already-open confirm dialog. Nothing is stopped yet."""
    _require_operator(request)
    payload = {"scope": scope, "scope_id": scope_id or None, "reason": reason}
    try:
        proposal = await post_json(_SAFESTOP_PROPOSE_PATH, payload)
    except ApiUnavailable as exc:
        logger.warning("fleet safestop propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"safestop-confirm-{proposal['proposal_id']}",
            title="Confirm scoped safe stop",
            proposal=proposal,
            confirm_url=f"{BASE_PATH}/fleet/safestop/{proposal['proposal_id']}/confirm",
            confirm_label="Engage safe stop",
            variant="safestop",
            target="#safestop-confirm-result",
        ),
    )


@router.post("/safestop/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_safestop(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation to `POST /og/api/safestop/{proposal_id}/confirm` and renders
    the engaged/timeout/expired result -- never assumes success."""
    _require_operator(request)
    try:
        result = await post_json(f"{_SAFESTOP_PROPOSE_PATH}/{proposal_id}/confirm", {})
    except ApiUnavailable as exc:
        logger.warning("fleet safestop confirm failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "_partials/safestop_confirm_result.html",
            {"result": None, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/safestop_confirm_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


# -- manual command, two-step confirmation (BUILD.md code-review round item 2) -------------------------


@router.post("/command/propose", response_class=HTMLResponse)
async def propose_command(
    request: Request,
    bank_id: str = Form(default=""),
    hub_id: str = Form(default=""),
    p_kw_setpoint: float = Form(...),
    reason: str = Form(...),
) -> HTMLResponse:
    """Step 1 of 2: relays the operator's target/setpoint/reason to `POST /og/api/fleet/command` and
    renders the real proposal as an already-open confirm dialog. Guardian evaluation happens at confirm
    time, not here."""
    _require_operator(request)
    if not bank_id and not hub_id:
        return templates.TemplateResponse(
            request, "_partials/propose_error.html", {"message": "bank id or hub id is required"}
        )
    payload = {
        "bank_id": bank_id or None,
        "hub_id": hub_id or None,
        "p_kw_setpoint": p_kw_setpoint,
        "reason": reason,
    }
    try:
        proposal = await post_json(_COMMAND_PROPOSE_PATH, payload)
    except ApiUnavailable as exc:
        logger.warning("fleet command propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"command-confirm-{proposal['proposal_id']}",
            title="Confirm manual command",
            proposal=proposal,
            confirm_url=f"{BASE_PATH}/fleet/command/{proposal['proposal_id']}/confirm",
            confirm_label="Send command",
            variant="danger",
            target="#command-confirm-result",
        ),
    )


@router.post("/command/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_command(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation to `POST /og/api/fleet/command/{proposal_id}/confirm` and
    renders the guardian's pass/veto/timeout/expired result. A 409 veto's body is itself a
    `CommandConfirmResult` (outcome != "PASS"), so it renders the same as a successful pass, just with a
    different outcome."""
    _require_operator(request)
    try:
        result = await post_json(f"{_COMMAND_PROPOSE_PATH}/{proposal_id}/confirm", {})
    except ApiUnavailable as exc:
        logger.warning("fleet command confirm failed: %s", exc)
        result = exc.detail if isinstance(exc.detail, dict) else None
        return templates.TemplateResponse(
            request,
            "_partials/fleet_command_confirm_result.html",
            {"result": result, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_command_confirm_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )
