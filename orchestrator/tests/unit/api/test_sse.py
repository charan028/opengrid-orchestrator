"""SSE streams (02b S7.2): content type, comment heartbeat wiring, and a first data frame. Each
`poll_stream` generator (`opengrid.api.sse`) emits its first frame immediately (no initial sleep), so
these tests never wait a full cadence interval."""

from __future__ import annotations

import json

import pytest

from .conftest import VIEWER_HEADERS

STREAM_PATHS = [
    "/og/api/stream/health",
    "/og/api/stream/alerts",
    "/og/api/fleet/stream",
    "/og/api/stream/dispatch",
    "/og/api/stream/control-room",
]


@pytest.mark.parametrize("path", STREAM_PATHS)
def test_stream_emits_json_data_event(client, path: str) -> None:
    with client.stream("GET", path, headers=VIEWER_HEADERS) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        data_line = None
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data_line = line
                break
        assert data_line is not None
        payload = json.loads(data_line.removeprefix("data:").strip())
        assert isinstance(payload, dict)


def test_stream_requires_viewer_role(client) -> None:
    resp = client.get("/og/api/stream/health")
    assert resp.status_code == 401
