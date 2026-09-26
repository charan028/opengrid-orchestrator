"""U1 live-run defects: the UI's server-side API calls carried no `X-Remote-User` (every screen degraded
with a 401), and nothing in the deploy sets `X-OG-Role`, so the role must derive from the identity."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.api_client as api_client
import opengrid.ui.routes.health as health_route


def _request_with(headers: dict[str, str]) -> Request:
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


@pytest.mark.asyncio
async def test_get_json_forwards_bound_remote_user(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["remote_user"] = request.headers.get("X-Remote-User")
        seen["proxy_auth"] = request.headers.get("X-OG-Proxy-Auth")
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def patched(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(api_client.httpx, "AsyncClient", patched)
    monkeypatch.setenv("OG_API_PROXY_SECRET", "s3cret")
    token = api_client.bind_remote_user("operator")
    try:
        assert await api_client.get_json("/og/api/health") == {"ok": True}
    finally:
        api_client._remote_user.reset(token)
    assert seen["remote_user"] == "operator"
    assert seen["proxy_auth"] == "s3cret"

    seen.clear()
    await api_client.get_json("/og/api/health")
    assert seen["remote_user"] is None
    assert seen["proxy_auth"] is None


def test_ui_route_binds_remote_user_for_its_api_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        seen.append(api_client._remote_user.get())
        raise api_client.ApiUnavailable("stub")

    monkeypatch.setattr(health_route, "get_json", fake_get_json)
    monkeypatch.setenv("OG_API_PROXY_SECRET", "s3cret")
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    client = TestClient(app)
    proxied = {"X-Remote-User": "operator", "X-OG-Proxy-Auth": "s3cret"}
    assert client.get("/og/health", headers=proxied).status_code == 200
    assert seen and seen[0] == "operator"

    # Without Apache's secret (a local process on the loopback port) the identity is not forwarded.
    for spoofed in ({"X-Remote-User": "operator"}, {"X-Remote-User": "operator", "X-OG-Proxy-Auth": "no"}):
        seen.clear()
        assert client.get("/og/health", headers=spoofed).status_code == 200
        assert seen and seen[0] is None


def test_status_badge_shows_the_status_when_no_label_is_given() -> None:
    """Live run: every badge rendered without an explicit label read "None" (Jinja's `default` only
    replaces undefined, not None)."""
    from opengrid.ui.render import render_status_badge

    html = render_status_badge("GOOD")
    assert ">GOOD<" in html and "None" not in html
    assert ">stale<" in render_status_badge("stale", label=None)
    assert ">PASS<" in render_status_badge("ok", label="PASS")


def test_fleet_filter_inputs_are_blank_when_no_filter_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live run: the Zone/Bank inputs showed the literal text "None" (same `default` vs None trap)."""
    import opengrid.ui.routes.fleet as fleet_route

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        return {"items": []}

    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    app = FastAPI()
    app.include_router(ui.build_router(), prefix="/og")
    body = TestClient(app).get("/og/fleet").text
    assert 'name="q" value=""' in body and 'name="bank" value=""' in body
