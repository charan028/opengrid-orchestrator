"""Shared HTTP call-with-retry helper for feeds' three source clients (02b S2.5-2.6).

Idempotent GETs only, capped exponential backoff with full jitter (base 1s, cap 8s, max 2 retries),
honoring `Retry-After` on 429. Never retried: 400, 403, 404, 422, and 401 (callers handle 401 themselves
via re-authentication/key-rotation, one forced retry, before this helper is invoked again).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

DEFAULT_TIMEOUT_S = 10.0
RETRY_BASE_S = 1.0
RETRY_CAP_S = 8.0
MAX_RETRIES = 2
NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 422})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class FeedHttpError(Exception):
    """Raised when a call exhausts its retries or hits a non-retryable status."""

    status_code: int | None
    message: str

    def __str__(self) -> str:
        return self.message


def _backoff_delay_s(attempt: int, *, rng: random.Random) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    ceiling = min(RETRY_CAP_S, RETRY_BASE_S * (2**attempt))
    return rng.uniform(0, ceiling)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_retries: int = MAX_RETRIES,
    rng: random.Random | None = None,
) -> httpx.Response:
    """GET (or other idempotent method) with capped exponential backoff + jitter.

    `params` is the query string; `data` (when given) is sent as a form-encoded body -- used only for
    the ERCOT ROPC token POST, which is idempotent in effect (re-authenticating twice is harmless).
    Raises `FeedHttpError` on a non-retryable status or once retries are exhausted. Network-level
    errors (timeout, connect error) are treated as retryable, same as 5xx.
    """
    rng = rng or random.Random()  # noqa: S311 -- jitter timing, not security-sensitive
    last_error: FeedHttpError | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.request(
                method, url, headers=headers, params=params, data=data, timeout=timeout_s
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = FeedHttpError(None, f"{method} {url} failed: {exc}")
        else:
            if response.status_code < 400:
                return response
            if response.status_code in NON_RETRYABLE_STATUS:
                raise FeedHttpError(response.status_code, f"{method} {url} -> {response.status_code}")
            if response.status_code not in RETRYABLE_STATUS:
                raise FeedHttpError(response.status_code, f"{method} {url} -> {response.status_code}")
            last_error = FeedHttpError(response.status_code, f"{method} {url} -> {response.status_code}")
            if attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    with_jitter_delay = _parse_retry_after(retry_after)
                    await asyncio.sleep(with_jitter_delay)
                    continue

        if attempt < max_retries:
            await asyncio.sleep(_backoff_delay_s(attempt, rng=rng))

    raise last_error if last_error is not None else FeedHttpError(None, f"{method} {url} failed")


def _parse_retry_after(value: str) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        return RETRY_CAP_S
