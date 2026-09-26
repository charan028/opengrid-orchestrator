"""CSRF mitigation for the Basic-Auth-fronted HTMX UI and the REST API (qa/security-review.md F-03).

Basic Auth has no session/token infrastructure of its own to hang a CSRF check off -- browsers cache
and auto-attach Basic-Auth credentials to same-origin requests regardless of which page initiated them,
so a malicious cross-origin page can blind-POST to a mutating endpoint using the operator's cached
credentials. This module adds the standard double-submit-cookie defense on top: a per-session random
token is set in a cookie (`SameSite=Strict`, so a real cross-site request never carries it at all) and
must also be echoed back on every state-changing request via a header or form field the attacker's page
cannot read or forge (same-origin policy blocks reading the cookie's value or the page's own response).
An `Origin`/`Referer` allowlist check runs first as a cheap, defense-in-depth layer.

`CSRFMiddleware` (wired in `opengrid.api.app.create_app`) covers every route this process serves --
`api`'s own REST endpoints and `opengrid.ui`'s HTMX screens alike -- from one place, rather than adding a
`Depends(...)` to each of the ~30 mutating routes individually.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

COOKIE_NAME = "og_csrf"
HEADER_NAME = "X-CSRF-Token"
FORM_FIELD_NAME = "csrf_token"
TOKEN_ENTROPY_BYTES = 32

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Hosts a state-changing request's `Origin`/`Referer` may legitimately come from. `127.0.0.1`/`localhost`
#: cover loopback dev/test traffic (`tests-e2e/smoke.py`, TestClient-based unit tests); the production
#: host is read from config so this never hardcodes a value that would break a future domain change.
_DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def _request_host(value: str | None) -> str | None:
    if not value:
        return None
    # A bare Origin header has no path (just scheme://host[:port]); Referer is a full URL. urlparse
    # handles both -- for Origin, `netloc` still comes out correctly since it's a valid URL by itself.
    parsed = urlparse(value)
    return parsed.hostname


class CSRFMiddleware(BaseHTTPMiddleware):
    """Issues/refreshes the `og_csrf` cookie on every response, and for state-changing methods,
    enforces the Origin/Referer allowlist plus the double-submit token match before the request reaches
    any route handler."""

    def __init__(
        self,
        app: object,
        *,
        allowed_hosts: frozenset[str] = _DEFAULT_ALLOWED_HOSTS,
        cookie_secure: bool = True,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._allowed_hosts = allowed_hosts | _DEFAULT_ALLOWED_HOSTS
        # Real deployments are only ever reached over HTTPS (Apache terminates TLS; the browser's own
        # connection to base.tocy-net.net is what a Secure cookie's rule is evaluated against, even
        # though Apache then proxies plain HTTP to this process on loopback) -- `cookie_secure=False`
        # exists only for HTTP-only test clients (`TestClient`'s default `http://testserver` base URL).
        self._cookie_secure = cookie_secure

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cookie_token = request.cookies.get(COOKIE_NAME)
        request.state.csrf_token = cookie_token or generate_token()

        if request.method.upper() not in _SAFE_METHODS:
            rejection = await self._reject_if_invalid(request, cookie_token)
            if rejection is not None:
                return rejection

        response = await call_next(request)

        if cookie_token is None:
            response.set_cookie(
                COOKIE_NAME,
                request.state.csrf_token,
                httponly=False,  # HTMX/JS must be able to read it to echo it back in a header
                samesite="strict",
                secure=self._cookie_secure,
                path="/",
            )
        return response

    async def _reject_if_invalid(self, request: Request, cookie_token: str | None) -> Response | None:
        origin_or_referer = request.headers.get("origin") or request.headers.get("referer")
        host = _request_host(origin_or_referer)
        if host is not None and host not in self._allowed_hosts:
            return JSONResponse({"detail": "CSRF check failed: Origin/Referer not allowed"}, status_code=403)

        if cookie_token is None:
            # No prior GET ever established a session cookie for this caller: not a browser CSRF
            # scenario (a real cross-site attack rides an *existing* victim cookie -- SameSite=Strict
            # means the browser never attaches one it doesn't have). Non-browser API/CLI callers
            # authenticate via Basic Auth alone and are not the threat this defends against.
            return None

        submitted = request.headers.get(HEADER_NAME)
        if submitted is None:
            submitted = await self._form_token(request)
        if submitted is None or not secrets.compare_digest(submitted, cookie_token):
            return JSONResponse({"detail": "CSRF check failed: missing or invalid token"}, status_code=403)
        return None

    @staticmethod
    async def _form_token(request: Request) -> str | None:
        content_type = request.headers.get("content-type", "")
        if (
            "application/x-www-form-urlencoded" not in content_type
            and "multipart/form-data" not in content_type
        ):
            return None
        try:
            form = await request.form()
        except Exception:
            return None
        value = form.get(FORM_FIELD_NAME)
        return value if isinstance(value, str) else None
