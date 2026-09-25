"""02b S2.3 EIA client: standby fallback for system load."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.eia import EiaClient
from opengrid.feeds.http_client import FeedHttpError

RECORDED_AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def _client(handler: httpx.MockTransport) -> EiaClient:
    return EiaClient(
        base_url="http://test/eia",
        api_key_env="TEST_EIA_KEY",
        respondent="ERCO",
        http_client=httpx.AsyncClient(transport=handler),
    )


async def test_hourly_demand_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EIA_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "test-key"
        assert request.url.params["facets[respondent][]"] == "ERCO"
        return httpx.Response(
            200,
            json={"response": {"data": [{"period": "2026-09-26T18", "value": "52104"}]}},
        )

    client = _client(httpx.MockTransport(handler))
    rows = await client.hourly_demand(recorded_at=RECORDED_AT)
    assert len(rows) == 1
    assert rows[0].source == "EIA"
    assert rows[0].quality == "ESTIMATED"


async def test_hourly_demand_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EIA_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(FeedHttpError):
        await client.hourly_demand(recorded_at=RECORDED_AT)
