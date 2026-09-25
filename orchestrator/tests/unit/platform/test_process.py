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

    with caplog.at_level("ERROR"):
        with pytest.raises(asyncio.CancelledError):
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


async def test_stop_event_ends_loop_cleanly(monkeypatch):
    """A signal-triggered stop (simulated directly here since signal handlers need a real loop/OS
    support that varies on Windows dev boxes) ends run_forever without raising."""
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1

    original_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        # Simulate the stop_event firing on the very first sleep window.
        coro.close()
        return None

    monkeypatch.setattr(process_module.asyncio, "wait_for", fake_wait_for)
    try:
        await run_forever(tick, interval_s=0.01, process_name="test-proc")
    finally:
        monkeypatch.setattr(process_module.asyncio, "wait_for", original_wait_for)
    assert calls >= 1
