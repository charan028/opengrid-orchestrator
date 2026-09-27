"""R3.4.1 (PROD-IO, follow-up to the pq_ingest event-loop fix): the obligation lifecycle step
(`advance_obligations`) was awaited inline in `_engine_tick`, on the same 2 s budget as dispatch.
Backgrounding it (`start_lifecycle_in_background`) must (1) never block the tick that starts it and
(2) run at most one pass at a time -- a second cycle arriving while a pass is still in flight must
SKIP, not queue or run concurrently with itself."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from opengrid.engine import start_lifecycle_in_background
from opengrid.engine.lifecycle import ClosingObligation

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


class _SlowBackend:
    """A lifecycle backend whose delivery-due read takes `delay_s` -- stands in for the contended-
    Postgres call slow enough to matter (R3.4.1's trigger for moving this step off the loop)."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def obligations_due_for_delivery(self, now: datetime) -> list[UUID]:
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return []

    async def obligations_due_for_close(self, now: datetime) -> list[ClosingObligation]:
        return []


async def _transition(obligation_id, to_state, *, reason_code, payload=None, at_risk=None):
    raise AssertionError("no obligation is due in this fixture")


async def _expire_none(*, now: datetime) -> list[UUID]:
    return []


def _state(backend: object) -> SimpleNamespace:
    return SimpleNamespace(lifecycle_backend=backend, lifecycle_task=None)


async def test_starting_a_slow_lifecycle_pass_does_not_block_the_caller() -> None:
    """Backgrounding's whole point: starting a pass must return immediately even though the pass
    itself takes seconds -- the tick that calls this must never await it."""
    backend = _SlowBackend(delay_s=0.2)
    state = _state(backend)

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW) is True
    elapsed = loop.time() - started

    assert elapsed < 0.05, "starting the pass must not wait on it"
    assert state.lifecycle_task is not None
    await state.lifecycle_task  # let it finish so the test doesn't leak a background task


async def test_a_second_cycle_skips_while_the_first_pass_is_still_running() -> None:
    """Single-flight: a lifecycle pass slow enough to still be running on the NEXT tick (a slow fake
    lifecycle here stands in for the 5 s case DISPATCH flagged) must be skipped that cycle, never
    queued and never run concurrently with itself."""
    backend = _SlowBackend(delay_s=0.2)
    state = _state(backend)

    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW) is True
    first_task = state.lifecycle_task
    await asyncio.sleep(0)  # let the task start running (reach its slow DB read) before checking it

    # A second tick arrives before the first pass's (slow) DB read has returned.
    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW) is False
    assert state.lifecycle_task is first_task, "no second task should have been started"
    assert backend.calls == 1

    await first_task

    # Once the first pass has finished, the next cycle is free to start a new one.
    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW) is True
    assert state.lifecycle_task is not first_task
    await state.lifecycle_task
    assert backend.calls == 2


async def test_a_pass_that_times_out_is_abandoned_not_raised() -> None:
    """A pass stuck long enough to hit the timeout is logged and dropped -- it must never raise into
    the tick, and must leave `lifecycle_task` in a normal `done()` state so the NEXT cycle can start a
    fresh pass rather than being permanently skipped."""
    backend = _SlowBackend(delay_s=1.0)
    state = _state(backend)

    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW, timeout_s=0.05) is True
    await state.lifecycle_task  # must not raise
    assert state.lifecycle_task.done()


async def test_no_lifecycle_backend_is_a_no_op() -> None:
    """`state.lifecycle_backend is None` (e.g. a process wired without lifecycle duties) must not start
    anything, matching the previous inline `if state.lifecycle_backend is not None:` guard."""
    state = _state(None)
    assert start_lifecycle_in_background(state, _transition, _expire_none, NOW) is False
    assert state.lifecycle_task is None
