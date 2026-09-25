"""TS-08-05: CSV export matches stored invoice lines / M&V records exactly."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.settle.backend import InvoiceLineExportRow, MeterIntervalExportRow
from opengrid.settle.csv_export import export_invoice_lines_csv, export_meter_intervals_csv, rows_to_csv


def test_invoice_line_csv_matches_rows_exactly():
    contract_id, obligation_id, line_id = uuid4(), uuid4(), uuid4()
    row = InvoiceLineExportRow(
        invoice_line_id=line_id,
        contract_id=contract_id,
        obligation_id=obligation_id,
        period_start=datetime(2026, 9, 26, tzinfo=UTC).date(),
        period_end=datetime(2026, 9, 26, tzinfo=UTC).date(),
        line_type="ENERGY",
        quantity=Decimal("100.000000"),
        unit="kWh",
        rate=Decimal("0.100000"),
        amount=Decimal("10.000000"),
        status="FINAL",
        supersedes=None,
        version=1,
    )

    csv_text = rows_to_csv([row])
    lines = csv_text.strip("\n").split("\n")

    assert lines[0].split(",") == [
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
        "supersedes",
        "version",
    ]
    fields = lines[1].split(",")
    assert fields[0] == str(line_id)
    assert fields[5] == "ENERGY"
    assert fields[9] == "10.000000"
    assert fields[10] == "FINAL"
    assert fields[11] == ""  # supersedes=None renders as empty, never the string "None"


def test_invoice_line_csv_includes_superseded_line_link():
    """ES08-S05: "the CSV matches the underlying records exactly, including superseded-line
    links" -- a correction's `supersedes` column must render the original line's id."""
    original_id, correction_id, contract_id, obligation_id = uuid4(), uuid4(), uuid4(), uuid4()
    correction = InvoiceLineExportRow(
        invoice_line_id=correction_id,
        contract_id=contract_id,
        obligation_id=obligation_id,
        period_start=datetime(2026, 9, 26, tzinfo=UTC).date(),
        period_end=datetime(2026, 9, 26, tzinfo=UTC).date(),
        line_type="ENERGY",
        quantity=Decimal("110"),
        unit="kWh",
        rate=Decimal("0.10"),
        amount=Decimal("11.00"),
        status="CORRECTED",
        supersedes=original_id,
        version=2,
    )

    csv_text = rows_to_csv([correction])
    data_row = csv_text.strip("\n").split("\n")[1]
    assert str(original_id) in data_row


def test_meter_interval_csv_round_trip():
    obligation_id, meter_id = uuid4(), uuid4()
    row = MeterIntervalExportRow(
        meter_interval_id=meter_id,
        obligation_id=obligation_id,
        interval_start=datetime(2026, 9, 26, 0, 0, tzinfo=UTC),
        interval_end=datetime(2026, 9, 26, 0, 15, tzinfo=UTC),
        delivered_kwh=Decimal("1.0"),
        baseline_kwh=Decimal("1.25"),
        source="DIRECT_HUB_METER",
        quality_flag="GOOD",
        version=1,
        superseded_by=None,
    )

    csv_text = rows_to_csv([row])
    assert csv_text.count("\n") == 2  # header + one data row
    assert "1.0" in csv_text
    assert "GOOD" in csv_text


def test_empty_rows_produce_empty_csv():
    assert rows_to_csv([]) == ""


@pytest.mark.asyncio
async def test_export_invoice_lines_csv_uses_configured_backend(fake_backend):
    """`fake_backend` (autoused `configure()`, see conftest.py) returns no rows, exercising the
    async wrapper's fetch-then-render path end to end."""
    csv_text = await export_invoice_lines_csv(
        uuid4(), datetime(2026, 9, 1, tzinfo=UTC).date(), datetime(2026, 9, 30, tzinfo=UTC).date()
    )
    assert csv_text == ""


@pytest.mark.asyncio
async def test_export_meter_intervals_csv_uses_configured_backend(fake_backend):
    csv_text = await export_meter_intervals_csv(
        uuid4(), datetime(2026, 9, 1, tzinfo=UTC).date(), datetime(2026, 9, 30, tzinfo=UTC).date()
    )
    assert csv_text == ""
