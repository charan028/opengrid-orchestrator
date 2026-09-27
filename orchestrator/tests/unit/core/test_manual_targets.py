"""`opengrid.core.manual_targets`: the one MANUAL_TARGET parser shared by og-api and og-engine."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from opengrid.core import manual_targets as mt

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def _row(hubs, kw, *, issued=NOW, minutes=10, **extra):
    payload = {
        "hub_ids": hubs,
        "p_kw_command": kw,
        "sign_convention": mt.SIGN_CONVENTION,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=minutes)).isoformat(),
        **extra,
    }
    return (uuid4(), payload, issued)


def test_newest_wins_expired_and_malformed_are_ignored_and_legacy_name_reads() -> None:
    rows = [
        _row(["h1", "h2"], -5.0, issued=NOW - timedelta(minutes=2)),
        _row(["h1"], 4.0, issued=NOW - timedelta(minutes=1)),
        _row(["h3"], -1.0, issued=NOW - timedelta(minutes=20)),  # expired
        (uuid4(), {"hub_ids": ["h4"]}, NOW),  # malformed
        (
            uuid4(),
            {"hub_ids": ["h5"], "p_kw_target": -2.0, "expires_at": (NOW + timedelta(minutes=1)).isoformat()},
            NOW,
        ),
    ]
    assert {h: t.p_kw_target for h, t in mt.parse_targets(rows, NOW).items()} == {
        "h1": 4.0,
        "h2": -5.0,
        "h5": -2.0,
    }


def test_a_foreign_sign_convention_is_refused() -> None:
    assert mt.parse_targets([_row(["h1"], 5.0, sign_convention="+discharge/-charge")], NOW) == {}


def test_a_cancel_ends_only_the_named_target_where_it_is_still_newest() -> None:
    first = _row(["h1", "h2"], -5.0, issued=NOW - timedelta(minutes=1))
    newer = _row(["h2"], -2.0, issued=NOW - timedelta(seconds=30))
    cancel = (
        uuid4(),
        {
            "hub_ids": ["h1", "h2"],
            "cancels": str(first[0]),
            "issued_at": NOW.isoformat(),
            "expires_at": NOW.isoformat(),
        },
        NOW,
    )
    targets = mt.parse_targets([first, newer, cancel], NOW)
    assert set(targets) == {"h2"} and targets["h2"].p_kw_target == -2.0


def test_the_sql_literal_matches_the_constants() -> None:
    assert f"event_class = '{mt.MANUAL_TARGET_EVENT}'" in mt.MANUAL_TARGET_ROWS_SQL
    assert f"interval '{mt.LIVE_WINDOW_HOURS} hours'" in mt.MANUAL_TARGET_ROWS_SQL


def test_core_module_is_pure_no_engine_or_io_imports() -> None:
    tree = ast.parse(Path(mt.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not any(m.startswith(("opengrid.engine", "opengrid.allocator", "psycopg")) for m in imported)


def test_the_engine_re_imports_the_core_implementation() -> None:
    from opengrid.engine import manual

    assert manual.parse_targets is mt.parse_targets
    assert manual.ManualTarget is mt.ManualTarget
    assert manual.SIGN_CONVENTION == mt.SIGN_CONVENTION


# --- effective_targets: the one status rule (engine, API list, guardian) ------------------------------------


def _states(rows, stops=(), now=NOW):
    return mt.effective_targets(
        rows, stops, now, bank_of_hub=lambda h: "b1", zone_of_bank=lambda b: "LZ_NORTH"
    )


def test_effective_targets_reports_every_status() -> None:
    op_target = _row(["h1"], -5.0, issued=NOW - timedelta(minutes=2))
    cancel = (
        uuid4(),
        {
            "hub_ids": ["h1"],
            "cancels": str(op_target[0]),
            "issued_at": NOW.isoformat(),
            "expires_at": NOW.isoformat(),
        },
        NOW,
    )
    late_target = _row(["h2"], -5.0, issued=NOW - timedelta(minutes=2))
    late_cancel = (
        uuid4(),
        {
            "hub_ids": ["h2"],
            "cancels": str(late_target[0]),
            "cancel_kind": "LATE_RECORD",
            "issued_at": NOW.isoformat(),
            "expires_at": NOW.isoformat(),
        },
        NOW,
    )
    expired = _row(["h3"], -1.0, issued=NOW - timedelta(minutes=20))
    active = _row(["h4"], 3.0, issued=NOW - timedelta(minutes=1))
    got = _states([op_target, late_target, expired, active, cancel, late_cancel])
    assert got["h1"].status is mt.TargetStatus.CANCELLED_BY_OPERATOR and got["h1"].cancelled_by == str(
        cancel[0]
    )
    assert got["h2"].status is mt.TargetStatus.CANCELLED_LATE_RECORD
    assert got["h3"].status is mt.TargetStatus.EXPIRED
    assert got["h4"].status is mt.TargetStatus.ACTIVE
    assert set(mt.active_targets(got)) == {"h4"}


def test_a_stop_cancelled_target_is_never_active_and_names_the_stop() -> None:
    target = _row(["h1"], -5.0, issued=NOW - timedelta(minutes=2))
    stop = [
        (uuid4(), "BANK", "b1", "ENGAGE", NOW - timedelta(minutes=1)),
        (uuid4(), "BANK", "b1", "RELEASE", NOW),
    ]
    got = _states([target], stop)
    assert got["h1"].status is mt.TargetStatus.CANCELLED_BY_SAFE_STOP
    assert got["h1"].stop_event_id == str(stop[0][0])
    assert mt.active_targets(got) == {}
    # A newer target after the stop is released is active again.
    newer = _row(["h1"], -2.0, issued=NOW + timedelta(seconds=1))
    assert (
        _states([target, newer], stop, now=NOW + timedelta(seconds=2))["h1"].status is mt.TargetStatus.ACTIVE
    )


def test_stop_rows_sql_selects_the_id_first() -> None:
    assert mt.STOP_EVENT_ROWS_SQL.split("FROM")[0].split()[1].rstrip(",").split(".")[-1] == "stop_event_id"


def test_a_stop_engaged_days_ago_and_never_released_still_cancels_a_target() -> None:
    """The stop read keeps each scope's latest event whatever its age (effective_targets' in-force rule)."""
    target = _row(["h1"], -5.0, issued=NOW - timedelta(minutes=1))
    old_engage = [(uuid4(), "BANK", "b1", "ENGAGE", NOW - timedelta(days=3))]
    got = _states([target], old_engage)
    assert got["h1"].status is mt.TargetStatus.CANCELLED_BY_SAFE_STOP
    sql = " ".join(mt.STOP_EVENT_ROWS_SQL.split())
    assert "OR s.created_at = ( SELECT max(l.created_at) FROM og.stop_event l" in sql
    assert "l.scope_kind = s.scope_kind AND l.scope_ref = s.scope_ref" in sql
