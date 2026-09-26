"""02b S2.4 NWS client: grid-point resolution (pinned + looked-up), hourly forecast with
If-Modified-Since, and the 304 Not Modified path."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.http_client import FeedHttpError
from opengrid.feeds.nws import GridPoint, NwsClient

RECORDED_AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

FORECAST_PAYLOAD = {
    "properties": {
        "updated": "2026-09-26T17:00:00+00:00",
        "periods": [
            {
                "number": 1,
                "startTime": "2026-09-26T18:00:00-05:00",
                "temperature": 91,
                "dewpoint": {"value": 21.1},
                "skyCover": 20,
            }
        ],
    }
}


def _client(handler: httpx.MockTransport) -> NwsClient:
    return NwsClient(
        base_url="http://test/nws",
        user_agent="OpenGrid-Test (ops@example.com)",
        http_client=httpx.AsyncClient(transport=handler),
    )


async def test_resolve_grid_point_pinned_skips_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("pinned grid point must not make an HTTP call")

    client = _client(httpx.MockTransport(handler))
    grid_point = await client.resolve_grid_point(pinned="EWX/156,91")
    assert grid_point == GridPoint(office="EWX", x=156, y=91)


async def test_resolve_grid_point_looks_up_and_caches() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["User-Agent"]
        return httpx.Response(200, json={"properties": {"gridId": "EWX", "gridX": 156, "gridY": 91}})

    client = _client(httpx.MockTransport(handler))
    first = await client.resolve_grid_point(lat=29.4, lon=-98.5)
    second = await client.resolve_grid_point(lat=29.4, lon=-98.5)
    assert first == second == GridPoint(office="EWX", x=156, y=91)
    assert calls["n"] == 1  # cached after the first lookup


async def test_resolve_grid_point_requires_pinned_or_latlon() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="pinned"):
        await client.resolve_grid_point()


async def test_hourly_forecast_success_sets_if_modified_since_next_time() -> None:
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json=FORECAST_PAYLOAD)

    client = _client(httpx.MockTransport(handler))
    grid_point = GridPoint(office="EWX", x=156, y=91)

    rows = await client.hourly_forecast(grid_point, recorded_at=RECORDED_AT)
    assert rows is not None
    assert len(rows) == 3
    assert "If-Modified-Since" not in seen_headers[0]

    await client.hourly_forecast(grid_point, recorded_at=RECORDED_AT)
    assert "if-modified-since" in seen_headers[1]


async def test_hourly_forecast_304_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    client = _client(httpx.MockTransport(handler))
    result = await client.hourly_forecast(GridPoint(office="EWX", x=156, y=91), recorded_at=RECORDED_AT)
    assert result is None


async def test_hourly_forecast_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(FeedHttpError):
        await client.hourly_forecast(GridPoint(office="EWX", x=156, y=91), recorded_at=RECORDED_AT)


async def test_hourly_forecast_accepts_update_time_instead_of_updated() -> None:
    """Live api.weather.gov payloads (2026-09) carry `updateTime`/`generatedAt`, not `updated`."""
    import copy

    payload = copy.deepcopy(FORECAST_PAYLOAD)
    payload["properties"]["updateTime"] = payload["properties"].pop("updated")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client(httpx.MockTransport(handler))
    obs = await client.hourly_forecast(GridPoint(office="EWX", x=156, y=91), recorded_at=RECORDED_AT)
    assert obs
    assert client._last_updated == datetime.fromisoformat("2026-09-26T17:00:00+00:00")
