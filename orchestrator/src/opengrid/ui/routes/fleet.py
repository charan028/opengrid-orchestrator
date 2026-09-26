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
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fleet")

_SAFESTOP_PROPOSE_PATH = "/og/api/safestop"
_COMMAND_PROPOSE_PATH = "/og/api/fleet/command"
_BULK_COMMAND_PATH = "/og/api/fleet/commands/bulk"
_MAP_PATH = "/og/api/fleet/map"
_HUB_STALE_AFTER_S = 10.0
_SAFESTOP_SCOPES = ("fleet", "zone", "bank")


def safestop_prefill(scope: str | None, scope_id: str | None) -> dict[str, str] | None:
    """Values for the safe-stop form when an operator follows a guardian safe-stop request
    (`opengrid.ui.routes.health.guardian_attention`). Only fills the step-1 form: the operator still
    proposes and then confirms the API's summary (K8); an unknown scope prefills nothing."""
    if scope not in _SAFESTOP_SCOPES:
        return None
    return {
        "scope": scope,
        "scope_id": (scope_id or "") if scope != "fleet" else "",
        "reason": "Guardian escalation: safe stop requested",
    }


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


def map_hub(hub: dict[str, Any]) -> dict[str, Any]:
    """The compact hub shape `static/og-map.js` draws. Mirrors `GET /og/api/fleet/map`'s documented
    fields (CR #19) so the map needs no change when that endpoint lands: until it does, `lat`/`lon` are
    absent and the module scatters the hub inside its real load zone, and `activity` is derived from
    health and the sign of `p_kw`."""
    return {
        "hub_id": hub.get("hub_id"),
        "bank_id": hub.get("bank_id"),
        "zone": hub.get("zone"),
        "health": hub.get("health"),
        "activity": hub.get("activity"),
        "kw": hub.get("kw", hub.get("p_kw")),
        "soc_kwh": hub.get("soc_kwh"),
        "soc_pct": hub.get("soc_pct"),
        "lat": hub.get("lat"),
        "lon": hub.get("lon"),
        "serving_obligations": hub.get("serving_obligations") or [],
        "can_serve_services": hub.get("can_serve_services") or [],
    }


def _serving_label(obligation: dict[str, Any]) -> str:
    """ "DATA_CENTER c6" -- the service and who it is for, the way CR #19 words the warning."""
    service = str(obligation.get("service_type") or "an obligation")
    customer = obligation.get("customer_id") or obligation.get("obligation_id")
    return f"{service} {str(customer)[:8]}" if customer else service


