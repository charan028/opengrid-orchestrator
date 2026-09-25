"""View-model tests for the dispatch & commitments screen (02b S8 screen 3), maps to TS-10-* (UI).

No HTTP, no DB: every function under test is pure, given JSON fixtures shaped like the
`opengrid.api` responses documented in 02b S7.1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opengrid.ui.routes.dispatch import (
    commitment_lock_events_view,
    grants_and_substitutions_view,
    ledger_timeline_view,
    pipeline_view,
    plan_view,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_NOW = datetime(2026, 9, 25, 18, 10, tzinfo=UTC)


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_pipeline_view_groups_by_state_across_all_customers() -> None:
    obligations = _load("dispatch_obligations.json")

    view = pipeline_view(obligations, now=_NOW)

    assert view["total"] == 6
    assert view["customer_count"] == 5  # CUST-A appears twice (OFFERED + SELECTED)
    by_state = {column["state"]: column for column in view["columns"]}
    assert by_state["OFFERED"]["count"] == 1
    assert by_state["SELECTED"]["count"] == 1
    assert by_state["COMMITTED"]["count"] == 1
    assert by_state["DELIVERING"]["count"] == 1
    # FULFILLED and SHORTFALL share one terminal column.
    assert by_state["FULFILLED_OR_SHORTFALL"]["count"] == 2
    at_risk_ids = {item["obligation_id"] for item in by_state["DELIVERING"]["items"] if item["at_risk"]}
    assert at_risk_ids == {"OBL-1003"}


def test_pipeline_view_ignores_unknown_states() -> None:
    view = pipeline_view([{"obligation_id": "X", "state": "REJECTED"}], now=_NOW)

    assert view["total"] == 1
    assert all(column["count"] == 0 for column in view["columns"])


def test_ledger_timeline_view_builds_stacked_series_plus_headroom() -> None:
    timeline = _load("dispatch_ledger_timeline.json")

    view = ledger_timeline_view("BANK-0001", timeline["reservations"], timeline["bank_capacity_kw"], now=_NOW)

    assert view["bank_id"] == "BANK-0001"
    series_names = {s["name"] for s in view["chart_option"]["series"]}
    # OBL-1099's reservation is released and must be excluded from the ledger.
    assert series_names == {"OBL-1002", "OBL-1003", "free headroom"}
    assert view["obligation_count"] == 2
    intervals = view["chart_option"]["xAxis"]["data"]
    assert intervals == ["2026-09-25T18:00:00+00:00", "2026-09-25T18:15:00+00:00"]
    headroom_series = next(s for s in view["chart_option"]["series"] if s["name"] == "free headroom")
    # First interval: 40 + 8 = 48 committed of 500 kW bank capacity.
    assert headroom_series["data"][0] == 452.0


def test_plan_view_flags_lp_vs_rule_fallback() -> None:
    plan = _load("dispatch_plan.json")

    view = plan_view(plan)

    assert view["has_plan"] is True
    assert view["is_lp"] is True
    assert view["mode_label"] == "LP optimizer"
    assert view["solver_gap_pct"] == 0.4

    rule_plan = {**plan, "plan_mode": "RULE_FALLBACK"}
    assert plan_view(rule_plan)["mode_label"] == "Rule-based fallback"
    assert plan_view(rule_plan)["is_lp"] is False


def test_plan_view_handles_no_plan_yet() -> None:
    assert plan_view(None) == {"has_plan": False}


def test_grants_view_flags_substitution_when_bank_changes() -> None:
    timeline = _load("dispatch_ledger_timeline.json")

    rows = grants_and_substitutions_view(timeline["grants"])

    by_id = {row["grant_id"]: row for row in rows}
    assert by_id["GRANT-1"]["kind"] == "commitment"
    assert by_id["GRANT-2"]["kind"] == "substitution"  # OBL-1002 moved BANK-0001 -> BANK-0002
    assert by_id["GRANT-3"]["kind"] == "headroom"


def test_commitment_lock_events_view_filters_lock_reason_or_supersession() -> None:
    commitments = _load("dispatch_commitments.json")["commitments"]

    events = commitment_lock_events_view(commitments)

    assert [e["commitment_id"] for e in events] == ["COMMIT-2"]
    assert events[0]["reason_code"] == "R-COMMIT-LOCK-L1"
    assert events[0]["supersedes"] == "COMMIT-0"
