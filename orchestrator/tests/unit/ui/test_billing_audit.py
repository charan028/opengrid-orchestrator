"""View-model tests for the billing & audit screen (02b S8 screen 7), maps to TS-10-* (UI).

No HTTP, no DB: every function under test is pure, given JSON fixtures shaped like the
`opengrid.api` responses documented in 02b S7.1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opengrid.ui.routes.billing_audit import (
    chain_verify_result_view,
    invoice_table_view,
    mnv_performance_view,
    trace_explorer_view,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_invoice_table_view_sums_totals_by_line_type() -> None:
    lines = _load("billing_invoice_lines.json")["lines"]

    view = invoice_table_view(lines)

    assert view["row_count"] == 2
    assert view["total_amount"] == 65.0
    assert view["totals_by_type"] == {"CAPACITY_PAYMENT": 50.0, "LD_PENALTY": 15.0}
    assert view["rows"][1]["quantity"] is None


def test_mnv_performance_view_computes_average_and_pass_rate() -> None:
    performance = _load("billing_invoice_lines.json")["performance"]

    view = mnv_performance_view(performance)

    assert view["avg_compliance_pct"] == 89.25
    assert view["pass_rate_pct"] == 50.0
    assert len(view["rows"]) == 2


def test_mnv_performance_view_handles_no_data() -> None:
    assert mnv_performance_view([]) == {"rows": [], "avg_compliance_pct": None, "pass_rate_pct": None}


def test_trace_explorer_view_orders_most_recent_first() -> None:
    events = _load("billing_trace_events.json")["events"]

    rows = trace_explorer_view(events)

    assert [row["trace_id"] for row in rows] == ["TRACE-2", "TRACE-1"]
    assert rows[0]["reason_codes"] == ["R-COMMIT-LOCK-L1"]


def test_chain_verify_result_view_reports_first_broken_link() -> None:
    result = _load("billing_trace_verify_fail.json")

    view = chain_verify_result_view(result)

    assert view["passed"] is False
    assert view["checked"] == 128
    assert view["first_broken"]["trace_id"] == "TRACE-77"


def test_chain_verify_result_view_pass() -> None:
    view = chain_verify_result_view({"passed": True, "checked": 50, "first_broken": None})

    assert view["passed"] is True
    assert view["first_broken"] is None
