"""02b S2.2/S2.6 ERCOT client: ROPC token flow and primary -> secondary key rotation on 401/403."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest

from opengrid.feeds.ercot import ErcotAuthError, ErcotClient
from opengrid.feeds.http_client import FeedHttpError
from opengrid.feeds.secrets import Secret

_PASSWORD = "hunter2"  # noqa: S105 -- a test fixture value, asserted to never leak (this file's own point)

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

SPP_PAYLOAD = {
    "data": [["2026-09-26", 1, 1, "LZ_NORTH", "LZ", 42.17, False]],
    "fields": [
        {"name": "deliveryDate"},
        {"name": "deliveryHour"},
        {"name": "deliveryInterval"},
        {"name": "settlementPoint"},
        {"name": "settlementPointType"},
        {"name": "settlementPointPrice"},
        {"name": "DSTFlag"},
    ],
}


@pytest.fixture(autouse=True)
def _ercot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ERCOT_USER", "user@example.com")
    monkeypatch.setenv("TEST_ERCOT_PASSWORD", _PASSWORD)
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
            body = request.content.decode()
            assert "response_type=id_token" in body
            return httpx.Response(200, json={"id_token": "tok-1"})
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
            return httpx.Response(200, json={"id_token": f"tok-{calls['token']}"})
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
            return httpx.Response(200, json={"id_token": "tok"})
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
            return httpx.Response(200, json={"id_token": "tok"})
        return httpx.Response(401)

    client = _make_client(httpx.MockTransport(handler))
    with pytest.raises(FeedHttpError):
        await client.fetch_product("np6-905-cd", now=NOW)
    assert client.active_key == "SECONDARY"  # rotation still happened before giving up


async def test_unknown_product_rejected() -> None:
    client = _make_client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError, match="unknown ERCOT product"):
        await client.fetch_product("not-a-product", now=NOW)


async def test_token_response_missing_id_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://test/token")
        return httpx.Response(200, json={"access_token": "wrong-field"})  # old `token` shape

    client = _make_client(httpx.MockTransport(handler))
    with pytest.raises(ErcotAuthError, match="id_token"):
        await client.fetch_product("np6-905-cd", now=NOW)


async def test_auth_failure_never_leaks_password(caplog: pytest.LogCaptureFixture) -> None:
    """A live ERCOT token rejection (e.g. `400 invalid_grant`) must never surface the password in the
    raised exception's message, its repr, or any log record (BUILD.md S5a/S6)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json=SPP_PAYLOAD)

    client = _make_client(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING), pytest.raises(ErcotAuthError) as exc_info:
        await client.fetch_product("np6-905-cd", now=NOW)

    assert _PASSWORD not in str(exc_info.value)
    assert _PASSWORD not in repr(exc_info.value)
    for record in caplog.records:
        assert _PASSWORD not in record.getMessage()
        assert _PASSWORD not in repr(record)


def test_secret_repr_and_str_never_show_the_value() -> None:
    secret = Secret(_PASSWORD)
    assert _PASSWORD not in repr(secret)
    assert _PASSWORD not in str(secret)
    assert secret.reveal() == _PASSWORD


async def test_energy_prices_outside_the_offer_floor_and_cap_are_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #43 A12: an SPP outside -$250..$5,000/MWh is a feed error. Both bounds themselves are
    real prices (kept); the rows beyond them are dropped at ingest and logged."""
    prices = [42.17, 5_000.0, 5_000.01, -250.0, -250.5]
    payload = {
        "data": [["2026-09-26", 1, i + 1, "LZ_NORTH", "LZ", p, False] for i, p in enumerate(prices[:4])]
        + [["2026-09-26", 2, 1, "LZ_NORTH", "LZ", prices[4], False]],
        "fields": SPP_PAYLOAD["fields"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"id_token": "tok-1"})
        return httpx.Response(200, json=payload)

    client = _make_client(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="opengrid.feeds.ercot"):
        obs, _events = await client.fetch_product("np6-905-cd", now=NOW)
    assert sorted(o.value for o in obs) == [-250.0, 42.17, 5_000.0]
    dropped = [r for r in caplog.records if "dropped at ingest" in r.getMessage()]
    assert sorted(r.__dict__["value"] for r in dropped) == [-250.5, 5_000.01]
