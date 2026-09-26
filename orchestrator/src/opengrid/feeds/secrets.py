"""Credential wrapper for feed clients (BUILD.md S5a/S6: never a raw credential in logs or errors).

`Secret` holds a resolved credential; `repr`/`str` never show the value, and only `reveal()` does,
at the one place it goes on the wire. `mask_secrets` scrubs any revealed values out of text (e.g. an
exception message echoed back by an upstream server) before it is raised or logged.
"""

from __future__ import annotations

from collections.abc import Iterable

MASK = "***"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({MASK})"

    def __str__(self) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def mask_secrets(text: str, secrets: Iterable[Secret]) -> str:
    """Replace every non-empty revealed value in `text` with the mask, longest first."""
    values = sorted({s.reveal() for s in secrets if s.reveal()}, key=len, reverse=True)
    for value in values:
        text = text.replace(value, MASK)
    return text
