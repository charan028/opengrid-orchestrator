"""og-settle process loop: one `run_forever` driving several cadences (regression for the live
2026-09-26 finding that three concurrent `run_forever` loops left SIGTERM handled by only one of them,
so systemd SIGKILLed og-settle after 90 s on every deploy), with each job in its own task so a slow
job never delays the heartbeat."""

from __future__ import annotations

import asyncio

from opengrid.platform.process import Cadence
from opengrid.settle.main import JobRunner


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_cadence_is_due_first_then_once_per_interval() -> None:
    clock = _Clock()
    cadence = Cadence(60.0, clock=clock)

    assert cadence.due()
    clock.now = 59.0
    assert not cadence.due()
    clock.now = 60.0
    assert cadence.due()
    clock.now = 61.0
    assert not cadence.due()


async def test_a_slow_job_never_delays_the_heartbeat_and_is_not_restarted_while_running() -> None:
    """Live 2026-09-26: run sequentially, a health pass over 2,000 hubs on a slow disk held the loop and
    og-settle's heartbeat went 52 s stale."""
    clock = _Clock()
    release_slow = asyncio.Event()
    beats: list[float] = []
    slow_starts: list[float] = []

    async def heartbeat() -> None:
        beats.append(clock.now)

    async def slow() -> None:
        slow_starts.append(clock.now)
        await release_slow.wait()

    runner = JobRunner(
        [("heartbeat", Cadence(5.0, clock=clock), heartbeat), ("slow", Cadence(5.0, clock=clock), slow)]
    )
    for tick in range(3):
        clock.now = tick * 5.0
        await runner.run_due()
        await asyncio.sleep(0)

    assert beats == [0.0, 5.0, 10.0]
    assert slow_starts == [0.0]  # still in flight: not started again
    release_slow.set()
    await runner.stop()


async def test_a_failing_job_is_isolated() -> None:
    clock = _Clock()
    ran: list[str] = []

    async def failing() -> None:
        ran.append("failing")
        raise RuntimeError("settle backend down")

    async def ok() -> None:
        ran.append("ok")

    runner = JobRunner(
        [("failing", Cadence(5.0, clock=clock), failing), ("ok", Cadence(5.0, clock=clock), ok)]
    )
    await runner.run_due()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert sorted(ran) == ["failing", "ok"]
    await runner.stop()
