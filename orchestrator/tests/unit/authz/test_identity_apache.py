"""`ApacheProxyIdentity` delegates to `opengrid.api.auth.verified_remote_user` unmodified."""

from __future__ import annotations

from starlette.requests import Request

from opengrid.authz.identity import ApacheProxyIdentity


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def test_resolves_the_remote_user_when_proxy_authenticated(monkeypatch) -> None:
    from opengrid.api.auth import PROXY_SECRET_ENV

    monkeypatch.setenv(PROXY_SECRET_ENV, "shh")
    provider = ApacheProxyIdentity()
    request = _request({"x-remote-user": "operator", "x-og-proxy-auth": "shh"})
    assert provider.identify(request) == "operator"


def test_none_without_the_proxy_secret(monkeypatch) -> None:
    from opengrid.api.auth import PROXY_SECRET_ENV

    monkeypatch.setenv(PROXY_SECRET_ENV, "shh")
    provider = ApacheProxyIdentity()
    request = _request({"x-remote-user": "operator"})
    assert provider.identify(request) is None
