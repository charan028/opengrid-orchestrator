"""02b S2.6 retry policy: idempotent GETs, capped exponential backoff + jitter, honors `Retry-After`
on 429, never retries 400/401/403/404/422."""

from __future__ import annotations

import random

import httpx
import pytest

from opengrid.feeds.http_client import FeedHttpError, request_with_retry


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_success_on_first_try() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "http://test/x")
    assert response.status_code == 200
    assert calls["n"] == 1


async def test_5xx_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "http://test/x",
            max_retries=2,
            rng=random.Random(1),  # noqa: S311 - test jitter, not crypto
        )
    assert response.status_code == 200
    assert calls["n"] == 3


async def test_5xx_exhausts_retries_and_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(FeedHttpError) as exc_info:
            await request_with_retry(
                client,
                "GET",
                "http://test/x",
                max_retries=2,
                rng=random.Random(1),  # noqa: S311
            )
    assert exc_info.value.status_code == 500


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_non_retryable_status_raises_immediately(status: int) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(FeedHttpError) as exc_info:
            await request_with_retry(client, "GET", "http://test/x", max_retries=2)
    assert exc_info.value.status_code == status
    assert calls["n"] == 1  # no retries spent on a non-retryable status


async def test_429_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("opengrid.feeds.http_client.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        response = await request_with_retry(client, "GET", "http://test/x", max_retries=1)
    assert response.status_code == 200
    assert sleeps == [3.0]
