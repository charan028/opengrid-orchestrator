"""A tiny `SecretStr`-like wrapper so credentials never render in a traceback, a log record, or a
pytest failure's local-variable dump (BUILD.md S5a "no secrets in logs"; S6 "never print, log or
commit secret values").

Feeds resolves every credential (`opengrid.platform.config.resolve_secret`) into one of these
immediately and keeps the wrapper in local variables from then on -- only `.reveal()` unwraps it, and
only at the one call site that must send the raw value over the wire (the HTTP request body/headers).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

_MASK = "***"


@dataclass(frozen=True, slots=True)
class Secret:
    """Wraps one credential value. `repr()`/`str()` never show it -- only `reveal()` does."""

    _value: str

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def reveal(self) -> str:
        """The raw value, for the one call site that must send it (the auth request body/headers)."""
        return self._value


def mask_secrets(text: str, secrets: Iterable[Secret]) -> str:
    """Defense in depth: scrub any secret's raw value out of an error message before it is raised or
    logged, in case a lower layer (e.g. an HTTP client's own exception text) ever echoes request
    content back. Every current call site should never actually need this -- it is a backstop, not the
    primary control (the primary control is never letting the raw value outlive the request call)."""
    masked = text
    for secret in secrets:
        value = secret.reveal()
        if value:
            masked = masked.replace(value, _MASK)
    return masked
