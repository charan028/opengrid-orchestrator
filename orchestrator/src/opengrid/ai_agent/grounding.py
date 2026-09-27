"""Numbers in a model's explanation must come from the evidence it was given. Owner: ui-a/ai.

The explanation prompt already says "never invent a number", but a prompt is a request, not a control.
This check is the control: every number in the prose must match a number in the evidence (the console
snapshot and any fleet tool result) or in the operator's own question, within rounding. Prose that
cites anything else is withheld and the console's own answer is shown instead.

Small whole numbers (0-10) are exempt: they are counting words ("two reasons", "3 of them") far more
often than data, and a wrong one cannot pass for a measured total.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_SMALL = 10.0


def _numbers_in_text(text: str) -> list[float]:
    out: list[float] = []
    for match in _NUMBER.finditer(text):
        try:
            out.append(float(match.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def numbers_in(value: Any) -> set[float]:
    """Every number in a JSON-like value: numeric leaves, and numbers written inside strings."""
    found: set[float] = set()
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, int | float):
            found.add(float(item))
        elif isinstance(item, str):
            found.update(_numbers_in_text(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return found


def _matches(value: float, allowed: Iterable[float]) -> bool:
    return any(abs(value - known) <= max(0.5, 0.005 * abs(known)) for known in allowed)


def ungrounded_numbers(text: str, *sources: Any) -> list[str]:
    """The numbers in `text` that no source supports (empty when the prose is grounded)."""
    allowed: set[float] = set()
    for source in sources:
        allowed |= numbers_in(source)
    missing: list[str] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if value.is_integer() and 0 <= value <= _SMALL:
            continue
        if not _matches(value, allowed):
            missing.append(raw)
    return missing
