"""Tests for ogsim.common.clock -- deterministic FakeClock, no wall sleeps."""

from __future__ import annotations

import asyncio

from ogsim.common.clock import FakeClock


def test_fake_clock_now_starts_at_given_value() -> None:
    clock = FakeClock(start=100.0)
    assert clock.now() == 100.0


def test_fake_clock_advance_moves_now() -> None:
    clock = FakeClock()
    clock.advance(5.0)
    assert clock.now() == 5.0


def test_fake_clock_sleep_releases_on_advance() -> None:
    clock = FakeClock()
    released = []

    async def waiter() -> None:
        await clock.sleep(10.0)
        released.append(True)

    async def scenario() -> None:
        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0)  # let the waiter register
        assert released == []
        clock.advance(5.0)
        await asyncio.sleep(0)
        assert released == []
        clock.advance(5.0)
        await task
        assert released == [True]

    asyncio.run(scenario())


def test_fake_clock_sleep_zero_returns_immediately() -> None:
    clock = FakeClock()

    async def scenario() -> None:
        await clock.sleep(0.0)

    asyncio.run(scenario())
