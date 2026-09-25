"""Operator/viewer role resolution -- 02b S7 ("two roles ... carried as an HTTP Basic Auth identity
mapped to a role by Apache ... re-asserted in FastAPI via a dependency that reads the header Apache
forwards"). Owner: ui-a (BUILD.md S4).

Loopback trust boundary (02b Open point 7): this module trusts `X-OG-Role` as-is because only Apache can
reach `og-api`'s bind address. It does not, and cannot, independently verify the header's authenticity --
that gap is documented, not hidden.
"""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)

VIEWER = "viewer"
OPERATOR = "operator"
_ROLE_HEADER = "x-og-role"
_KNOWN_ROLES = (VIEWER, OPERATOR)


def role_of(request: Request) -> str:
    """Return `"operator"` or `"viewer"` (default) for the current request. A header present but not one
    of the known role names is logged (BUILD.md code-review round item 5) and treated as `"viewer"` --
    Apache is trusted to only ever forward a known role, so an unrecognized value is worth a warning even
    though the request still degrades safely rather than failing closed."""
    raw = request.headers.get(_ROLE_HEADER, VIEWER).strip().lower()
    if raw in _KNOWN_ROLES:
        return raw
    logger.warning("unknown %s header value=%r; defaulting to %s", _ROLE_HEADER, raw, VIEWER)
    return VIEWER


def is_operator(request: Request) -> bool:
    """True only for an operator; templates use this to hide write actions (BUILD.md UI brief)."""
    return role_of(request) == OPERATOR
