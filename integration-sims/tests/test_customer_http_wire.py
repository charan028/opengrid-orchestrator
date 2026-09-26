"""`HttpxTransport` on the wire: requests go to the Apache-fronted host with only HTTP Basic
credentials -- never `X-Remote-User` or `X-OG-Proxy-Auth` (Apache sets both) -- and a base URL
ending in `/og/api` does not double the path prefix."""

from __future__ import annotations

import httpx
import pytest

from ogsim.customer.api_client import CustomerApiClient, HttpxTransport

AUTH = ("og-cust-dc", "pw")


def _recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"obligations": [], "invoices": [], "contracts": []})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "base", ["https://base.example", "https://base.example/og/api", "https://base.example/"]
)
async def test_requests_carry_basic_auth_only_and_the_right_path(base: str) -> None:
    seen: list[httpx.Request] = []
    client = CustomerApiClient(HttpxTransport(base, transport=_recording_transport(seen)), AUTH)
    await client.get_obligations()
    await client.cancel_obligation("o1")
    assert [str(r.url) for r in seen] == [
        "https://base.example/og/api/customer/obligations",
        "https://base.example/og/api/customer/obligations/o1/cancel",
    ]
    for request in seen:
        assert request.headers["authorization"].startswith("Basic ")
        assert "x-og-proxy-auth" not in request.headers
        assert "x-remote-user" not in request.headers


def test_relative_base_url_is_refused() -> None:
    with pytest.raises(ValueError):
        HttpxTransport("/og/api")
