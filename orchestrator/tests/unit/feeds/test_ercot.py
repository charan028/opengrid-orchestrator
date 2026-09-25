"""02b S2.2/S2.6 ERCOT client: ROPC token flow and primary -> secondary key rotation on 401/403."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.ercot import ErcotClient
from opengrid.feeds.http_client import FeedHttpError

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

SPP_PAYLOAD = {
    "data": [["2026-09-26", "2026-09-26T18:00:00", "LZ_NORTH", "42.17"]],
    "fields": [
        {"name": "deliveryDate"},
        {"name": "deliveryDateTime"},
        {"name": "settlementPoint"},
        {"name": "settlementPointPrice"},
    ],
}


@pytest.fixture(autouse=True)
def _ercot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ERCOT_USER", "user@example.com")
    monkeypatch.setenv("TEST_ERCOT_PASSWORD", "hunter2")
    monkeypatch.setenv("TEST_ERCOT_KEY_PRIMARY", "primary-key")
    monkeypatch.setenv("TEST_ERCOT_KEY_SECONDARY", "secondary-key")


def _make_client(handler: httpx.MockTransport) -> ErcotClient:
    return ErcotClient(
        base_url="http://test/ercot",
        username_env="TEST_ERCOT_USER",
        password_env="TEST_ERCOT_PASSWORD",
        primary_key_env="TEST_ERCOT_KEY_PRIMARY",
        secondary_key_env="TEST_ERCOT_KEY_SECONDARY",
        http_client=httpx.AsyncClient(transport=handler),
        token_url="http://test/token",
    )


async def test_fetch_product_success_with_primary_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"access_token": "tok-1"})
        assert request.headers["Ocp-Apim-Subscription-Key"] == "primary-key"
        return httpx.Response(200, json=SPP_PAYLOAD)

    client = _make_client(httpx.MockTransport(handler))
    obs, events = await client.fetch_product("np6-905-cd", now=NOW)
    assert len(obs) == 1
    assert events == []
    assert client.active_key == "PRIMARY"


async def test_401_triggers_reauth_then_key_rotation_on_repeat_401() -> None:
    calls = {"data": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{calls['token']}"})
        calls["data"] += 1
        key = request.headers["Ocp-Apim-Subscription-Key"]
        if key == "primary-key":
            return httpx.Response(401)
        return httpx.Response(200, json=SPP_PAYLOAD)

    client = _make_client(httpx.MockTransport(handler))
    obs, events = await client.fetch_product("np6-905-cd", now=NOW)

    assert len(obs) == 1
    assert len(events) == 1
    assert events[0].from_key == "PRIMARY"
    assert events[0].to_key == "SECONDARY"
    assert client.active_key == "SECONDARY"
    assert calls["token"] == 2  # one forced re-authentication before rotation


async def test_key_rotation_is_sticky_across_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        key = request.headers["Ocp-Apim-Subscription-Key"]
        if key == "primary-key":
            return httpx.Response(401)
        return httpx.Response(200, json=SPP_PAYLOAD)

    client = _make_client(httpx.MockTransport(handler))
    await client.fetch_product("np6-905-cd", now=NOW)
    assert client.active_key == "SECONDARY"

    # A second call should go straight to the secondary key -- no further rotation event fires.
    _, second_events = await client.fetch_product("np6-905-cd", now=NOW)
    assert second_events == []
    assert client.active_key == "SECONDARY"


async def test_both_keys_failing_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(401)

    client = _make_client(httpx.MockTransport(handler))
    with pytest.raises(FeedHttpError):
        await client.fetch_product("np6-905-cd", now=NOW)
    assert client.active_key == "SECONDARY"  # rotation still happened before giving up


async def test_unknown_product_rejected() -> None:
    client = _make_client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="unknown ERCOT product"):
        await client.fetch_product("not-a-product", now=NOW)
