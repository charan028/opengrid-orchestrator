"""HTTP client the UI's own screen routes use to consume `opengrid.api`'s REST endpoints (02b S7) for
first-paint server rendering. Owner: ui-a (BUILD.md S4).

The UI and API are built in parallel by separate agents (BUILD.md task brief: "consume them by path per
02b S7"); this module is the only place that boundary is crossed, and it is a plain HTTP call to the
running `og-api` process, never an import of `opengrid.api` internals. Tests monkeypatch `get_json`
directly and feed it recorded JSON fixtures instead of a live API (BUILD.md task brief).

Live, per-second updates are the browser's job (SSE via `og.sse()`, static/og.js) -- this module is only
used for the initial HTMX GET / server-rendered paint of each screen (02b S8 "static shell + first paint
is a normal HTMX GET").
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

import httpx

from opengrid.api.auth import PROXY_SECRET_ENV

_DEFAULT_BASE_URL = "http://127.0.0.1:8080"
_TIMEOUT_S = 3.0
_POST_TIMEOUT_S = 5.0
_ENV_BASE_URL = "OG_API_BASE_URL"

#: The identity of the browser request a screen route is serving, bound per request by
#: `opengrid.ui.routes` and forwarded to `opengrid.api` as `X-Remote-User` (its auth requires it on every
#: non-health endpoint, `opengrid.api.auth`). Without this every server-side first-paint call was a 401
#: and every screen rendered its degraded banner (found on the first live run, U1).
_remote_user: ContextVar[str | None] = ContextVar("og_ui_remote_user", default=None)


def bind_remote_user(user: str | None) -> Token[str | None]:
    """Bind the caller identity for the current request; returns the token for `ContextVar.reset`."""
    return _remote_user.set(user)


def _headers() -> dict[str, str]:
    """Headers for a first-paint GET: the bound (already proxy-verified) identity plus the proxy secret,
    exactly as `post_json` sends them (`_identity_headers`), or none when no identity is bound."""
    return _identity_headers(_remote_user.get()) or {}


class ApiUnavailable(Exception):  # noqa: N818 -- shared symbol name; ui-b's screens already import it
    """Raised when the API cannot be reached or returns a non-2xx status. Routes catch this and render
    the screen degraded (missing widgets, a visible banner) rather than a 500 -- an operator console must
    stay usable when one dependency is slow or down (02b S6.5 degraded-mode principle, applied to the
    UI's own read path).

    `status_code`/`detail` are populated when the failure was a non-2xx HTTP response (as opposed to a
    transport error/timeout), taken from the API's own response, so a caller that needs to distinguish
    e.g. a 409 guardian veto from a 503 timeout or a 410 expired proposal can branch on them without
    re-parsing the exception message."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: Any = None) -> None:
        if status_code == 401:
            # Seen live: the console opened on og-api's own port (8080) instead of through Apache or the
            # dev proxy, so no X-Remote-User reached the UI and every first-paint call was refused.
            message += (
                " -- no identity reached the console; open it through Apache (production) or the dev"
                " proxy (http://127.0.0.1:8088/og/), not on og-api's port directly"
            )
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def api_base_url() -> str:
    """Base URL of the `og-api` process. Overridable via `OG_API_BASE_URL` for tests/dev; defaults to the
    loopback address `[api].bind_host`/`bind_port` resolve to in production (02b S1.4)."""
    return os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL)


def _response_detail(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


async def get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """GET `path` (e.g. `/og/api/health`) from the API and return the parsed JSON body.

    Raises `ApiUnavailable` on any transport error, timeout, or non-2xx response -- callers must treat
    that as "no data yet," never let it propagate to a bare 500.
    """
    url = f"{api_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(url, params=params, headers=_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise ApiUnavailable(
            f"GET {path} failed: {exc}",
            status_code=exc.response.status_code,
            detail=_response_detail(exc.response),
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"GET {path} failed: {exc}") from exc


def _identity_headers(remote_user: str | None) -> dict[str, str] | None:
    """The forwarded identity plus the proxy secret the API requires before it believes it
    (`opengrid.api.auth.proxy_authenticated`). `remote_user` must already be the proxy-verified identity
    (`opengrid.ui.role.remote_user`), so the UI never vouches for an identity Apache did not assert."""
    if not remote_user:
        return None
    return {"X-Remote-User": remote_user, "X-OG-Proxy-Auth": os.environ.get(PROXY_SECRET_ENV, "")}


async def post_json(
    path: str,
    payload: dict[str, Any],
    *,
    remote_user: str | None = None,
    timeout_s: float | None = None,
) -> Any:
    """POST `path` (e.g. `/og/api/safestop`) with a JSON `payload` and return the parsed JSON body.
    `remote_user` forwards the Apache-authenticated identity (`X-Remote-User`) to the API, which needs it
    to tell two operators apart (the two-person stop release).

    Shares `get_json`'s `ApiUnavailable` contract (BUILD.md code-review round: this used to be a private
    `_post_json` copy in `opengrid.ui.routes.billing_audit`, plus a second bare `httpx.AsyncClient` for
    the CSV relay in the same module -- both now go through this one shared client).

    `timeout_s` overrides the default for a call the API itself holds open (e.g. the safe-stop release
    approval, which waits up to 10 s for the guardian's signed release)."""
    url = f"{api_base_url()}{path}"
    headers = _identity_headers(remote_user)
    try:
        async with httpx.AsyncClient(timeout=timeout_s or _POST_TIMEOUT_S) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise ApiUnavailable(
            f"POST {path} failed: {exc}",
            status_code=exc.response.status_code,
            detail=_response_detail(exc.response),
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"POST {path} failed: {exc}") from exc


async def delete_json(path: str, *, remote_user: str | None = None) -> Any:
    """DELETE `path` through the same authenticated UI-to-API boundary as `post_json`."""
    url = f"{api_base_url()}{path}"
    headers = _identity_headers(remote_user)
    try:
        async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
            response = await client.delete(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise ApiUnavailable(
            f"DELETE {path} failed: {exc}",
            status_code=exc.response.status_code,
            detail=_response_detail(exc.response),
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"DELETE {path} failed: {exc}") from exc


async def get_bytes(path: str, *, params: dict[str, Any] | None = None) -> bytes:
    """GET `path` and return the raw response body (e.g. a CSV export the API formats itself) -- the one
    non-JSON shape this client needs to relay unchanged. Same `ApiUnavailable` contract as `get_json`."""
    url = f"{api_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
            response = await client.get(url, params=params, headers=_headers())
            response.raise_for_status()
            return response.content
    except httpx.HTTPStatusError as exc:
        raise ApiUnavailable(
            f"GET {path} failed: {exc}",
            status_code=exc.response.status_code,
            detail=_response_detail(exc.response),
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"GET {path} failed: {exc}") from exc
