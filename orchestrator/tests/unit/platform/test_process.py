"""PLAT-006: `opengrid.platform.process.run_forever` -- a tick exception is logged and the loop
continues; `asyncio.CancelledError` propagates and stops the loop."""

from __future__ import annotations

import asyncio

import pytest

from opengrid.platform import process as process_module
from opengrid.platform.process import run_forever


async def test_tick_exception_is_logged_and_loop_continues(caplog):
    calls = 0
    stop_after = 3

    async def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == stop_after:
            raise asyncio.CancelledError
        raise RuntimeError("boom")

    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        await run_forever(tick, interval_s=0.0, process_name="test-proc")

    assert calls == stop_after
    assert any("tick failed" in r.message for r in caplog.records)


async def test_cancelled_error_propagates_and_stops_loop():
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_forever(tick, interval_s=0.0, process_name="test-proc")
    assert calls == 1


async def test_stop_event_ends_loop_cleanly():
    """A stop request (what the SIGTERM/SIGINT handler does) ends run_forever without raising, after
    the in-flight tick finishes."""
    calls = 0
    stop_event_holder: dict[str, asyncio.Event] = {}
    original_event = asyncio.Event

    def capturing_event(*args, **kwargs):
        ev = original_event(*args, **kwargs)
        stop_event_holder.setdefault("event", ev)
        return ev

    async def tick() -> None:
        nonlocal calls
        calls += 1
        ev = stop_event_holder.get("event")
        if ev is not None:
            ev.set()

    process_module.asyncio.Event = capturing_event  # type: ignore[method-assign]
    try:
        await run_forever(tick, interval_s=10.0, process_name="test-proc")
    finally:
        process_module.asyncio.Event = original_event  # type: ignore[method-assign]
    assert calls == 1
