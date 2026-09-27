"""Auth: `X-Remote-User` identity, config-mapped roles (02b S7, BUILD.md api row).

Apache (`deploy/README.md`) terminates HTTP Basic Auth and forwards the authenticated identity in
`X-Remote-User` after stripping any client-supplied value. `[api].bind_host` is loopback-only, but
loopback is not a trust boundary on the base server: the simulators, workspace test runs and any other
local process can reach the port and set `X-Remote-User` themselves. So the identity header is only
believed when the request also carries `X-OG-Proxy-Auth` equal to the shared secret
`OG_API_PROXY_SECRET`, which Apache sets (after unsetting any client value) and nothing else on the host
is given. No secret configured, no proxy header, or a wrong one: 401, fail closed.

`/og/api/health` is the deploy-time liveness probe (`deploy/scripts/deploy.sh` polls it directly on
loopback with no Apache in front, so it never carries either header) -- it is gated on the *connection*
being loopback instead, and never trusts the header if the connection somehow was not (defence in depth
beyond `bind_host`).

Roles: `operator`, `viewer` and `customer`. A customer identity (`[api.roles.customer]`, a table of
`user = "<customer_id>"`) may only use the customer API (`opengrid.customer_api`); `require_viewer` and
`require_operator` refuse it, so a customer can never read the operator console's fleet-wide data. A
utility identity (`[api.roles.utility]`, `user = "<utility_id>"`, D-33) likewise reaches only its own
tolling obligations and calls through the utility routes of the customer API.
"""

from __future__ import annotations

import logging
import os
import secrets
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from opengrid.api.deps import get_config
from opengrid.authz.enforce import policy_engine_for
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

_REMOTE_USER_HEADER = "x-remote-user"
PROXY_AUTH_HEADER = "x-og-proxy-auth"
PROXY_SECRET_ENV = "OG_API_PROXY_SECRET"  # noqa: S105 -- an env-var name, not a secret
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class Role(StrEnum):
    """Operator/viewer (Apache's `AuthUserFile` accounts, deploy/README.md), customer and utility (D-33)."""

    OPERATOR = "operator"
    VIEWER = "viewer"
    CUSTOMER = "customer"
    UTILITY = "utility"


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
    matching the role name itself for operator/viewer -- Apache's own accounts are literally named
    `operator`/`viewer` (deploy/README.md "UI credentials"), so that fallback works with zero extra
    configuration, while `[api.roles.operator]`/`[api.roles.viewer]` let an operator map additional
    named accounts. A customer is only ever an explicitly configured `[api.roles.customer]` key (it
    also needs its customer_id there), never a name fallback.
    """
    if user in _configured_members(cfg, Role.OPERATOR) or user == Role.OPERATOR.value:
        return Role.OPERATOR
    if user in _configured_members(cfg, Role.VIEWER) or user == Role.VIEWER.value:
        return Role.VIEWER
    if user in _configured_members(cfg, Role.CUSTOMER):
        return Role.CUSTOMER
    if user in _configured_members(cfg, Role.UTILITY):
        return Role.UTILITY
    return None


def proxy_authenticated(request: Request) -> bool:
    """True only when the request carries `X-OG-Proxy-Auth` equal to `OG_API_PROXY_SECRET` (constant-
    time compare). An unset or empty secret authenticates nothing (fail closed) and is logged."""
    expected = os.environ.get(PROXY_SECRET_ENV, "")
    if not expected:
        logger.error(
            "proxy secret env var is not set; refusing every identity header", extra={"env": PROXY_SECRET_ENV}
        )
        return False
    presented = request.headers.get(PROXY_AUTH_HEADER)
    return presented is not None and secrets.compare_digest(presented.encode(), expected.encode())


def verified_remote_user(request: Request) -> str | None:
    """The `X-Remote-User` identity, only if the request came through Apache (`proxy_authenticated`);
    otherwise `None`. The single place any module (API or UI) reads the identity header."""
    user = request.headers.get(_REMOTE_USER_HEADER)
    if not user or not proxy_authenticated(request):
        return None
    return user


async def current_identity(request: Request, cfg: Annotated[Config, Depends(get_config)]) -> Identity:
    """Every non-health endpoint depends on this: 401 if the header is missing or did not come through
    Apache (no or wrong proxy secret), 403 if the identity does not map to a known role. Takes `cfg`
    through `opengrid.api.deps.get_config` (not `request.app.state.config` directly) so tests can
    override it the same way as every other dependency."""
    if not request.headers.get(_REMOTE_USER_HEADER):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing X-Remote-User")
    user = verified_remote_user(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Identity not asserted by the proxy")
    role = role_for_identity(user, cfg)
    if role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"No role mapped for identity {user!r}")
    return Identity(user, role)


async def require_viewer(
    identity: Annotated[Identity, Depends(current_identity)], cfg: Annotated[Config, Depends(get_config)]
) -> Identity:
    """`viewer` can read every GET/SSE endpoint (02b S7); `operator` implies `viewer` access too. A
    `customer` is refused: fleet-wide reads would expose other customers' data.

    The allow/deny call itself is delegated to `opengrid.authz`'s `PolicyEngine` (`config/authz.toml`'s
    `api.read` rule) rather than re-implemented here -- the one additive wiring point BUILD.md's authz
    ownership calls for. The outcome is unchanged: `viewer`/`operator` pass, `customer` still gets 403.
    """
    decision = policy_engine_for(cfg).decide(role=identity.role.value, action="api.read")
    if decision.deny:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator or viewer role required")
    return identity


async def require_operator(
    identity: Annotated[Identity, Depends(current_identity)], cfg: Annotated[Config, Depends(get_config)]
) -> Identity:
    """Only `operator` may call a mutating endpoint (02b S7). Routed through the same `PolicyEngine`
    (`api.write`) as `require_viewer` above; behaviour is unchanged."""
    decision = policy_engine_for(cfg).decide(role=identity.role.value, action="api.write")
    if decision.deny:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")
    return identity


async def require_loopback_health_probe(request: Request) -> None:
    """`/og/api/health` only: accept the connection when it originates from loopback, and never use
    `X-Remote-User` to make that decision (it is not read at all here) -- non-loopback callers are
    rejected outright regardless of any header they present."""
    client_host = request.client.host if request.client else None
    if client_host not in LOOPBACK_HOSTS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="health endpoint is loopback-only")
