"""og-settle process loop: one `run_forever` driving several cadences (regression for the live
2026-09-26 finding that three concurrent `run_forever` loops left SIGTERM handled by only one of them,
so systemd SIGKILLed og-settle after 90 s on every deploy)."""

from __future__ import annotations

from opengrid.settle.main import Cadence, run_due


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


async def test_run_due_runs_only_due_jobs_and_isolates_failures() -> None:
    clock = _Clock()
    ran: list[str] = []

    async def failing() -> None:
        ran.append("failing")
        raise RuntimeError("settle backend down")

    async def ok() -> None:
        ran.append("ok")

    jobs = [
        ("failing", Cadence(5.0, clock=clock), failing),
        ("ok", Cadence(5.0, clock=clock), ok),
        ("slow", Cadence(60.0, clock=clock), ok),
    ]

    await run_due(jobs)
    assert ran == ["failing", "ok", "ok"]

    clock.now = 5.0
    ran.clear()
    await run_due(jobs)
    assert ran == ["failing", "ok"]  # the 60 s job is not due yet; the failure did not stop "ok"
