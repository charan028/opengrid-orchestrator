"""Billing & audit screen (02b S8 screen 7, UI-MNV subset). Owner: ui-b (BUILD.md S4).

Renders `/og/billing`: invoice lines with filters and CSV export, an M&V performance summary, and a
trace explorer with a chain-verify button and its pass/fail result. Server-rendered first paint comes
from `opengrid.ui.api_client.get_json` (02b S7.1); the CSV export and the chain-verify POST are relayed
to `opengrid.api` directly (the API owns CSV formatting and `trace.verify`). View-model functions below
are pure and unit-tested against JSON fixtures, no HTTP or DB involved.

Assumption pending the `api` agent's implementation: `GET /og/api/billing/invoice-lines` is expected to
embed an optional `"performance"` array (`opengrid.core.models.engine.Performance` rows) alongside
`"lines"` for the M&V summary, since 02b S7.1 lists no separate performance endpoint. If the API agent
instead ships a dedicated endpoint, `mnv_performance_view` below is unaffected -- only
`billing_page`'s fetch needs a one-line change.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, api_base_url, get_json
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing")

_POST_TIMEOUT_S = 5.0
_INVOICE_LINES_PATH = "/og/api/billing/invoice-lines"
_TRACE_EVENTS_PATH = "/og/api/trace/events"
_TRACE_VERIFY_PATH = "/og/api/trace/verify"


async def _post_json(path: str, payload: dict[str, Any]) -> Any:
    """POST an `opengrid.api` endpoint (02b S7.1); `opengrid.ui.api_client` only covers GET, so the one
    mutating call this screen needs (`trace/verify`, itself read-only in effect) is made the same way,
    against the same base URL, with the same `ApiUnavailable` contract."""
    url = f"{api_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"POST {path} failed: {exc}") from exc


def invoice_table_view(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Invoice-line table with totals by line type (02b S8 screen 7)."""
    rows: list[dict[str, Any]] = []
    totals_by_type: dict[str, float] = {}
    total_amount = 0.0
    for line in lines:
        amount = float(line["amount"])
        total_amount += amount
        totals_by_type[line["line_type"]] = totals_by_type.get(line["line_type"], 0.0) + amount
        rows.append(
            {
                "invoice_line_id": line["invoice_line_id"],
                "contract_id": line["contract_id"],
                "obligation_id": line["obligation_id"],
                "period_start": line["period_start"],
                "period_end": line["period_end"],
                "line_type": line["line_type"],
                "quantity": line.get("quantity"),
                "unit": line.get("unit"),
                "rate": line.get("rate"),
                "amount": amount,
                "status": line.get("status", "PROVISIONAL"),
            }
        )
    return {
        "rows": rows,
        "total_amount": total_amount,
        "totals_by_type": totals_by_type,
        "row_count": len(rows),
    }


def mnv_performance_view(performance: list[dict[str, Any]]) -> dict[str, Any]:
    """M&V performance summary: average compliance and pass rate against threshold (02b S8 screen 7)."""
    if not performance:
        return {"rows": [], "avg_compliance_pct": None, "pass_rate_pct": None}
    rows = [
        {
            "obligation_id": row["obligation_id"],
            "interval_start": row["interval_start"],
            "interval_end": row["interval_end"],
            "compliance_pct": float(row["compliance_pct"]),
            "availability_pct": float(row["availability_pct"])
            if row.get("availability_pct") is not None
            else None,
            "passed_threshold": bool(row["passed_threshold"]),
        }
        for row in performance
    ]
    avg_compliance_pct = sum(row["compliance_pct"] for row in rows) / len(rows)
    pass_rate_pct = 100.0 * sum(1 for row in rows if row["passed_threshold"]) / len(rows)
    return {"rows": rows, "avg_compliance_pct": avg_compliance_pct, "pass_rate_pct": pass_rate_pct}


