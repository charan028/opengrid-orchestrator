"""Playwright fixtures for the UI end-to-end walkthrough (WP U1).

Two modes, chosen by `OG_UI_BASE_URL`:

* unset (default, runs anywhere today): a uvicorn server is started in a background thread on a free
  loopback port, serving a bare `FastAPI()` app with `opengrid.ui.build_router()` mounted at `/og`. The
  UI's only outbound boundary, `opengrid.ui.api_client.get_json`/`post_json`, is monkeypatched to serve
  the recorded fixtures in `orchestrator/tests/unit/ui/fixtures/` -- the same technique
  `orchestrator/tests/unit/ui/conftest.py` uses, but with one complete path map covering all 7 screens
  and both two-step flows. `/og/api/stream/*` is stubbed with a keepalive-only SSE response so the live
  screens' header badge stays deterministic; it never emits a data message (see
  `test_polling_and_live.py` for the live-stack-only update test).
* set (e.g. `http://localhost:8080`): the tests run against that dev stack unchanged.

Roles: the UI reads `X-OG-Role` (`opengrid.ui.role`); `operator_page`/`viewer_page` set it through the
browser context's `extra_http_headers`, so every document, htmx and EventSource request carries it.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from playwright.sync_api import Browser, Page

LIVE_BASE_URL_ENV = "OG_UI_BASE_URL"
ROLE_HEADER = "X-OG-Role"
# `opengrid.ui.role` trusts `X-Remote-User` only when the request also carries the proxy secret that
# Apache (or the dev proxy) injects as `X-OG-Proxy-Auth` (`opengrid.api.auth.proxy_authenticated`), and
# maps the identity to a role through `[api.roles]` on `app.state.config`. Fixture mode sets both.
PROXY_SECRET = "e2e-proxy-secret"  # noqa: S105 -- a test value, not a secret
PROXY_AUTH_HEADER = "X-OG-Proxy-Auth"
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "orchestrator" / "tests" / "unit" / "ui" / "fixtures"
_SERVER_READY_TIMEOUT_S = 15.0
_SERVER_STOP_TIMEOUT_S = 5.0
_MAP_TILE_URL_GLOB = "**/tile.openstreetmap.org/**"

SAFESTOP_PROPOSAL_ID = "11111111-1111-1111-1111-111111111111"
COMMAND_PROPOSAL_ID = "33333333-3333-3333-3333-333333333333"


def live_base_url() -> str | None:
    """The dev-stack URL when the suite runs against a real system, else None (fixture mode)."""
    value = os.environ.get(LIVE_BASE_URL_ENV)
    return value.rstrip("/") if value else None


def _load(name: str) -> Any:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def _get_responses() -> dict[str, Any]:
    """GET path -> fixture body, one entry per `get_json` call the 7 screens make."""
    hub_detail = _load("hub_detail.json")
    # the dispatch route reads reservations/grants/capacity *and* commitments off one timeline body
    timeline = {**_load("dispatch_ledger_timeline.json"), **_load("dispatch_commitments.json")}
    return {
        "/og/api/health": _load("health.json"),
        "/og/api/fleet/hubs": _load("hubs.json"),
        "/og/api/fleet/hubs/hub-0001": hub_detail,
        "/og/api/fleet/hubs/hub-0002": {**hub_detail, "hub_id": "hub-0002"},
        "/og/api/dispatch/opportunities": _load("dispatch_obligations.json"),
        "/og/api/dispatch/plan/latest": _load("dispatch_plan.json"),
        "/og/api/ledger/BANK-0001/timeline": timeline,
        "/og/api/markets/series": _load("markets_series_price.json"),
        "/og/api/forecast": _load("markets_forecast.json"),
        "/og/api/profitability/summary": _load("profitability_summary.json"),
        "/og/api/billing/invoice-lines": _load("billing_invoice_lines.json"),
        "/og/api/trace/events": _load("billing_trace_events.json"),
    }


def _post_responses() -> dict[str, Any]:
    """POST path -> fixture body for the propose/confirm flows, alert ack and chain verify."""
    return {
        "/og/api/safestop": _load("fleet_safestop_propose.json"),
        f"/og/api/safestop/{SAFESTOP_PROPOSAL_ID}/confirm": _load("fleet_safestop_confirm.json"),
        "/og/api/fleet/command": _load("fleet_command_propose.json"),
        f"/og/api/fleet/command/{COMMAND_PROPOSAL_ID}/confirm": _load("fleet_command_confirm_pass.json"),
        "/og/api/alerts/7/ack": _load("alert_ack.json"),
        "/og/api/trace/verify": {"passed": True, "checked": 12, "first_broken": None},
    }


def _route_modules() -> list[ModuleType]:
    import opengrid.ui.api_client as api_client
    import opengrid.ui.routes.billing_audit as billing_audit
    import opengrid.ui.routes.control_room as control_room
    import opengrid.ui.routes.dispatch as dispatch
    import opengrid.ui.routes.fleet as fleet
    import opengrid.ui.routes.health as health
    import opengrid.ui.routes.markets as markets
    import opengrid.ui.routes.profitability as profitability

    return [api_client, billing_audit, control_room, dispatch, fleet, health, markets, profitability]


def _install_fake_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `get_json`/`post_json` on the client module and on every route module that imported them
    by name (same caveat as the unit conftest)."""
    from opengrid.ui.api_client import ApiUnavailable

    gets, posts = _get_responses(), _post_responses()

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path not in gets:
            raise ApiUnavailable(f"no fixture registered for GET {path}")
        return gets[path]

    async def fake_post_json(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        if path not in posts:
            raise ApiUnavailable(f"no fixture registered for POST {path}")
        return posts[path]

    for module in _route_modules():
        for name, fake in (("get_json", fake_get_json), ("post_json", fake_post_json)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fake)


def _build_app(ready: threading.Event) -> FastAPI:
    import opengrid.ui as ui

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ready.set()
        yield

    from opengrid.api.auth import PROXY_SECRET_ENV
    from opengrid.platform.config import Config

    os.environ.setdefault(PROXY_SECRET_ENV, PROXY_SECRET)
    app = FastAPI(lifespan=lifespan)
    app.state.config = Config({"api": {"roles": {"operator": ["operator"], "viewer": ["viewer"]}}})
    app.include_router(ui.build_router(), prefix="/og")

    @app.get("/og/api/stream/{name}")
    async def keepalive_stream(name: str) -> StreamingResponse:
        # ponytail: keepalive only, no data frames -- enough to keep the "live" badge deterministic
        async def frames() -> AsyncIterator[bytes]:
            yield b": keepalive\n\n"
            await asyncio.Event().wait()

        return StreamingResponse(frames(), media_type="text/event-stream")

    return app


@pytest.fixture(scope="session")
def ui_base_url() -> Iterator[str]:
    """Base URL of the system under test: `OG_UI_BASE_URL`, or the in-process fixture server."""
    live = live_base_url()
    if live:
        yield live
        return

    monkeypatch = pytest.MonkeyPatch()
    _install_fake_api(monkeypatch)
    ready = threading.Event()
    # bind + listen before uvicorn starts so the port accepts connections (queued) from the first request
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = uvicorn.Config(_build_app(ready), log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="og-ui-e2e")
    thread.start()
    assert ready.wait(_SERVER_READY_TIMEOUT_S), "fixture server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(_SERVER_STOP_TIMEOUT_S)
        monkeypatch.undo()


def _role_page(browser: Browser, base_url: str, role: str) -> Iterator[Page]:
    headers = {ROLE_HEADER: role, "X-Remote-User": role}
    if not live_base_url():
        headers[PROXY_AUTH_HEADER] = os.environ.get("OG_API_PROXY_SECRET", PROXY_SECRET)
    context = browser.new_context(base_url=base_url, extra_http_headers=headers)
    page = context.new_page()
    page.route(_MAP_TILE_URL_GLOB, lambda route: route.abort())  # map tiles add nothing to the assertions
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def operator_page(browser: Browser, ui_base_url: str) -> Iterator[Page]:
    yield from _role_page(browser, ui_base_url, "operator")


@pytest.fixture
def viewer_page(browser: Browser, ui_base_url: str) -> Iterator[Page]:
    yield from _role_page(browser, ui_base_url, "viewer")
