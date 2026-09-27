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
