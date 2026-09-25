"""Billing & audit screen (02b S7.1, S8 screen 7): invoice lines (with CSV export), the trace
explorer, and chain verification.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.store import StoreProtocol
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api", tags=["billing"])

_INVOICE_CSV_HEADER = [
    "invoice_line_id",
    "contract_id",
    "obligation_id",
    "period_start",
    "period_end",
    "line_type",
    "quantity",
    "unit",
    "rate",
    "amount",
    "status",
]


@router.get("/billing/invoice-lines", response_model=None)
async def invoice_lines(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
    format: Annotated[Literal["json", "csv"], Query()] = "json",
) -> StreamingResponse | list[dict[str, Any]]:
    """Insert-only invoice lines; `format=csv` streams a download (02b S7.1)."""
    lines = await store.invoice_lines(t0=from_, t1=to)
    if format == "csv":
        return _as_csv(lines)
    return [line.model_dump(mode="json") for line in lines]


@router.get("/billing/performance")
async def performance(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
) -> list[dict[str, Any]]:
    """M&V performance per obligation for the period (02b S8 screen 7 "M&V performance summary"),
    kept as its own endpoint rather than folded into `invoice-lines` so that response shape stays
    stable for either consumer (billing rows vs. M&V rows are different tables/cardinalities)."""
    rows = await store.performance_summary(t0=from_, t1=to)
    return [row.model_dump(mode="json") for row in rows]


def _as_csv(lines: list[Any]) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_INVOICE_CSV_HEADER)
    for line in lines:
        writer.writerow([getattr(line, field) for field in _INVOICE_CSV_HEADER])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoice-lines.csv"},
    )


@router.get("/trace/events")
async def trace_events(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    event_class: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(le=2000, gt=0)] = 200,
) -> list[dict[str, Any]]:
    """Trace explorer query (02b S7.1, S8 screen 7)."""
    events = await store.trace_events(event_class=event_class, t0=from_, t1=to, limit=limit)
    return [e.model_dump(mode="json") for e in events]


@router.post("/trace/verify")
async def verify_trace(
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    stream_id: str,
    from_seq: int = 0,
) -> dict[str, Any]:
    """Runs `trace.verify(range)`, returns pass/fail + first broken link if any (02b S7.1)."""
    result = await trace_store.verify(stream_id, from_seq=from_seq)
    return {"ok": result.ok, "broken_at_seq": result.broken_at_seq, "reason": result.reason}
