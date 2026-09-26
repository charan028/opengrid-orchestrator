"""ogsim.customer.api_client -- HTTP client adapter for the orchestrator's customer API.

Boundary for tests (mirrors `ogsim.common.mqtt_client`'s `MqttTransport` protocol):
`CustomerApiClient` depends only on `HttpTransport`, a tiny protocol matching the subset
of an HTTP client this module needs. Unit tests substitute a fake transport; no live
orchestrator is required.

`CustomerApiClient` targets the customer-role routes the orchestrator's API agent is
building under `/og/api/customer/...` (POST opportunities, GET obligations/invoices, POST
invoices/{id}/dispute, POST obligations/{id}/cancel|renominate).

Auth (lead coordination, dev-environment Apache accounts): the sim calls the API THROUGH
Apache, never the loopback API directly, and authenticates with HTTP Basic using one of the
Apache basic-auth users the owner provisioned per customer group (`og-cust-dc`,
`og-cust-pipe`, `og-cust-ercot`, `og-cust-dist`, `og-cust-partner`; see
`ogsim.customer.config.resolve_customer_credentials`). It never sets `X-Remote-User`
itself -- that header is Apache's to set (from the verified Basic-auth identity) once it
proxies to the orchestrator, and a caller-supplied one would be trivially spoofable.
Passwords are read from environment variables (`EnvironmentFile=/etc/opengrid/
customer_sim.env` in the systemd unit) and are never logged.

Every path lookup goes through `_CUSTOMER_PATHS`/`_OPERATOR_FALLBACK_PATHS`, so the mapping
can be adjusted in one place if the final contract differs. `API_MODE_OPERATOR_FALLBACK`
targets today's operator-only `POST /og/api/opportunities` instead, for use before the
customer role/routes exist; every other action has no operator-endpoint equivalent and
raises `OperatorFallbackUnsupportedError` in that mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from ogsim.customer.config import API_MODE_CUSTOMER, API_MODE_OPERATOR_FALLBACK

ADMISSION_REJECTED_STATUS = 409

#: HTTP Basic (username, password); never logged or repr'd (dataclasses below carry no
#: credentials, only this bare tuple, passed straight through to the transport).
BasicAuth = tuple[str, str]


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: dict[str, Any]


class HttpTransport(Protocol):
    """The subset of an async HTTP client this adapter needs, so tests can fake it."""

    async def get(self, path: str, auth: BasicAuth) -> HttpResult: ...

    async def post(self, path: str, json: dict[str, Any], auth: BasicAuth) -> HttpResult: ...


class HttpxTransport:
    """Adapts `httpx.AsyncClient` to the `HttpTransport` protocol above. `base_url` is the
    Apache-fronted URL (`OGSIM_CUSTOMER_API_BASE`, e.g. `https://base.<host>`), never the
    orchestrator's loopback port -- Apache is what turns the HTTP Basic identity into the
    trusted `X-Remote-User` the orchestrator's API reads, and adds the proxy secret itself.

    Only HTTP Basic credentials are sent: never `X-Remote-User`, never `X-OG-Proxy-Auth`. The
    request paths are absolute (`/og/api/customer/...`), so only the scheme and host of
    `base_url` are used -- a base ending in `/og/api` would otherwise double the prefix.
    `transport` is for tests (an `httpx.MockTransport`)."""

    def __init__(self, base_url: str, timeout_s: float = 10.0, transport: Any = None) -> None:
        self._base_url = _origin(base_url)
        self._timeout_s = timeout_s
        self._transport = transport

    def _client(self, auth: BasicAuth) -> Any:
        import httpx

        return httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout_s, auth=auth, transport=self._transport
        )

    async def get(self, path: str, auth: BasicAuth) -> HttpResult:
        async with self._client(auth) as client:
            resp = await client.get(path)
            return HttpResult(resp.status_code, _safe_json(resp))

    async def post(self, path: str, json: dict[str, Any], auth: BasicAuth) -> HttpResult:
        async with self._client(auth) as client:
            resp = await client.post(path, json=json)
            return HttpResult(resp.status_code, _safe_json(resp))


def _origin(base_url: str) -> str:
    """`scheme://host[:port]` of `base_url` (the request paths carry the full `/og/api/...`)."""
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError("OGSIM_CUSTOMER_API_BASE must be an absolute URL, e.g. https://base.<host>")
    return f"{parts.scheme}://{parts.netloc}"


def _safe_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class AdmissionRejectedError(RuntimeError):
    """Raised when the orchestrator rejects a submitted opportunity (409, or any 4xx)."""

    def __init__(self, status_code: int, reason_code: str | None, body: dict[str, Any]) -> None:
        super().__init__(f"opportunity rejected ({status_code}): {reason_code}")
        self.status_code = status_code
        self.reason_code = reason_code
        self.body = body


class OperatorFallbackUnsupportedError(RuntimeError):
    """Raised when `api_mode=operator_fallback` is asked for an action the existing
    operator-only API has no equivalent of: only opportunity submission works in that
    mode. Callers (ogsim.customer.runtime) treat this as "skip for now", not a fault."""


# Customer-role paths the orchestrator's API agent is building (BUILD.md coordination:
# scoped to the caller's own customer_id via the `customer` role).
_CUSTOMER_PATHS = {
    "opportunities": "/og/api/customer/opportunities",
    "obligations": "/og/api/customer/obligations",
    "contracts": "/og/api/customer/contracts",
    "invoices": "/og/api/customer/invoices",
    "invoice_dispute": "/og/api/customer/invoices/{invoice_id}/dispute",
    "obligation_cancel": "/og/api/customer/obligations/{obligation_id}/cancel",
    "obligation_renominate": "/og/api/customer/obligations/{obligation_id}/renominate",
}
# Today's operator-only equivalent, for the fallback mode. Only "opportunities" has one.
_OPERATOR_FALLBACK_PATHS = {"opportunities": "/og/api/opportunities"}


class CustomerApiClient:
    """One customer operator's view of the orchestrator's API: submits opportunities,
    polls obligation/invoice state, and reacts to delivery and billing (BUILD.md owner
    requirement 3). Authenticates with HTTP Basic (`auth`) through Apache; never sets
    `X-Remote-User` itself."""

    def __init__(
        self, transport: HttpTransport, auth: tuple[str, str], mode: str = API_MODE_CUSTOMER
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._mode = mode

    def _require_customer_mode(self, action: str) -> None:
        if self._mode != API_MODE_CUSTOMER:
            raise OperatorFallbackUnsupportedError(
                f"{action} has no operator-endpoint equivalent; requires api_mode={API_MODE_CUSTOMER!r} "
                f"(currently {self._mode!r})"
            )

    async def submit_opportunity(self, payload: dict[str, Any]) -> dict[str, Any]:
        paths = _CUSTOMER_PATHS if self._mode == API_MODE_CUSTOMER else _OPERATOR_FALLBACK_PATHS
        result = await self._transport.post(paths["opportunities"], json=payload, auth=self._auth)
        if result.status_code >= 400:
            raise AdmissionRejectedError(result.status_code, result.body.get("reason_code"), result.body)
        return result.body

    async def get_obligations(self) -> list[dict[str, Any]]:
        self._require_customer_mode("get_obligations")
        result = await self._transport.get(_CUSTOMER_PATHS["obligations"], auth=self._auth)
        return list(result.body.get("obligations", []))

    async def get_contracts(self) -> list[dict[str, Any]]:
        """Reads this customer's own contracts (lead coordination: startup contract-id
        discovery reads this and `get_obligations` from the API rather than trusting a
        hard-coded YAML value, per dev/seed/customer_services_seed.sql)."""
        self._require_customer_mode("get_contracts")
        result = await self._transport.get(_CUSTOMER_PATHS["contracts"], auth=self._auth)
        return list(result.body.get("contracts", []))

    async def get_invoices(self) -> list[dict[str, Any]]:
        self._require_customer_mode("get_invoices")
        result = await self._transport.get(_CUSTOMER_PATHS["invoices"], auth=self._auth)
        return list(result.body.get("invoices", []))

    async def dispute_invoice(self, invoice_id: str, reason_code: str) -> dict[str, Any]:
        self._require_customer_mode("dispute_invoice")
        path = _CUSTOMER_PATHS["invoice_dispute"].format(invoice_id=invoice_id)
        result = await self._transport.post(path, json={"reason_code": reason_code}, auth=self._auth)
        return result.body

    async def cancel_obligation(self, obligation_id: str) -> dict[str, Any]:
        self._require_customer_mode("cancel_obligation")
        path = _CUSTOMER_PATHS["obligation_cancel"].format(obligation_id=obligation_id)
        result = await self._transport.post(path, json={}, auth=self._auth)
        return result.body

    async def renominate_obligation(
        self, obligation_id: str, window_start: str, window_end: str
    ) -> dict[str, Any]:
        self._require_customer_mode("renominate_obligation")
        path = _CUSTOMER_PATHS["obligation_renominate"].format(obligation_id=obligation_id)
        body = {"window_start": window_start, "window_end": window_end}
        result = await self._transport.post(path, json=body, auth=self._auth)
        return result.body


__all__ = [
    "ADMISSION_REJECTED_STATUS",
    "API_MODE_CUSTOMER",
    "API_MODE_OPERATOR_FALLBACK",
    "AdmissionRejectedError",
    "CustomerApiClient",
    "HttpResult",
    "HttpTransport",
    "HttpxTransport",
    "OperatorFallbackUnsupportedError",
]
