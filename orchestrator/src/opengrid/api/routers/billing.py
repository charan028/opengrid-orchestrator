"""Billing & audit screen (02b S7.1, S8 screen 7): invoice lines (with CSV export and an embedded M&V
`performance` array), the trace explorer, and chain verification.

Response shapes below match what `opengrid.ui.routes.billing_audit` (ui-b) already consumes -- its own
module docstring records the agreed contract: `GET .../invoice-lines` embeds `"performance"` alongside
`"lines"`, `GET .../trace/events` filters on `class` (not `event_class`), and `POST .../trace/verify`
takes a JSON body and returns `{"passed", "checked", "first_broken"}`.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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
) -> StreamingResponse | dict[str, Any]:
    """`format=csv` streams the invoice-line CSV unchanged (02b S7.1); otherwise
    `{"lines": [...], "performance": [...]}` -- the M&V performance rows for the same period, embedded
    per ui-b's `billing_audit.py` contract rather than a separate endpoint."""
    lines = await store.invoice_lines(t0=from_, t1=to)
    if format == "csv":
        return _as_csv(lines)
    performance = await store.performance_summary(t0=from_, t1=to)
    return {
        "lines": [line.model_dump(mode="json") for line in lines],
        "performance": [row.model_dump(mode="json") for row in performance],
    }


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
    class_: Annotated[str | None, Query(alias="class")] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(le=2000, gt=0)] = 200,
) -> list[dict[str, Any]]:
    """Trace explorer query (02b S7.1, S8 screen 7); `class` is the query param name (matching
    `opengrid.ui`'s call), mapped internally to `event_class`."""
    events = await store.trace_events(event_class=class_, t0=from_, t1=to, limit=limit)
    return [e.model_dump(mode="json") for e in events]


class TraceVerifyRequest(BaseModel):
    """`class`/`from`/`to` are accepted for forward compatibility with `opengrid.ui`'s call but not
    used to filter what gets verified: a hash chain is verified as a whole stream from its start, so a
    time/class-filtered subset can't be checked for broken links without producing false failures.
    `stream_id` verifies one stream; omitted, every known stream is verified."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    class_: str | None = Field(default=None, alias="class")
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    stream_id: str | None = None


@router.post("/trace/verify")
async def verify_trace(
    body: TraceVerifyRequest,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """Runs `trace.verify()` over `stream_id` (or every known stream) and returns pass/fail plus the
    first broken link, if any (02b S7.1)."""
    stream_ids = [body.stream_id] if body.stream_id else await store.list_stream_ids()
    checked = 0
    first_broken: dict[str, Any] | None = None
    passed = True
    for stream_id in stream_ids:
        result = await trace_store.verify(stream_id, from_seq=0)
        checked += 1
        if not result.ok:
            passed = False
            if first_broken is None:
                first_broken = {
                    "stream_id": stream_id,
                    "seq": result.broken_at_seq,
                    "reason": result.reason,
                }
    return {"passed": passed, "checked": checked, "first_broken": first_broken}
