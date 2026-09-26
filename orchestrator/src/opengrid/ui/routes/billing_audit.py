"""Billing & audit screen (02b S8 screen 7, UI-MNV subset). Owner: ui-b (BUILD.md S4).

Renders `/og/billing`: invoice lines with filters and CSV export, an M&V performance summary, and a
trace explorer with a chain-verify button and its pass/fail result. Server-rendered first paint comes
from `opengrid.ui.api_client.get_json` (02b S7.1); the CSV export and the chain-verify POST are relayed
to `opengrid.api` through the same shared `opengrid.ui.api_client` module (`get_bytes`/`post_json`) --
this screen used to keep a private `_post_json` copy plus a second bare `httpx.AsyncClient` for the CSV
relay; both now go through the one client every other screen already uses (BUILD.md code-review round).
View-model functions below are pure and unit-tested against JSON fixtures, no HTTP or DB involved.

Assumption pending the `api` agent's implementation: `GET /og/api/billing/invoice-lines` is expected to
embed an optional `"performance"` array (`opengrid.core.models.engine.Performance` rows) alongside
`"lines"` for the M&V summary, since 02b S7.1 lists no separate performance endpoint. If the API agent
instead ships a dedicated endpoint, `mnv_performance_view` below is unaffected -- only
`billing_page`'s fetch needs a one-line change.

Open issue for the `api`/`trace` agents: the trace explorer (below) cannot show a per-event age
indicator (BUILD.md code-review round item 5) because `opengrid.core.models.engine.TraceRow` -- what
`GET /og/api/trace/events` serializes -- carries no timestamp field at all (only `trace_id`, decision/
event class, `stream_id`, `seq`, `reason_codes`, `hash`, `prev_hash`). Adding one (e.g. `created_at`,
which `opengrid.trace.store.TraceRecordRef` already computes but does not expose on the row) is a
prerequisite; fabricating an age from `seq` alone would be misleading.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json, post_json
from opengrid.ui.api_client import get_bytes as api_get_bytes
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.settlement import SETTLEMENT_VIEW_PATH, filter_options, invoice_view, last_updated
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing")

_INVOICE_LINES_PATH = "/og/api/billing/invoice-lines"
_TRACE_EVENTS_PATH = "/og/api/trace/events"
_TRACE_VERIFY_PATH = "/og/api/trace/verify"
_DEFAULT_PERIOD_DAYS = 30
_MARKET_TZ = ZoneInfo("America/Chicago")


def api_date(value: str | None, default: datetime) -> str:
    """`GET /og/api/billing/invoice-lines` requires `from`/`to` as ISO dates (422 otherwise, found on the
    first live run): default a blank filter to `default`, and cut a `datetime-local` form value
    (`2026-09-25T10:00`) down to its date part."""
    return (value or default.isoformat())[:10]


def export_period(from_: str | None, to: str | None, *, now: datetime | None = None) -> tuple[str, str]:
    """The CSV export's `from`/`to` as plain ISO dates, which `GET .../invoice-lines` requires (a blank
    field or a `datetime-local` value is a 422): blank defaults to the first of the current month and
    TOMORROW, in ERCOT local time; a datetime is cut to its date.

    Why tomorrow: the API keeps lines with `period_end <= to`, and a date compares as midnight, so
    `to = today` would drop today's lines. Day-boundary edge: the default is the Chicago date, while the
    API compares the stored period dates as given (settle writes them as dates), so between 19:00 and
    24:00 CT (already the next UTC day) a line dated by UTC may fall one day later -- the +1 day margin
    covers that too."""
    today = (now or datetime.now(UTC)).astimezone(_MARKET_TZ).date()
    start = (from_ or "").strip()[:10] or today.replace(day=1).isoformat()
    end = (to or "").strip()[:10] or (today + timedelta(days=1)).isoformat()
    return start, end


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
    customer: str | None = Query(default=None),
    contract: str | None = Query(default=None),
    obligation: str | None = Query(default=None),
) -> HTMLResponse:
    """Billing & audit screen (`/og/billing`, viewer role read-only, on-demand per 02b S8). Invoice lines
    come from the shared settlement view (`opengrid.ui.settlement`, same contract labels as
    Profitability); M&V performance still rides on `GET .../invoice-lines`."""
    now = datetime.now(tz=UTC)
    invoice_params = {
        "from": api_date(from_, now - timedelta(days=_DEFAULT_PERIOD_DAYS)),
        "to": api_date(to, now + timedelta(days=1)),
    }
    trace_params = {k: v for k, v in {"class": class_, "from": from_, "to": to}.items() if v}
    degraded: str | None = None
    lines: list[dict[str, Any]] = []
    performance: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    view: dict[str, Any] = {}

    try:
        raw_view = await get_json(SETTLEMENT_VIEW_PATH, params=invoice_params)
        view = raw_view if isinstance(raw_view, dict) else {}
    except ApiUnavailable as exc:
        logger.warning("billing: %s unavailable: %s", SETTLEMENT_VIEW_PATH, exc)
        degraded = str(exc)

    try:
        billing = await get_json(_INVOICE_LINES_PATH, params=invoice_params)
        lines = billing.get("lines", billing) if isinstance(billing, dict) else billing or []
        performance = billing.get("performance", []) if isinstance(billing, dict) else []
    except ApiUnavailable as exc:
        logger.warning("billing: %s unavailable: %s", _INVOICE_LINES_PATH, exc)
        degraded = degraded or str(exc)

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
            "customer": customer,
            "contract": contract,
            "obligation": obligation,
            "options": filter_options(view),
            "invoices": invoice_view(view, customer=customer, contract=contract, obligation=obligation),
            "legacy_line_count": len(lines) if isinstance(lines, list) else 0,
            "last_invoiced_at": last_updated(view, "invoice_line"),
            "last_metered_at": last_updated(view, "meter_interval"),
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
    start, end = export_period(from_, to)
    params = {"from": start, "to": end, "format": "csv"}
    try:
        content = await api_get_bytes(_INVOICE_LINES_PATH, params=params)
    except ApiUnavailable as exc:
        logger.warning("billing: CSV export failed: %s", exc)
        return Response(
            content=f"The invoice-line export for {start} to {end} could not be produced: {exc.detail or exc}\n",
            media_type="text/plain; charset=utf-8",
            status_code=exc.status_code if exc.status_code in (400, 401, 403, 404, 422) else 502,
        )
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=invoice_lines_{start}_{end}.csv"},
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
        result = await post_json(
            _TRACE_VERIFY_PATH, {"class": class_, "from": from_, "to": to}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        result = {"passed": False, "checked": 0, "first_broken": {"error": str(exc)}}
    return templates.TemplateResponse(
        request, "billing_audit_verify_result.html", {"result": chain_verify_result_view(result)}
    )
