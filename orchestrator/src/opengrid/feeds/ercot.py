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
from typing import Any, Literal

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.http_client import DEFAULT_TIMEOUT_S, FeedHttpError, request_with_retry
from opengrid.feeds.normalize import (
    ERCOT_ENERGY_PRICE_PRODUCTS,
    EXTREME_UNCORROBORATED,
    ercot_as_price_to_feed_obs,
    ercot_load_to_feed_obs,
    ercot_solar_by_region_to_feed_obs,
    ercot_solar_to_feed_obs,
    ercot_spp_to_feed_obs,
    ercot_wind_to_feed_obs,
    screen_prices,
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

#: D-28 regional solar product id (see `PRODUCT_PATHS`); `[feeds.ercot].solar_by_region_enabled` switches
#: its polling off without a deploy (`opengrid.feeds._build_scheduler`).
SOLAR_BY_REGION_PRODUCT = "np4-745-cd"

PRODUCT_PATHS: dict[str, tuple[str, dict[str, str]]] = {
    "np6-905-cd": ("/np6-905-cd/spp_node_zone_hub", {"settlementPointType": "LZ"}),
    "np6-345-cd": ("/np6-345-cd/act_sys_load_by_wzn", {}),
    "np4-732-cd": ("/np4-732-cd/wpp_hrly_avrg_actl_fcast", {}),
    "np4-737-cd": ("/np4-737-cd/spp_hrly_avrg_actl_fcast", {}),
    "np4-188-cd": ("/np4-188-cd/dam_clear_price_for_cap", {}),
    # D-28: Solar Power Production - Hourly Averaged Actual and Forecasted Values by Geographical Region
    # (ERCOT data product NP4-745-CD, hourly, public). Endpoint verified against ERCOT's data-product page
    # and the public-API spec; the per-region field names are inferred from NP4-737-CD's live-confirmed
    # pattern and are UNCONFIRMED against a live response (see `ercot_solar_by_region_to_feed_obs`).
    SOLAR_BY_REGION_PRODUCT: ("/np4-745-cd/spp_hrly_actual_fcast_geo", {}),
}

# BUILD.md follow-up finding: confirmed live that np6-345-cd/np4-732-cd/np4-737-cd/np4-188-cd return
# an EMPTY `data` array with no date-range query params (unlike np6-905-cd, which defaults to
# "latest"). Each needs an explicit lookback window to reliably return its most recent posting; the
# param names differ per product's own API (also confirmed live -- np6-345-cd rejects
# `operatingDateFrom/To`, only `operatingDayFrom/To` is accepted).
_DEFAULT_LOOKBACK_DAYS = 2
_DATE_RANGE_PARAM_NAMES: dict[str, tuple[str, str]] = {
    "np6-345-cd": ("operatingDayFrom", "operatingDayTo"),
    "np4-732-cd": ("postedDatetimeFrom", "postedDatetimeTo"),
    "np4-737-cd": ("postedDatetimeFrom", "postedDatetimeTo"),
    "np4-188-cd": ("deliveryDateFrom", "deliveryDateTo"),
    SOLAR_BY_REGION_PRODUCT: ("postedDatetimeFrom", "postedDatetimeTo"),
}


def _default_date_range_params(product: str, now: datetime) -> dict[str, str]:
    """A `{from_param: value, to_param: value}` lookback window for products that return no data
    without one (see `_DATE_RANGE_PARAM_NAMES`); `{}` for products that already default to "latest"
    (np6-905-cd)."""
    names = _DATE_RANGE_PARAM_NAMES.get(product)
    if names is None:
        return {}
    from_param, to_param = names
    start = now - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    if from_param.endswith("Datetime"):
        return {from_param: start.strftime("%Y-%m-%dT%H:%M:%S"), to_param: now.strftime("%Y-%m-%dT%H:%M:%S")}
    return {from_param: start.date().isoformat(), to_param: now.date().isoformat()}


_NORMALIZERS = {
    "np6-905-cd": ercot_spp_to_feed_obs,
    "np6-345-cd": ercot_load_to_feed_obs,
    "np4-732-cd": ercot_wind_to_feed_obs,
    "np4-737-cd": ercot_solar_to_feed_obs,
    "np4-188-cd": ercot_as_price_to_feed_obs,
    SOLAR_BY_REGION_PRODUCT: ercot_solar_by_region_to_feed_obs,
}


def _screen_prices(product: str, observations: list[FeedObs]) -> list[FeedObs]:
    """FR-ING-117 / V-P1 (`normalize.screen_prices`): extremes are kept and flagged
    EXTREME_UNCORROBORATED (never clipped); only a non-number or a value outside the hard bounds is
    quarantined, and each quarantined row is logged."""
    kept, quarantined = screen_prices(observations)
    for row in quarantined:
        logger.warning(
            "ercot price quarantined at ingest (not a number, or outside the V-P1 hard bounds)",
            extra={
                "product": product,
                "series": row.series,
                "ts": row.ts.isoformat(),
                "value": str(row.value),
            },
        )
    for row in kept:
        if row.quality == EXTREME_UNCORROBORATED:
            logger.warning(
                "ercot price outside the normal band; kept as EXTREME_UNCORROBORATED",
                extra={
                    "product": product,
                    "series": row.series,
                    "ts": row.ts.isoformat(),
                    "value": row.value,
                },
            )
    return kept


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
        _path, static_params = PRODUCT_PATHS[product]
        params = {**static_params, **_default_date_range_params(product, now)}
        page = await self.fetch_page(product, params=params, now=now)
        return page.observations, page.rotation_events

    async def fetch_page(
        self, product: str, *, params: dict[str, str], now: datetime | None = None
    ) -> ErcotPage:
        """One page of `product` with caller-supplied query `params` (replacing the product's static
        and default date-range params entirely), normalized, plus ERCOT's `_meta.totalPages`. The live
        poll (`fetch_product`) and the history backfill (`tools/ercot_backfill.py`) share this one
        auth/key-rotation/normalization path."""
        now = now or datetime.now(UTC)
        if product not in PRODUCT_PATHS:
            raise ValueError(f"unknown ERCOT product: {product}")
        path, _static_params = PRODUCT_PATHS[product]
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
            observations = normalizer(payload, product=product, recorded_at=now)
            if product in ERCOT_ENERGY_PRICE_PRODUCTS:
                observations = _screen_prices(product, observations)
            return ErcotPage(
                observations=observations,
                rotation_events=events,
                total_pages=_total_pages(payload),
            )

        raise ErcotAuthError(f"ERCOT authentication failed for product {product!r}")


def ercot_client_from_config(ercot_cfg: dict[str, Any], http_client: httpx.AsyncClient) -> ErcotClient:
    """The one place an `ErcotClient` is built from a `[feeds.ercot]` config table (og-feeds and the
    history backfill tool share it, so both use the same env-var names and token URL)."""
    return ErcotClient(
        base_url=ercot_cfg["base_url"],
        username_env=ercot_cfg["username_env"],
        password_env=ercot_cfg["password_env"],
        primary_key_env=ercot_cfg["subscription_key_env"],
        secondary_key_env=ercot_cfg.get("subscription_key_secondary_env", "ERCOT_PUBLIC_API_KEY_SECONDARY"),
        http_client=http_client,
        token_url=ercot_cfg.get("token_url", DEFAULT_TOKEN_URL),
    )


@dataclass
class ErcotPage:
    observations: list[FeedObs]
    rotation_events: list[KeyRotationEvent]
    total_pages: int  # ERCOT `_meta.totalPages`; 1 when the response carries no paging metadata


def _total_pages(payload: object) -> int:
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    total = meta.get("totalPages") if isinstance(meta, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else 1
