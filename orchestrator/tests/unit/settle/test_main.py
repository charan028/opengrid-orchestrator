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


async def test_a_job_that_stops_progressing_is_reported_stalled() -> None:
    """Health now runs inside og-settle and reports "settle" ok for itself, so a hung or always-failing
    settlement job would be invisible through the heartbeat; the runner tracks each job's last success."""
    clock = _Clock()
    hang = asyncio.Event()

    async def settle() -> None:
        if clock.now > 0:
            await hang.wait()  # second run never finishes

    runner = JobRunner([("settle", Cadence(60.0, clock=clock), settle)], clock=clock)
    await runner.run_due()
    await asyncio.sleep(0)
    assert runner.stalled({"settle": 180.0}) == {}

    clock.now = 60.0
    await runner.run_due()
    await asyncio.sleep(0)
    clock.now = 170.0
    assert runner.stalled({"settle": 180.0}) == {}
    clock.now = 200.0
    assert runner.stalled({"settle": 180.0}) == {"settle": 200.0}
    hang.set()
    await runner.stop()


async def test_an_always_failing_job_is_reported_stalled() -> None:
    clock = _Clock()

    async def failing() -> None:
        raise RuntimeError("settle backend down")

    runner = JobRunner([("settle", Cadence(60.0, clock=clock), failing)], clock=clock)
    for t in (0.0, 60.0, 120.0, 180.0, 240.0):
        clock.now = t
        await runner.run_due()
        await asyncio.sleep(0)
    assert runner.stalled({"settle": 180.0}) == {"settle": 240.0}
    await runner.stop()


async def test_stall_watch_adopts_alerts_from_a_previous_process() -> None:
    """Health no longer clears foreign rules, so an ALR-SETTLE-STALLED left open by a previous og-settle
    must be cleared by this one once the job progresses -- and not raised twice while it is still stalled."""
    from opengrid.settle.main import StallWatch

    stalled: dict[str, float] = {}
    raised: list[object] = []
    cleared: list[int] = []

    async def _raise(finding) -> int:
        raised.append(finding)
        return 99

    async def _clear(alert_id: int) -> None:
        cleared.append(alert_id)

    async def _find_open() -> dict[str, int]:
        return {"settle": 7}

    healthy = StallWatch(lambda: dict(stalled), raise_alert=_raise, clear_alert=_clear, find_open=_find_open)
    await healthy.check()
    assert cleared == [7] and raised == []

    stalled["settle"] = 400.0
    still = StallWatch(lambda: dict(stalled), raise_alert=_raise, clear_alert=_clear, find_open=_find_open)
    await still.check()
    assert raised == []  # adopted, not duplicated
    stalled.clear()
    await still.check()
    assert cleared == [7, 7]


async def test_asset_drift_job_runs_the_assets_sweep(monkeypatch) -> None:
    import opengrid.settle.main as settle_main
    from opengrid.assets.runner import RunOnceResult

    calls: list[object] = []

    async def _sweep(service, *, now):
        calls.append(service)
        return RunOnceResult(evaluated=3, calibrations_requested=1, work_orders_opened=0, errors=0)

    monkeypatch.setattr(settle_main, "run_asset_drift_sweep", _sweep)
    service = object()
    await settle_main.make_asset_drift_job(service)()  # type: ignore[arg-type]
    assert calls == [service]


async def test_stall_watch_raises_once_per_episode_and_clears_on_recovery() -> None:
    from opengrid.settle.main import StallWatch

    stalled: dict[str, float] = {"settle": 200.0}
    raised: list[tuple[str, dict]] = []
    cleared: list[int] = []

    async def _raise(finding) -> int:
        raised.append((finding.rule, finding.detail))
        return 41

    async def _clear(alert_id: int) -> None:
        cleared.append(alert_id)

    watch = StallWatch(lambda: dict(stalled), raise_alert=_raise, clear_alert=_clear)
    await watch.check()
    await watch.check()  # same episode: not raised again
    assert raised == [
        ("ALR-SETTLE-STALLED", {"process": "settle", "job": "settle", "since_success_s": 200.0})
    ]

    stalled.clear()
    await watch.check()
    assert cleared == [41]

    stalled["settle"] = 300.0
    await watch.check()  # a new episode raises again
    assert len(raised) == 2
