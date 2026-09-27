"""View-model tests for the profitability screen (02b S8 screen 6), maps to TS-10-* (UI).

No HTTP, no DB: every function under test is pure, given a JSON fixture shaped like
`GET /og/api/profitability/summary` (02b S7.1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opengrid.ui.routes.profitability import (
    forgone_upside_view,
    lp_vs_baseline_view,
    profitability_table_view,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_profitability_table_view_sums_totals() -> None:
    rows = _load("profitability_summary.json")["rows"]

    view = profitability_table_view(rows)

    assert view["row_count"] == 2
    assert view["totals"]["revenue"] == 580.0
    assert view["totals"]["net_value"] == 410.0
    assert view["totals"]["forgone_upside"] == 40.0
    assert view["rows"][1]["rule_baseline_value"] is None


def test_lp_vs_baseline_view_only_includes_rows_with_a_baseline() -> None:
    rows = _load("profitability_summary.json")["rows"]

    view = lp_vs_baseline_view(rows)

    assert view["compared_count"] == 1
    assert view["chart_option"]["xAxis"]["data"] == ["OBL-2001"]
    series_by_name = {s["name"]: s for s in view["chart_option"]["series"]}
    assert series_by_name["MILP net value"]["data"] == [365.0]
    assert series_by_name["Rule baseline"]["data"] == [300.0]


def test_forgone_upside_view_totals_positive_upside_only() -> None:
    rows = _load("profitability_summary.json")["rows"]

    view = forgone_upside_view(rows)

    assert view["total_forgone_upside"] == 40.0
    assert view["obligation_count"] == 1
