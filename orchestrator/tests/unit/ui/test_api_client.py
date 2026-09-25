"""Tests for the shared `opengrid.ui.api_client` HTTP client (BUILD.md code-review round item 1/4):
`post_json` and `get_bytes` were added so `opengrid.ui.routes.billing_audit` could drop its private
`_post_json` copy and its second bare `httpx.AsyncClient` for the CSV relay. Uses `httpx.MockTransport`
to avoid a live `og-api` process, matching `get_json`'s own existing test-by-monkeypatch style but at the
transport layer since these tests exercise the client itself, not a caller."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opengrid.ui import api_client


def _client_with_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedAsyncClient)


async def test_post_json_returns_parsed_body_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(202, json={"proposal_id": "abc", "summary": "ok", "expires_in_s": 60.0})

    _client_with_transport(monkeypatch, handler)

    result = await api_client.post_json("/og/api/safestop", {"scope": "fleet", "reason": "x"})

    assert result == {"proposal_id": "abc", "summary": "ok", "expires_in_s": 60.0}


async def test_post_json_raises_api_unavailable_with_status_and_detail_on_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"outcome": "VETOED", "vetoed_rule_ids": ["R-1"]})

    _client_with_transport(monkeypatch, handler)

    with pytest.raises(api_client.ApiUnavailable) as excinfo:
        await api_client.post_json("/og/api/fleet/command/x/confirm", {})

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"outcome": "VETOED", "vetoed_rule_ids": ["R-1"]}


async def test_post_json_raises_api_unavailable_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _client_with_transport(monkeypatch, handler)

    with pytest.raises(api_client.ApiUnavailable) as excinfo:
        await api_client.post_json("/og/api/safestop", {})

    assert excinfo.value.status_code is None


async def test_get_bytes_returns_raw_body_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})

    _client_with_transport(monkeypatch, handler)

    content = await api_client.get_bytes("/og/api/billing/invoice-lines", params={"format": "csv"})

    assert content == b"a,b\n1,2\n"


async def test_get_bytes_raises_api_unavailable_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    _client_with_transport(monkeypatch, handler)

    with pytest.raises(api_client.ApiUnavailable) as excinfo:
        await api_client.get_bytes("/og/api/billing/invoice-lines")

    assert excinfo.value.status_code == 503