def trace_explorer_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trace explorer rows, most recent first (02b S8 screen 7)."""
    ordered = sorted(events, key=lambda event: event.get("seq", 0), reverse=True)
    return [
        {
            "trace_id": event["trace_id"],
            "decision_type": event.get("decision_type"),
            "event_class": event.get("event_class"),
            "stream_id": event.get("stream_id"),
            "seq": event.get("seq"),
            "reason_codes": event.get("reason_codes") or [],
            "hash": event.get("hash"),
            "prev_hash": event.get("prev_hash"),
        }
        for event in ordered
    ]


def chain_verify_result_view(result: dict[str, Any]) -> dict[str, Any]:
    """Chain-verify result: pass/fail and the first broken link, if any (`POST /trace/verify`, 02b
    S7.1/S8 screen 7)."""
    return {
        "passed": bool(result.get("passed")),
        "checked": result.get("checked", 0),
        "first_broken": result.get("first_broken"),
    }


def _to_trace_table_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render a `trace_explorer_view` row into a `data_table.html`-ready dict (joins the reason-code
    list into one display string)."""
    return {**row, "reason_codes_joined": ", ".join(row["reason_codes"])}


@router.get("", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    class_: str | None = Query(default=None, alias="class"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> HTMLResponse:
    """Billing & audit screen (`/og/billing`, viewer role read-only, on-demand per 02b S8)."""
    now = datetime.now(tz=UTC)
    invoice_params = {k: v for k, v in {"from": from_, "to": to}.items() if v}
    trace_params = {k: v for k, v in {"class": class_, "from": from_, "to": to}.items() if v}
    degraded: str | None = None
    lines: list[dict[str, Any]] = []
    performance: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    try:
        billing = await get_json(_INVOICE_LINES_PATH, params=invoice_params)
        lines = billing.get("lines", billing) if isinstance(billing, dict) else billing or []
        performance = billing.get("performance", []) if isinstance(billing, dict) else []
    except ApiUnavailable as exc:
        logger.warning("billing: %s unavailable: %s", _INVOICE_LINES_PATH, exc)
        degraded = str(exc)

    try:
        trace = await get_json(_TRACE_EVENTS_PATH, params=trace_params)
        events = trace.get("events", trace) if isinstance(trace, dict) else trace or []
    except ApiUnavailable as exc:
        logger.warning("billing: %s unavailable: %s", _TRACE_EVENTS_PATH, exc)
        degraded = degraded or str(exc)

    return templates.TemplateResponse(
        request,
        "billing_audit.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "class_": class_,
            "from_": from_,
            "to": to,
            "invoices": invoice_table_view(lines if isinstance(lines, list) else []),
            "mnv": mnv_performance_view(performance if isinstance(performance, list) else []),
            "trace_events": [
                _to_trace_table_row(row)
                for row in trace_explorer_view(events if isinstance(events, list) else [])
            ],
            "generated_at": now.isoformat(),
            "degraded": degraded,
        },
    )


@router.get("/invoice-lines/export.csv")
async def export_invoice_lines_csv(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> Response:
    """Stream the CSV export by relaying `GET .../invoice-lines?format=csv` from `opengrid.api`
    unchanged (the API owns CSV formatting; the UI only adds the download headers)."""
    params = {k: v for k, v in {"from": from_, "to": to, "format": "csv"}.items() if v}
    url = f"{api_base_url()}{_INVOICE_LINES_PATH}"
    async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
    return Response(
        content=response.content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoice_lines.csv"},
    )


@router.post("/trace/verify", response_class=HTMLResponse)
async def run_chain_verify(
    request: Request,
    class_: str | None = Query(default=None, alias="class"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> HTMLResponse:
    """HTMX partial: runs the chain-verify button, returns the pass/fail fragment (02b S7.1/S8)."""
    try:
        result = await _post_json(_TRACE_VERIFY_PATH, {"class": class_, "from": from_, "to": to})
    except ApiUnavailable as exc:
        result = {"passed": False, "checked": 0, "first_broken": {"error": str(exc)}}
    return templates.TemplateResponse(
        request, "billing_audit_verify_result.html", {"result": chain_verify_result_view(result)}
    )
