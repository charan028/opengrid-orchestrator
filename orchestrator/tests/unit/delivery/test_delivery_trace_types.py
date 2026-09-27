"""Every trace decision_type the delivery code writes is allowed by og.trace's CHECK (the latest migration
defining `trace_decision_type_check`). Prod rc3 quarantined the first DELIVERY_RECORD row because 0041's
list lacked it; this catches such a gap without a database."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ORCH = Path(__file__).resolve().parents[3]
MIGRATIONS = ORCH / "migrations"
_CHECK = re.compile(r"ADD CONSTRAINT trace_decision_type_check CHECK \(decision_type IN\s*\((.*?)\)\)", re.S)


def _allowed_decision_types() -> set[str]:
    latest: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for match in _CHECK.finditer(path.read_text(encoding="utf-8")):
            latest = set(re.findall(r"'([A-Z_]+)'", match.group(1)))
    return latest


def _decision_types_written(files: list[Path]) -> set[str]:
    """The literal second argument of every `.append(stream, decision_type, event_class, ...)` call."""
    found: set[str] = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                found.add(node.args[1].value)
    return found


def test_the_delivery_trace_decision_types_are_allowed_by_the_trace_check() -> None:
    src = ORCH / "src" / "opengrid"
    written = _decision_types_written(
        [*sorted((src / "delivery").glob("*.py")), src / "api" / "routers" / "delivery.py"]
    )

    assert {"DELIVERY_RECORD", "ALERT", "OPERATOR_ACTION"} <= written
    assert written <= _allowed_decision_types(), written - _allowed_decision_types()
