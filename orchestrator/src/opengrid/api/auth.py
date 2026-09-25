"""Auth: `X-Remote-User` identity, config-mapped roles (02b S7, BUILD.md api row).

Apache (`deploy/README.md`) terminates HTTP Basic Auth and forwards the authenticated identity in
`X-Remote-User` after stripping any client-supplied value; `api` trusts that header because
`[api].bind_host` is loopback-only and nothing but Apache can reach it (02b Open point 7). Every
endpoint except `/og/api/health` requires the header. `/og/api/health` is the deploy-time liveness
probe (`deploy/scripts/deploy.sh` polls it directly on loopback with no Apache in front, so it never
carries the header) -- it is gated on the *connection* being loopback instead, and never trusts the
header if the connection somehow was not (defence in depth beyond `bind_host`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from opengrid.api.deps import get_config
from opengrid.platform.config import Config

_REMOTE_USER_HEADER = "x-remote-user"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class Role(StrEnum):
    """The two roles carried by Apache's `AuthUserFile` groups (deploy/README.md)."""

    OPERATOR = "operator"
    VIEWER = "viewer"


class Identity:
    """The authenticated caller: who Apache says they are, and which role they map to."""

    __slots__ = ("role", "user")

    def __init__(self, user: str, role: Role) -> None:
        self.user = user
        self.role = role


def _configured_members(cfg: Config, role: Role) -> set[str]:
    return set(cfg.get(f"api.roles.{role.value}", []) or [])


def role_for_identity(user: str, cfg: Config) -> Role | None:
    """Map an `X-Remote-User` value to a role via `[api.roles]` config, falling back to the identity
    matching the role name itself -- Apache's own accounts are literally named `operator`/`viewer`
    (deploy/README.md "UI credentials"), so that fallback works with zero extra configuration, while
    `[api.roles.operator]`/`[api.roles.viewer]` let an operator map additional named accounts.
    """
    if user in _configured_members(cfg, Role.OPERATOR) or user == Role.OPERATOR.value:
        return Role.OPERATOR
    if user in _configured_members(cfg, Role.VIEWER) or user == Role.VIEWER.value:
        return Role.VIEWER
    return None


async def current_identity(request: Request, cfg: Annotated[Config, Depends(get_config)]) -> Identity:
    """Every non-health endpoint depends on this: 401 if the header is missing, 403 if the identity
    does not map to a known role. Takes `cfg` through `opengrid.api.deps.get_config` (not
    `request.app.state.config` directly) so tests can override it the same way as every other
    dependency."""
    user = request.headers.get(_REMOTE_USER_HEADER)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing X-Remote-User")
    role = role_for_identity(user, cfg)
    if role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"No role mapped for identity {user!r}")
    return Identity(user, role)


async def require_viewer(identity: Annotated[Identity, Depends(current_identity)]) -> Identity:
    """`viewer` can read every GET/SSE endpoint (02b S7); `operator` implies `viewer` access too."""
    return identity


async def require_operator(identity: Annotated[Identity, Depends(current_identity)]) -> Identity:
    """Only `operator` may call a mutating endpoint (02b S7)."""
    if identity.role is not Role.OPERATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")
    return identity


async def require_loopback_health_probe(request: Request) -> None:
    """`/og/api/health` only: accept the connection when it originates from loopback, and never use
    `X-Remote-User` to make that decision (it is not read at all here) -- non-loopback callers are
    rejected outright regardless of any header they present."""
    client_host = request.client.host if request.client else None
    if client_host not in LOOPBACK_HOSTS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="health endpoint is loopback-only")
