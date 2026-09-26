"""`IdentityProvider` protocol: resolves the caller's asserted identity (a user id string) from the
request, or `None`. Never maps a user to a role -- `opengrid.api.auth.role_for_identity` stays the one
place that happens, identity-provider-agnostic, so either provider composes unchanged with today's
`[api.roles]` config.

`ApacheProxyIdentity` wraps the existing proxy-secret model (`opengrid.api.auth.verified_remote_user`)
unmodified and stays the default. `OidcIdentity` validates a `Bearer` JWT against a configured issuer's
JWKS (RS256, `pyjwt`) -- OFF by default, config-selected via `[authz.oidc].enabled` (`oidc_config_from`).
Neither provider is wired into `opengrid.api.auth.current_identity` yet; see the platform build report
for the exact swap-in line when an owner is ready to enable OIDC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from fastapi import Request

from opengrid.platform.config import Config

_BEARER_PREFIX = "Bearer "


class IdentityProvider(Protocol):
    def identify(self, request: Request) -> str | None:
        """The verified caller identity (e.g. an `X-Remote-User` value, or a JWT `sub`/username claim),
        or `None` if this request carries no valid assertion for this provider."""
        ...


@dataclass(frozen=True, slots=True)
class ApacheProxyIdentity:
    """The current, default provider: `X-Remote-User`, trusted only via `X-OG-Proxy-Auth` (`api/auth.py`
    module docstring). Imports `opengrid.api.auth` locally to avoid a circular import (that module does
    not import `opengrid.authz` today)."""

    def identify(self, request: Request) -> str | None:
        from opengrid.api.auth import verified_remote_user

        return verified_remote_user(request)


class OidcError(Exception):
    pass


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    jwks: dict[str, Any]
    audience: str | None = None
    username_claim: str = "sub"
    leeway_s: int = 30


def oidc_config_from(cfg: Config) -> OidcConfig | None:
    """`None` unless `[authz.oidc].enabled = true` (config-selectable, OFF by default). `jwks` is read
    from config (`[authz.oidc.jwks]`), never fetched over the network here -- mirrors how feed secrets
    are named-not-embedded (`platform/config.py`); refreshing the mirrored JWKS document out of band is
    a deploy-time concern, out of scope for this light version. Tests build an `OidcConfig` directly
    from a locally generated key pair instead of going through config at all."""
    if not cfg.get("authz.oidc.enabled", False):
        return None
    issuer = cfg.get("authz.oidc.issuer")
    jwks = cfg.get("authz.oidc.jwks")
    if not issuer or not jwks:
        raise OidcError("authz.oidc.enabled is true but issuer/jwks are not configured")
    return OidcConfig(
        issuer=str(issuer),
        jwks=jwks,
        audience=cfg.get("authz.oidc.audience"),
        username_claim=str(cfg.get("authz.oidc.username_claim", "sub")),
        leeway_s=int(cfg.get("authz.oidc.leeway_s", 30)),
    )


class OidcIdentity:
    """Validates `Authorization: Bearer <JWT>` (RS256) against `config.issuer`'s JWKS. Rejects (returns
    `None`, never raises past construction) an absent/malformed header, an unknown `kid`, a bad
    signature, a wrong issuer/audience, or an expired token (`pyjwt`'s own checks, `leeway_s` for clock
    skew) -- same fail-closed posture as `verified_remote_user`."""

    def __init__(self, config: OidcConfig) -> None:
        self._config = config
        self._keys_by_kid = {
            key["kid"]: jwt.PyJWK.from_dict(key) for key in config.jwks.get("keys", []) if "kid" in key
        }

    def identify(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.startswith(_BEARER_PREFIX):
            return None
        token = header[len(_BEARER_PREFIX) :]
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None
        signing_key = self._keys_by_kid.get(unverified_header.get("kid", ""))
        if signing_key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=[signing_key.algorithm_name],
                issuer=self._config.issuer,
                audience=self._config.audience,
                leeway=self._config.leeway_s,
                options={"verify_aud": self._config.audience is not None},
            )
        except jwt.InvalidTokenError:
            return None
        user = claims.get(self._config.username_claim)
        return str(user) if user else None
