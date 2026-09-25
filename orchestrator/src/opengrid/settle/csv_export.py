"""CSV export of invoice lines and M&V (meter-interval) records (02a S7.3, ES08-S05).

`rows_to_csv` is pure (given rows already fetched, render exact CSV text) so TS-08-05 ("export CSV,
diff against DB query, 100% row/field match") can be tested without a database. The `export_*`
wrappers add the DB fetch via the module-level backend configured by `opengrid.settle.configure`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, fields
from datetime import UTC, date, datetime
from uuid import UUID

from opengrid.settle import _require_backend
from opengrid.settle.backend import InvoiceLineExportRow, MeterIntervalExportRow


def rows_to_csv[RowT: (InvoiceLineExportRow, MeterIntervalExportRow)](rows: list[RowT]) -> str:
    """Render `rows` (either export-row dataclass) as CSV text, column order = dataclass field
    order, every value stringified exactly as Python renders it (so a diff against a DB query using
    the same string conversion matches 100%, per TS-08-05)."""
    buffer = io.StringIO()
    if not rows:
        return ""
    fieldnames = [f.name for f in fields(rows[0])]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else str(v)) for k, v in asdict(row).items()})
    return buffer.getvalue()


async def export_invoice_lines_csv(contract_id: UUID, period_start: date, period_end: date) -> str:
    """CSV of every invoice-line version for `contract_id` in `[period_start, period_end]`."""
    backend = _require_backend()
    rows = await backend.fetch_invoice_lines_for_period(contract_id, period_start, period_end)
    return rows_to_csv(rows)


async def export_meter_intervals_csv(obligation_id: UUID, period_start: date, period_end: date) -> str:
    """CSV of every meter-interval version for `obligation_id` in `[period_start, period_end)`."""
    backend = _require_backend()
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    rows = await backend.fetch_meter_intervals_for_period(obligation_id, start_dt, end_dt)
    return rows_to_csv(rows)
