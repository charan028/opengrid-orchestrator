"""Screen 8: Power quality & assets (`/og/pq`, WP-J 07-delivery/06 S6.6/S6.7). Owner: ui (MERGE).

Fleet-wide: open maintenance work orders and the drift/quarantine they record. Per hub (`?hub=`): the
latest waveform summary (per-phase RMS, PF, THD, phase angle, frequency), the harmonic spectrum (latest
bars plus the last 15 minutes of current harmonics), raw-capture metadata, the bank's measured PQ
aggregate, asset health with its event history, and calibration history with the guardian's G-25
decision. Everything is read from the WP-J API (`opengrid.api.routers.pq`); this module computes no PQ
figure (`opengrid.core.pq` is canonical).

Operator writes are the API's own two-step flows, relayed like Fleet's safe stop: an on-demand raw
waveform capture, and a remote calibration request (which only records a PENDING attempt; the guardian
decides and signs, K3). Nothing happens on the first click.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.routes.fleet import _confirm_dialog_context, _require_operator
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pq")

_PHASES = ("a", "b", "c")
_SUMMARY_STALE_AFTER_S = 20.0  # 2x the 10 s summary cadence (S5.4), as the API's freshness gate


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def summary_view(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per-phase rows of the latest waveform summary; `None` when the hub reported nothing in the window."""
    if not summary:
        return None
    rows = []
    for ph in _PHASES:
        row = {
            "phase": ph.upper(),
            "v_rms": _f(summary.get(f"v_rms_{ph}")),
            "i_rms": _f(summary.get(f"i_rms_{ph}")),
            "pf": _f(summary.get(f"pf_{ph}")),
            "thd_v": _f(summary.get(f"thd_v_pct_{ph}")),
            "thd_i": _f(summary.get(f"thd_i_pct_{ph}")),
            "angle": _f(summary.get(f"phase_angle_deg_{ph}")),
        }
        if any(v is not None for k, v in row.items() if k != "phase"):
            rows.append(row)
    return {"ts": summary.get("ts"), "freq_hz": _f(summary.get("freq_hz")), "phases": rows}


def _orders(harmonics: dict[str, Any] | None) -> list[str]:
    return sorted((harmonics or {}).keys(), key=lambda k: int(k) if str(k).isdigit() else 999)


def spectrum_bars(summary: dict[str, Any] | None) -> dict[str, Any]:
    """ECharts option: the latest summary's harmonic magnitudes (% of fundamental), voltage and current."""
    hv = (summary or {}).get("harmonics_v") or {}
    hi = (summary or {}).get("harmonics_i") or {}
    orders = sorted(set(_orders(hv)) | set(_orders(hi)), key=lambda k: int(k) if str(k).isdigit() else 999)
    return {
        "xAxis": {"type": "category", "name": "order", "data": [f"H{o}" for o in orders]},
        "yAxis": {"type": "value", "name": "% of fund."},
        "legend": {},
        "tooltip": {"trigger": "axis"},
        "series": [
            {
                "name": "Current",
                "type": "bar",
                "data": [_f((hi.get(o) or {}).get("mag_pct")) for o in orders],
            },
            {
                "name": "Voltage",
                "type": "bar",
                "itemStyle": {"color": "token:--muted@0.7"},
                "data": [_f((hv.get(o) or {}).get("mag_pct")) for o in orders],
            },
        ],
        "empty": not orders,
    }


def spectrum_trend(points: list[dict[str, Any]]) -> dict[str, Any]:
    """ECharts option: each current-harmonic order's magnitude over the spectrum window, oldest first."""
    orders: list[str] = []
    for p in points:
        for o in _orders(p.get("harmonics_i")):
            if o not in orders:
                orders.append(o)
    orders.sort(key=lambda k: int(k) if str(k).isdigit() else 999)
    return {
        "xAxis": {"type": "time"},
        "yAxis": {"type": "value", "name": "% of fund."},
        "legend": {},
        "tooltip": {"trigger": "axis"},
        "series": [
            {
                "name": f"I H{o}",
                "type": "line",
                "showSymbol": False,
                "data": [
                    [p["ts"], _f(((p.get("harmonics_i") or {}).get(o) or {}).get("mag_pct"))] for p in points
                ],
            }
            for o in orders
        ],
        "empty": not points,
    }


