"""SSE streams (02b S7.2): role enforcement, and that each stream route returns a properly configured
`EventSourceResponse` whose first frame carries real JSON data.

These call the route coroutines directly with a fake `Request` rather than driving them through
`TestClient`'s streaming transport: an ASGI streaming response's background ping/disconnect-listener
tasks are awkward to tear down deterministically from a synchronous test client (a client that stops
reading early does not reliably deliver `http.disconnect`), so exercising the actual infinite generator
over a real transport is left to `tests/e2e` (02b's `ui_latency.py`-style click-to-photon check) rather
than risking a hung unit-test run here.
"""

from __future__ import annotations

import json

import pytest

from opengrid.api.routers import dispatch, fleet, health


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    """Just enough of `starlette.Request` for `sse.poll_stream`: headers (unused here) and
    `is_disconnected()`, which flips to `True` after the first check so the generator yields exactly
    one frame and returns -- no background task lingers past the test."""

    def __init__(self) -> None:
        self.client = _FakeClient()
        self._checked = False

    async def is_disconnected(self) -> bool:
        if not self._checked:
            self._checked = True
            return False
        return True


@pytest.mark.parametrize(
    ("router_module", "route_name"),
    [
        (health, "stream_health"),
        (health, "stream_alerts"),
        (fleet, "stream_fleet"),
        (dispatch, "stream_dispatch"),
        (dispatch, "stream_control_room"),
    ],
)
async def test_stream_route_emits_one_json_frame(router_module, route_name, fake_store, fake_config) -> None:
    route = getattr(router_module, route_name)
    request = _FakeRequest()
    response = await route(request, store=fake_store, cfg=fake_config, _identity=None)
    assert response.media_type == "text/event-stream"

    frames = [chunk async for chunk in response.body_iterator]
    assert frames, "expected at least one SSE frame before is_disconnected() flipped true"
    data_frames = [f for f in frames if isinstance(f, dict) and f.get("event") == "message"]
    assert data_frames
    payload = json.loads(data_frames[0]["data"])
    assert isinstance(payload, dict)


def test_stream_requires_viewer_role(client) -> None:
    resp = client.get("/og/api/stream/health")
    assert resp.status_code == 401


def test_stream_fleet_requires_viewer_role(client) -> None:
    resp = client.get("/og/api/fleet/stream")
    assert resp.status_code == 401
