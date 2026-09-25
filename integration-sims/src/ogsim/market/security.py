"""ERCOT B2C token issuance and subscription-key checking for ogsim.market."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from ogsim.market.config import MarketConfig

TOKEN_LIFETIME_S = 3600


@dataclass
class IssuedToken:
    token: str
    issued_at: float

    def expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now - self.issued_at > TOKEN_LIFETIME_S


class TokenStore:
    """Accepts any of the configured test users and issues opaque bearer
    tokens with a 1-hour expiry, mirroring ERCOT's Azure AD B2C ROPC flow."""

    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg
        self._tokens: dict[str, IssuedToken] = {}

    def authenticate(self, username: str, password: str) -> str | None:
        expected = self.cfg.test_users.get(username)
        if expected is None or expected != password:
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = IssuedToken(token=token, issued_at=time.time())
        return token

    def valid(self, token: str) -> bool:
        issued = self._tokens.get(token)
        return issued is not None and not issued.expired()


def key_kind(cfg: MarketConfig, subscription_key: str | None) -> str | None:
    """Returns 'primary', 'secondary', or None if the key is unrecognized."""
    if subscription_key == cfg.key_primary:
        return "primary"
    if subscription_key == cfg.key_secondary:
        return "secondary"
    return None
