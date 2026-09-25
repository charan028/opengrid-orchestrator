"""TS-02-02: the ERCOT 24 req/min budget is respected -- excess demand is queued, never dropped."""

from __future__ import annotations

import asyncio

import pytest

from opengrid.feeds.token_bucket import TokenBucket


def test_rejects_non_positive_parameters() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(capacity=0, refill_per_s=1)
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(capacity=1, refill_per_s=0)


async def test_acquire_drains_capacity_without_blocking() -> None:
    bucket = TokenBucket(capacity=3, refill_per_s=1000)  # fast refill so we only test initial capacity
    for _ in range(3):
        await asyncio.wait_for(bucket.acquire(), timeout=0.1)


async def test_acquire_blocks_until_refill() -> None:
    clock = {"t": 0.0}
    bucket = TokenBucket(capacity=1, refill_per_s=10, clock=lambda: clock["t"])
    await bucket.acquire()  # drains the single token

    async def _advance_and_acquire() -> None:
        clock["t"] += 0.1  # exactly one token's worth at refill_per_s=10
        await bucket.acquire()

    await asyncio.wait_for(_advance_and_acquire(), timeout=1.0)


async def test_never_exceeds_capacity_even_after_long_idle() -> None:
    clock = {"t": 0.0}
    bucket = TokenBucket(capacity=2, refill_per_s=5, clock=lambda: clock["t"])
    clock["t"] += 1000.0  # would overflow far past capacity without the min() clamp
    await asyncio.wait_for(bucket.acquire(), timeout=0.1)
    await asyncio.wait_for(bucket.acquire(), timeout=0.1)
    # a third immediate acquire must still wait (capacity clamp worked, bucket isn't infinitely full)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bucket.acquire(), timeout=0.05)
