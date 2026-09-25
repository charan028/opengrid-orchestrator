"""ERCOT Public API client (02b S2.2, S2.6): ROPC token auth, the five MVP-S products, and primary ->
secondary subscription-key rotation.

Reference only for HTTP call shape: the legacy `src/opengrid/clients/ercot_client.py` prototype (BUILD.md
"reused as thin reference, rewritten to the feeds interface" -- this module is new code, not an import of
that prototype).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.http_client import DEFAULT_TIMEOUT_S, FeedHttpError, request_with_retry
from opengrid.feeds.normalize import (
    ercot_as_price_to_feed_obs,
    ercot_load_to_feed_obs,
    ercot_solar_to_feed_obs,
    ercot_spp_to_feed_obs,
    ercot_wind_to_feed_obs,
)
from opengrid.feeds.secrets import Secret, mask_secrets
from opengrid.platform.config import resolve_secret

logger = logging.getLogger(__name__)

# The real Azure AD B2C ROPC token endpoint (02b S2.2). Overridable via `feeds.ercot.token_url` for a
# test/sim environment that stands in for it; a named constant per BUILD.md S5a ("no hard-coded hosts...
# or named constants") since no config key exists for it yet in `orchestrator.toml`.
DEFAULT_TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"  # noqa: S105
)
TOKEN_CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"  # noqa: S105 -- ERCOT's published public-API client id, not a secret

TOKEN_LIFETIME_S = 3600
TOKEN_RENEW_MARGIN_S = 300  # renew 5 min before expiry (02b S2.2)

KeyName = Literal["PRIMARY", "SECONDARY"]

PRODUCT_PATHS: dict[str, tuple[str, dict[str, str]]] = {
    "np6-905-cd": ("/np6-905-cd/spp_node_zone_hub", {"settlementPointType": "LZ"}),
    "np6-345-cd": ("/np6-345-cd/act_sys_load_by_wzn", {}),
    "np4-732-cd": ("/np4-732-cd/wpp_hrly_avrg_actl_fcast", {}),
    "np4-737-cd": ("/np4-737-cd/spp_hrly_avrg_actl_fcast", {}),
    "np4-188-cd": ("/np4-188-cd/dam_clear_price_for_cap", {}),
}

_NORMALIZERS = {
    "np6-905-cd": ercot_spp_to_feed_obs,
    "np6-345-cd": ercot_load_to_feed_obs,
    "np4-732-cd": ercot_wind_to_feed_obs,
    "np4-737-cd": ercot_solar_to_feed_obs,
    "np4-188-cd": ercot_as_price_to_feed_obs,
}


@dataclass
class KeyRotationEvent:
    from_key: KeyName
    to_key: KeyName
    reason: str


class ErcotAuthError(Exception):
    """Both keys failed, or the ROPC token endpoint rejected the credentials outright."""


@dataclass
class _TokenCache:
    access_token: str | None = None
    expires_at: datetime | None = None

    def is_valid(self, *, now: datetime) -> bool:
        return (
            self.access_token is not None
            and self.expires_at is not None
            and now < self.expires_at - timedelta(seconds=TOKEN_RENEW_MARGIN_S)
        )


@dataclass
class ErcotClient:
    """One instance for the `og-feeds` process lifetime. Holds the ROPC token cache and the currently
    active subscription key; rotation is one-way (primary -> secondary) for the process lifetime (02b
    S2.6), never automatic rotation back.
    """

    base_url: str
    username_env: str
    password_env: str
    primary_key_env: str
    secondary_key_env: str
    http_client: httpx.AsyncClient
    token_url: str = DEFAULT_TOKEN_URL
    _token: _TokenCache = field(default_factory=_TokenCache)
    _active_key: KeyName = "PRIMARY"

    @property
    def active_key(self) -> KeyName:
        return self._active_key

    async def _authenticate(self) -> str:
        # Resolved once and kept as `Secret` from here on -- BUILD.md S5a/S6: never a raw credential
        # in a local variable pytest (or a debugger) could dump, only `.reveal()`'d at the one call
        # site that must send it.
        username = Secret(resolve_secret(self.username_env))
        password = Secret(resolve_secret(self.password_env))
        secrets = (username, password)
        try:
            response = await request_with_retry(
                self.http_client,
                "POST",
                self.token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "password",
                    "username": username.reveal(),
                    "password": password.reveal(),
                    "client_id": TOKEN_CLIENT_ID,
                    "response_type": "id_token",
                    "scope": f"openid {TOKEN_CLIENT_ID} offline_access",
                },
                timeout_s=DEFAULT_TIMEOUT_S,
                max_retries=0,
            )
        except FeedHttpError as exc:
            # `raise ... from None` drops the original traceback frame (whose locals held the request
            # body) from anything pytest/logging renders; the message itself is masked as a backstop.
            raise ErcotAuthError(mask_secrets(str(exc), secrets)) from None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ErcotAuthError(mask_secrets(str(exc), secrets)) from None

        body = response.json()
        token = body.get("id_token")
        if not token:
            raise ErcotAuthError("ROPC token response missing id_token")
        return str(token)

    async def _get_token(self, *, now: datetime, force: bool = False) -> str:
        if force or not self._token.is_valid(now=now):
            token = await self._authenticate()
            self._token = _TokenCache(
                access_token=token, expires_at=now + timedelta(seconds=TOKEN_LIFETIME_S)
            )
        if self._token.access_token is None:  # defensive: _authenticate() always sets it or raises
            raise ErcotAuthError("token cache empty after successful authentication")
        return self._token.access_token

    def _current_subscription_key(self) -> Secret:
        env_name = self.primary_key_env if self._active_key == "PRIMARY" else self.secondary_key_env
        return Secret(resolve_secret(env_name))

    def _rotate_key(self, *, reason: str) -> KeyRotationEvent | None:
        if self._active_key == "SECONDARY":
            return None
        event = KeyRotationEvent(from_key="PRIMARY", to_key="SECONDARY", reason=reason)
        self._active_key = "SECONDARY"
        logger.warning("ercot key rotation", extra={"reason": reason})
        return event

    async def fetch_product(
        self, product: str, *, now: datetime | None = None
    ) -> tuple[list[FeedObs], list[KeyRotationEvent]]:
        """Poll one ERCOT product, normalize its rows, and return `(observations, rotation_events)`.

        On a 401/403, retries once with a freshly re-authenticated token; if that also fails with
        401/403, rotates to the secondary subscription key and retries once more before giving up (02b
        S2.2, S2.6). Raises `FeedHttpError`/`FeedDataError` if all of that is exhausted.
        """
        now = now or datetime.now(UTC)
        if product not in PRODUCT_PATHS:
            raise ValueError(f"unknown ERCOT product: {product}")
        path, params = PRODUCT_PATHS[product]
        url = f"{self.base_url}{path}"
        events: list[KeyRotationEvent] = []

        for reauth in (False, True):
            token = Secret(await self._get_token(now=now, force=reauth))
            subscription_key = self._current_subscription_key()
            request_secrets = (token, subscription_key)
            try:
                response = await request_with_retry(
                    self.http_client,
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {token.reveal()}",
                        "Ocp-Apim-Subscription-Key": subscription_key.reveal(),
                    },
                    params=params,
                )
            except FeedHttpError as exc:
                if exc.status_code in (401, 403) and not reauth:
                    continue  # one forced re-authentication + retry, per 02b S2.2
                if exc.status_code in (401, 403) and reauth:
                    rotation = self._rotate_key(reason=f"http_{exc.status_code}")
                    if rotation is None:
                        raise FeedHttpError(
                            exc.status_code, mask_secrets(str(exc), request_secrets)
                        ) from None
                    events.append(rotation)
                    subscription_key = self._current_subscription_key()
                    try:
                        response = await request_with_retry(
                            self.http_client,
                            "GET",
                            url,
                            headers={
                                "Authorization": f"Bearer {token.reveal()}",
                                "Ocp-Apim-Subscription-Key": subscription_key.reveal(),
                            },
                            params=params,
                        )
                    except FeedHttpError as retry_exc:
                        raise FeedHttpError(
                            retry_exc.status_code,
                            mask_secrets(str(retry_exc), (token, subscription_key)),
                        ) from None
                else:
                    raise FeedHttpError(exc.status_code, mask_secrets(str(exc), request_secrets)) from None
            payload = response.json()
            normalizer = _NORMALIZERS[product]
            return normalizer(payload, product=product, recorded_at=now), events

        raise ErcotAuthError(f"ERCOT authentication failed for product {product!r}")
