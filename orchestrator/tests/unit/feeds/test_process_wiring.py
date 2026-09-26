"""og-feeds process wiring: the heartbeat must survive a bad tick, and og-feeds must never install a
second `run_forever` signal handler on top of `opengrid.forecast`'s recompute cadence (02b S1.1-S1.3).

Both are live defects: the heartbeat was never written because every cycle's NWS `KeyError` raised
before the write, and og-feeds ignored SIGTERM because two `run_forever` loops in one process each
tried to register their own SIGTERM/SIGINT handler -- asyncio allows only one handler per signal, so
the second registration silently replaced the first's.
"""

from __future__ import annotations

import pytest

from opengrid.feeds import _new_http_client, _tick_with_heartbeat


class _BoomError(Exception):
    pass


async def test_heartbeat_written_even_when_run_cycle_raises() -> None:
    calls: list[str] = []

    async def run_cycle() -> None:
        calls.append("run_cycle")
        raise _BoomError("nws KeyError, or any other single-cycle failure")

    async def write_hb() -> None:
        calls.append("heartbeat")

    with pytest.raises(_BoomError):
        await _tick_with_heartbeat(run_cycle, write_hb, None)

    assert calls == ["run_cycle", "heartbeat"]  # heartbeat still ran despite the failure


async def test_extra_tick_skipped_when_run_cycle_raises() -> None:
    calls: list[str] = []

    async def run_cycle() -> None:
        raise _BoomError

    async def write_hb() -> None:
        calls.append("heartbeat")

    async def extra_tick() -> None:
        calls.append("extra_tick")

    with pytest.raises(_BoomError):
        await _tick_with_heartbeat(run_cycle, write_hb, extra_tick)

    assert calls == ["heartbeat"]  # extra_tick (forecast recompute) never ran this cycle


async def test_successful_cycle_runs_heartbeat_then_extra_tick_in_order() -> None:
    calls: list[str] = []

    async def run_cycle() -> None:
        calls.append("run_cycle")

    async def write_hb() -> None:
        calls.append("heartbeat")

    async def extra_tick() -> None:
        calls.append("extra_tick")

    await _tick_with_heartbeat(run_cycle, write_hb, extra_tick)

    assert calls == ["run_cycle", "heartbeat", "extra_tick"]


async def test_extra_tick_optional() -> None:
    async def run_cycle() -> None:
        return None

    async def write_hb() -> None:
        return None

    await _tick_with_heartbeat(run_cycle, write_hb, None)  # must not raise with no extra_tick


async def test_shared_http_client_follows_redirects() -> None:
    """`request_with_retry` treats any status under 400 -- including a 3xx -- as success and returns it
    as-is (it only branches on `NON_RETRYABLE_STATUS`/`RETRYABLE_STATUS`, both >= 400). Without
    `follow_redirects=True` on the client, a redirect from ERCOT/EIA/NWS would hand a caller a
    redirect's (empty/HTML) body instead of JSON -- an unhandled `JSONDecodeError`, not a clean
    `FeedHttpError`. httpx defaults `follow_redirects` to `False`, so this must be set explicitly."""
    async with _new_http_client() as client:
        assert client.follow_redirects is True