def work_orders_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OPEN first, then IN_PROGRESS, then the rest; evidence flattened to one readable line."""
    rank = {"OPEN": 0, "IN_PROGRESS": 1}
    out = []
    for r in rows:
        evidence = r.get("evidence")
        text = (
            ", ".join(f"{k}: {v}" for k, v in evidence.items() if k not in ("calibration_id",))
            if isinstance(evidence, dict)
            else str(evidence or "")
        )
        out.append({**r, "evidence_text": text})
    return sorted(
        out, key=lambda r: (rank.get(str(r.get("status")), 2), str(r.get("opened_at") or "")), reverse=False
    )


async def _get(path: str, *, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    try:
        return await get_json(path, params=params), None
    except ApiUnavailable as exc:
        if exc.status_code == 404:
            return None, None
        logger.warning("pq screen: %s unavailable: %s", path, exc)
        return None, str(exc)


@router.get("", response_class=HTMLResponse)
async def pq_screen(request: Request, hub: str | None = Query(default=None)) -> HTMLResponse:
    degraded: str | None = None
    raw_orders, err = await _get("/og/api/work-orders", params={"limit": 100})
    degraded = degraded or err
    orders = work_orders_view(raw_orders if isinstance(raw_orders, list) else [])
    hub_id = (hub or "").strip() or next((str(o["hub_id"]) for o in orders if o.get("status") == "OPEN"), "")

    ctx: dict[str, Any] = {"hub_id": hub_id, "hub_found": False}
    if hub_id:
        waveform, err = await _get(f"/og/api/hubs/{hub_id}/waveform")
        degraded = degraded or err
        ctx["hub_found"] = waveform is not None
        if waveform is not None:
            spectrum, err = await _get(f"/og/api/hubs/{hub_id}/spectrum")
            degraded = degraded or err
            health, err = await _get(f"/og/api/hubs/{hub_id}/asset-health")
            degraded = degraded or err
            calibration, err = await _get(f"/og/api/hubs/{hub_id}/calibration-history")
            degraded = degraded or err
            location, _ = await _get(f"/og/api/fleet/hubs/{hub_id}")
            bank_id = location.get("bank_id") if isinstance(location, dict) else None
            bank_pq, err = await _get(f"/og/api/banks/{bank_id}/pq") if bank_id else (None, None)
            degraded = degraded or err
            summary = waveform.get("summary")
            ctx |= {
                "summary": summary_view(summary),
                "summary_stale_after_s": _SUMMARY_STALE_AFTER_S,
                "captures": waveform.get("raw_captures") or [],
                "bars": spectrum_bars(summary),
                "trend": spectrum_trend((spectrum or {}).get("points") or []),
                "asset": health if isinstance(health, dict) else None,
                "calibrations": (calibration or {}).get("attempts") or [],
                "bank_id": bank_id,
                "bank_pq": bank_pq if isinstance(bank_pq, dict) else None,
            }

    return templates.TemplateResponse(
        request,
        "pq.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "work_orders": orders,
            "open_count": sum(1 for o in orders if o.get("status") == "OPEN"),
            "degraded": degraded,
            "rendered_at": datetime.now(UTC).isoformat(),
            **ctx,
        },
    )


# -- operator actions: the API's two-step flows, relayed ---------------------------------------------------

_ACTIONS = {
    "capture": ("waveform-capture", "Confirm raw waveform capture", "Request capture"),
    "calibrate": ("calibrate", "Confirm remote calibration request", "Request calibration"),
}


def _result(request: Request, *, message: str, ok: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_partials/pq_action_result.html", {"message": message, "ok": ok}
    )


@router.post("/{hub_id}/{action}/propose", response_class=HTMLResponse)
async def propose_action(request: Request, hub_id: str, action: str, reason: str = Form(...)) -> HTMLResponse:
    """Step 1 of 2: the API stores a proposal and returns its summary; nothing is published or recorded."""
    _require_operator(request)
    if action not in _ACTIONS:
        return _result(request, message=f"unknown action {action!r}", ok=False)
    path, title, label = _ACTIONS[action]
    try:
        proposal = await post_json(
            f"/og/api/hubs/{hub_id}/{path}", {"reason": reason}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"pq-{action}-confirm-{proposal['proposal_id']}",
            title=title,
            proposal=proposal,
            confirm_url=f"{BASE_PATH}/pq/{hub_id}/{action}/{proposal['proposal_id']}/confirm",
            confirm_label=label,
            variant="danger",
            target="#pq-action-result",
        ),
    )


@router.post("/{hub_id}/{action}/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_action(request: Request, hub_id: str, action: str, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation. A capture is published to the hub; a calibration is recorded
    PENDING for the guardian's G-25 check (the API never signs)."""
    _require_operator(request)
    if action not in _ACTIONS:
        return _result(request, message=f"unknown action {action!r}", ok=False)
    path = _ACTIONS[action][0]
    try:
        result = await post_json(
            f"/og/api/hubs/{hub_id}/{path}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        if exc.status_code == 429:
            return _result(request, message=f"Rate limited: {exc.detail}", ok=False)
        if exc.status_code == 409:
            return _result(request, message=f"Refused: {exc.detail}", ok=False)
        if exc.status_code in (404, 410):
            return _result(request, message="The proposal expired or is unknown; propose again.", ok=False)
        return _result(request, message=str(exc), ok=False)
    if action == "capture":
        return _result(
            request,
            message=f"Capture requested ({result.get('request_id')}); it appears under raw captures.",
            ok=True,
        )
    return _result(
        request,
        message=f"Calibration {result.get('calibration_id')} recorded PENDING; awaiting the guardian's G-25 decision.",
        ok=True,
    )
