"""ogsim.utility_aen.channels.customer_api -- the utility EMS calling the orchestrator's utility customer
API (`/og/api/customer/v1/utility/`, D-33) THROUGH Apache, with HTTP Basic credentials of the utility's
own Apache account. Like `ogsim.customer`, it never sets `X-Remote-User` (Apache's to set) and never calls
the orchestrator's loopback port. The HTTP transport is `ogsim.customer.api_client.HttpxTransport`
(reused, one fresh client per request, so no CSRF cookie is ever carried: a machine client).

Settings (`channels.customer_api` in the sim config; env wins):
- `base_url` / `OGSIM_UTILITY_API_BASE`: the Apache-fronted origin, e.g. `https://base.tocy-net.net`;
- credentials: `OGSIM_UTILITY_<CODE>_USER` / `OGSIM_UTILITY_<CODE>_PASSWORD` (`/etc/opengrid/utility_sim.env`,
  0640 root:opengrid), `<CODE>` = the utility's `env_code` (AEN for Austin Energy). Never logged.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from ogsim.customer.api_client import BasicAuth, HttpResult, HttpTransport, HttpxTransport
from ogsim.utility_aen.channels.base import CALL_STATES, CallResult, CallSpec, ChannelError, ChannelSettings

NAME = "customer_api"
API_PREFIX = "/og/api/customer/v1/utility"
BASE_URL_ENV_VAR = "OGSIM_UTILITY_API_BASE"
DEFAULT_TIMEOUT_S = 10.0
_TRANSPORT_FAILURE_STATUSES = frozenset({401, 403, 500, 502, 503, 504})


class CredentialsError(ChannelError):
    """The utility's Apache credentials are not in the environment (names the variable, never a value)."""


def resolve_credentials(env_code: str) -> BasicAuth:
    code = env_code.strip().upper()
    user_var, password_var = f"OGSIM_UTILITY_{code}_USER", f"OGSIM_UTILITY_{code}_PASSWORD"
    user, password = os.environ.get(user_var, ""), os.environ.get(password_var, "")
    if not user or not password:
        raise CredentialsError(f"{user_var}/{password_var} must both be set")
    return user, password


def _result(call_ref: str, http: HttpResult) -> CallResult:
    """Map an API answer onto a `CallResult`; a refusal body is `{"detail": {"reason_code", ...}}`."""
    if http.status_code in _TRANSPORT_FAILURE_STATUSES:
        raise ChannelError(f"utility API answered HTTP {http.status_code}")
    body: dict[str, Any] = http.body
    if http.status_code >= 400:
        detail = body.get("detail")
        refusal = detail if isinstance(detail, dict) else {"detail": str(detail)}
        return CallResult(
            call_ref=call_ref,
            accepted=False,
            state="REFUSED",
            reason_code=refusal.get("reason_code"),
            detail=refusal.get("detail"),
            remote_id=refusal.get("call_id"),
        )
    state = str(body.get("state", "UNKNOWN"))
    return CallResult(
        call_ref=call_ref,
        accepted=body.get("outcome") == "ACCEPTED",
        state=state if state in CALL_STATES else "UNKNOWN",
        reason_code=body.get("reason_code"),
        detail=body.get("detail"),
        delivered_kw=body.get("delivered_kw"),
        delivered_kwh=body.get("delivered_kwh"),
        remote_id=body.get("call_id"),
        delivery_state=body.get("delivery_state"),
    )


class CustomerApiChannel:
    """`Channel` over the utility customer API. Remembers `call_ref -> orchestrator call_id` for status
    and cancel; after a restart, re-issuing the same spec recovers it (the API replays the idempotency
    key and returns the original call)."""

    name = NAME

    def __init__(self, transport: HttpTransport, auth: BasicAuth) -> None:
        self._transport = transport
        self._auth = auth
        self._remote: dict[str, str] = {}

    async def _post(self, path: str, body: dict[str, Any]) -> HttpResult:
        try:
            return await self._transport.post(path, json=body, auth=self._auth)
        except (httpx.HTTPError, OSError) as exc:
            raise ChannelError(f"utility API unreachable: {type(exc).__name__}") from exc

    async def _get(self, path: str) -> HttpResult:
        try:
            return await self._transport.get(path, auth=self._auth)
        except (httpx.HTTPError, OSError) as exc:
            raise ChannelError(f"utility API unreachable: {type(exc).__name__}") from exc

    async def issue_call(self, spec: CallSpec) -> CallResult:
        body = {
            "kw": spec.kw,
            "duration_minutes": spec.duration_min,
            "start_at": spec.start.isoformat(),
            "idempotency_key": spec.call_ref,
            "reason": spec.reason,
        }
        result = _result(spec.call_ref, await self._post(f"{API_PREFIX}/calls", body))
        if result.remote_id:
            self._remote[spec.call_ref] = result.remote_id
        return result

    def _unknown(self, call_ref: str) -> CallResult:
        return CallResult(
            call_ref=call_ref, accepted=False, state="UNKNOWN", detail="call not issued by this EMS"
        )

    async def cancel(self, call_ref: str, *, end_at: datetime | None = None) -> CallResult:
        remote = self._remote.get(call_ref)
        if remote is None:
            return self._unknown(call_ref)
        body = {"end_at": end_at.isoformat()} if end_at is not None else {}
        http = await self._post(f"{API_PREFIX}/calls/{remote}/cancel", body)
        return _result(call_ref, http)

    async def status(self, call_ref: str) -> CallResult:
        remote = self._remote.get(call_ref)
        if remote is None:
            return self._unknown(call_ref)
        return _result(call_ref, await self._get(f"{API_PREFIX}/calls/{remote}"))

    async def obligations(self) -> dict[str, Any]:
        """`GET obligations`: my tolling obligations and today's reservation window."""
        http = await self._get(f"{API_PREFIX}/obligations")
        if http.status_code >= 400:
            raise ChannelError(f"utility API answered HTTP {http.status_code}")
        return http.body


def build(settings: ChannelSettings) -> CustomerApiChannel:
    """Factory registered as `customer_api`. `settings` may carry `transport` (tests) and `env_code`."""
    base_url = str(os.environ.get(BASE_URL_ENV_VAR) or settings.get("base_url", ""))
    transport = settings.get("transport")
    if transport is None:
        if not base_url:
            raise ChannelError(f"{BASE_URL_ENV_VAR} is not set; refusing to guess the orchestrator URL")
        transport = HttpxTransport(base_url, timeout_s=float(settings.get("timeout_s", DEFAULT_TIMEOUT_S)))
    return CustomerApiChannel(transport, resolve_credentials(str(settings.get("env_code", "AEN"))))


__all__ = ["API_PREFIX", "BASE_URL_ENV_VAR", "NAME", "CredentialsError", "CustomerApiChannel", "build"]