def bulk_risk_reasons(hubs: list[dict[str, Any]], hub_ids: list[str]) -> list[str]:
    """Why a bulk manual command over `hub_ids` needs the second confirmation (CR #19 item 2): any
    selected hub that is serving a customer, or that is in a critical/failure state. Returns one plain
    sentence per group, e.g. `3 hubs serving DATA_CENTER c6`; an empty list means the ordinary two-step
    confirm is enough. The API's own `requires_double_confirm` is honoured on top of this -- this is the
    console's independent read of the same rule, so the operator sees the reason even before proposing."""
    selected = set(hub_ids)
    chosen = [h for h in hubs if h.get("hub_id") in selected]
    serving: dict[str, int] = {}
    faulted = 0
    delivering = 0
    for hub in chosen:
        obligations = hub.get("serving_obligations") or []
        if obligations:
            for obligation in obligations:
                label = _serving_label(obligation)
                serving[label] = serving.get(label, 0) + 1
        elif float(hub.get("p_kw") or hub.get("kw") or 0) > 0.1:
            delivering += 1
        if str(hub.get("health") or "").lower() in ("fault", "offline"):
            faulted += 1
    reasons = [
        f"{count} hub{'' if count == 1 else 's'} serving {label}"
        for label, count in sorted(serving.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if delivering:
        reasons.append(f"{delivering} hub{'' if delivering == 1 else 's'} currently delivering power")
    if faulted:
        reasons.append(f"{faulted} hub{'' if faulted == 1 else 's'} in a fault or offline state")
    return reasons


def parse_hub_ids(raw: str | None) -> list[str]:
    """The selection posted by the Fleet map/table: comma-separated hub ids, de-duplicated, order kept."""
    seen: dict[str, None] = {}
    for part in (raw or "").split(","):
        hub_id = part.strip()
        if hub_id:
            seen.setdefault(hub_id, None)
    return list(seen)


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
    safestop_scope: str | None = Query(default=None),
    safestop_scope_id: str | None = Query(default=None),
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

    # The richer map payload (coordinates, activity, obligations) when it exists; the hub list otherwise.
    # Its absence is not a degraded screen -- the map draws from the hub list either way (CR #19).
    map_hubs = [map_hub(h) for h in hubs]
    try:
        raw_map = await get_json(_MAP_PATH, params=params)
        items = raw_map.get("items", raw_map) if isinstance(raw_map, dict) else raw_map
        if isinstance(items, list) and items:
            map_hubs = [map_hub(h) for h in items]
    except ApiUnavailable as exc:
        logger.info(
            "fleet screen: %s not serving yet (%s); drawing the map from the hub list", _MAP_PATH, exc
        )

    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "table_rows": [_to_table_row(h) for h in hubs],
            "map_hubs": map_hubs,
            "filters": {"zone": zone, "bank": bank, "health": health},
            "safestop_prefill": safestop_prefill(safestop_scope, safestop_scope_id),
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
        proposal = await post_json(_SAFESTOP_PROPOSE_PATH, payload, remote_user=remote_user(request))
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
        result = await post_json(
            f"{_SAFESTOP_PROPOSE_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
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


# -- safe-stop RELEASE, two operators (K8: guardian-signed, og-safestop relays) -----------------------


def _release_result(request: Request, **context: Any) -> HTMLResponse:
    base = {"result": None, "status_code": None, "message": None, "release_request": None}
    return templates.TemplateResponse(request, "_partials/safestop_release_result.html", {**base, **context})


@router.post("/safestop/release/request", response_class=HTMLResponse)
async def request_release(
    request: Request,
    scope: str = Form(...),
    scope_id: str = Form(default=""),
    reason: str = Form(...),
) -> HTMLResponse:
    """Operator A: files the release request. Nothing is released; a DIFFERENT operator must approve it."""
    _require_operator(request)
    path = f"{_SAFESTOP_PROPOSE_PATH}/{scope}/{scope_id or 'FLEET'}/release"
    try:
        accepted = await post_json(path, {"reason": reason}, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("safestop release request failed: %s", exc)
        return _release_result(request, status_code=exc.status_code, message=str(exc))
    return _release_result(request, release_request=accepted)


@router.post("/safestop/release/review", response_class=HTMLResponse)
async def review_release(request: Request, proposal_id: str = Form(...)) -> HTMLResponse:
    """Operator B, step 1 of 2: an open confirm dialog for the request id; nothing is written yet."""
    _require_operator(request)
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"release-approve-{proposal_id}",
            title="Approve safe-stop release",
            proposal={
                "summary": f"Approve release request {proposal_id}. You must not be the operator who "
                "requested it; the guardian signs only for two different authorised operators.",
                "expires_in_s": None,
            },
            confirm_url=f"{BASE_PATH}/fleet/safestop/release/{proposal_id}/approve",
            confirm_label="Approve release",
            variant="danger",
            target="#safestop-release-result",
        ),
    )


@router.post("/safestop/release/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_release(request: Request, proposal_id: str) -> HTMLResponse:
    """Operator B, step 2 of 2: relays the approval; renders released / pending / refused -- never
    assumes the stop was released."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_SAFESTOP_PROPOSE_PATH}/release/{proposal_id}/approve", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        logger.warning("safestop release approve failed: %s", exc)
        return _release_result(request, status_code=exc.status_code, message=str(exc))
    return _release_result(request, result=result, status_code=status.HTTP_200_OK)


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
        proposal = await post_json(_COMMAND_PROPOSE_PATH, payload, remote_user=remote_user(request))
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


@router.post("/command/bulk/propose", response_class=HTMLResponse)
async def propose_bulk_command(
    request: Request,
    hub_ids: str = Form(default=""),
    p_kw_setpoint: float = Form(...),
    reason: str = Form(...),
) -> HTMLResponse:
    """Step 1 of 2 for a selection (CR #19 item 2): relays the selected hubs, setpoint and reason to
    `POST /og/api/fleet/commands/bulk` and renders the real proposal as an already-open confirm dialog.
    When any selected hub is serving a customer or is in a fault/offline state -- this module's own
    `bulk_risk_reasons`, or the API's `requires_double_confirm` -- the dialog demands a second,
    explicit acknowledgement naming the reason before its confirm button will act. The guardian still
    evaluates and signs every command at confirm time; nothing here bypasses it."""
    _require_operator(request)
    selected = parse_hub_ids(hub_ids)
    if not selected:
        return templates.TemplateResponse(
            request,
            "_partials/propose_error.html",
            {"message": "select at least one hub on the map or in the table first"},
        )
    hubs: list[dict[str, Any]] = []
    try:
        raw = await get_json("/og/api/fleet/hubs")
        hubs = raw.get("items", []) if isinstance(raw, dict) else []
    except ApiUnavailable as exc:
        logger.info("bulk propose: hub list unavailable for the risk check (%s)", exc)
    reasons = bulk_risk_reasons(hubs, selected)
    payload = {"hub_ids": selected, "p_kw_setpoint": p_kw_setpoint, "reason": reason}
    try:
        proposal = await post_json(_BULK_COMMAND_PATH, payload, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("fleet bulk command propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    double = bool(proposal.get("requires_double_confirm")) or bool(reasons)
    context = _confirm_dialog_context(
        dialog_id=f"bulk-confirm-{proposal['proposal_id']}",
        title=f"Confirm command for {len(selected)} hub" + ("" if len(selected) == 1 else "s"),
        proposal=proposal,
        confirm_url=f"{BASE_PATH}/fleet/command/bulk/{proposal['proposal_id']}/confirm",
        confirm_label="Send to selection",
        variant="danger",
        target="#bulk-confirm-result",
    )
    if double:
        context["acknowledge"] = "I understand this overrides what these hubs are doing now: " + "; ".join(
            reasons or ["the API flagged this selection as high risk"]
        )
    return templates.TemplateResponse(request, "_partials/confirm_dialog.html", context)


@router.post("/command/bulk/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_bulk_command(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2 for a selection: relays the confirmation and renders the same pass/veto/timeout/
    expired fragment the single-hub flow uses."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_BULK_COMMAND_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        logger.warning("fleet bulk command confirm failed: %s", exc)
        result = exc.detail if isinstance(exc.detail, dict) else None
        return templates.TemplateResponse(
            request,
            "_partials/fleet_bulk_confirm_result.html",
            {"result": result, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_bulk_confirm_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


@router.post("/command/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_command(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation to `POST /og/api/fleet/command/{proposal_id}/confirm` and
    renders the guardian's pass/veto/timeout/expired result. A 409 veto's body is itself a
    `CommandConfirmResult` (outcome != "PASS"), so it renders the same as a successful pass, just with a
    different outcome."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_COMMAND_PROPOSE_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
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
