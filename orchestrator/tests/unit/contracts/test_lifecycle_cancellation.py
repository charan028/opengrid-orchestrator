"""R3.4.1 follow-up (DISPATCH review): `engine.lifecycle` now runs `advance_obligations` as a
backgrounded task under `asyncio.wait_for(30s)`. `transition_obligation` writes the obligation's
state and its trace row as two separate commits (`repo.update_obligation_state`, then
`trace.append`) -- a cancellation landing between them could leave a state change with no trace
(K10). `transition_obligation` shields that write pair (`asyncio.shield`) so a caller's timeout can
only stop US from waiting on it; the write itself always finishes both halves or neither."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.contracts.errors import ConcurrentUpdateError
from opengrid.contracts.lifecycle import transition_obligation
from opengrid.core.models.engine import Obligation


def _obligation(**overrides: object) -> Obligation:
    now = datetime.now(UTC)
    base: dict[str, object] = dict(
        obligation_id=uuid4(),
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        service_type="ERCOT_ENERGY",
        tier="T2",
        window_start=now,
        window_end=now + timedelta(minutes=15),
        committed_qty_kw=Decimal("10"),
        state="COMMITTED",
        version=1,
    )
    base.update(overrides)
    return Obligation(**base)  # type: ignore[arg-type]


class _SlowRepo:
    """A repo whose `update_obligation_state` takes `delay_s` -- long enough for an outer
    `asyncio.wait_for` to time out and try to cancel `transition_obligation` while the write is
    in flight, before the trace has been appended."""

    def __init__(self, obligation: Obligation, delay_s: float) -> None:
        self._obligation = obligation
        self.delay_s = delay_s
        self.updates: list[str] = []

    async def get_obligation(self, obligation_id):
        return self._obligation

    async def update_obligation_state(
        self, obligation_id, *, to_state, reason_code, expected_version, at_risk=None
    ):
        await asyncio.sleep(self.delay_s)
        self._obligation = self._obligation.model_copy(
            update={"state": to_state, "version": expected_version + 1}
        )
        self.updates.append(to_state)
        return self._obligation


class _RecordingTrace:
    def __init__(self) -> None:
        self.appended: list[dict[str, object]] = []

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes):
        self.appended.append(payload)


async def test_a_timeout_racing_the_write_never_leaves_a_state_change_without_its_trace() -> None:
    obligation = _obligation()
    repo = _SlowRepo(obligation, delay_s=0.05)
    trace = _RecordingTrace()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            transition_obligation(repo, trace, obligation.obligation_id, "DELIVERING", reason_code=None),
            timeout=0.01,
        )

    # The timeout fired well before the write finished -- prove the shielded write is still running
    # (not silently dropped) and let it land.
    assert repo.updates == []
    await asyncio.sleep(0.1)

    # The core invariant: a state write is never left without its trace row, regardless of whether the
    # caller that requested it is still around to see the result.
    assert len(repo.updates) == len(trace.appended) == 1
    assert trace.appended[0]["to_state"] == "DELIVERING" == repo.updates[0]


async def test_a_timeout_before_any_write_starts_leaves_nothing_written() -> None:
    """Cancelling during the initial `get_obligation` read (before the write pair is even entered) must
    still leave the obligation and trace untouched -- the shield only protects the write, not the read."""
    obligation = _obligation()

    class _SlowRead(_SlowRepo):
        async def get_obligation(self, obligation_id):
            await asyncio.sleep(self.delay_s)
            return await super().get_obligation(obligation_id)

    repo = _SlowRead(obligation, delay_s=0.05)
    trace = _RecordingTrace()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            transition_obligation(repo, trace, obligation.obligation_id, "DELIVERING", reason_code=None),
            timeout=0.01,
        )

    await asyncio.sleep(0.1)
    assert repo.updates == []
    assert trace.appended == []


async def test_no_timeout_still_writes_state_and_trace_together() -> None:
    """Sanity check: shielding must not change the normal (uncancelled) outcome."""
    obligation = _obligation()
    repo = _SlowRepo(obligation, delay_s=0.0)
    trace = _RecordingTrace()

    updated = await transition_obligation(
        repo, trace, obligation.obligation_id, "DELIVERING", reason_code=None
    )

    assert updated.state == "DELIVERING"
    assert repo.updates == ["DELIVERING"]
    assert trace.appended[0]["to_state"] == "DELIVERING"


# --- DISPATCH follow-up: an orphaned shielded write (nobody left awaiting it) must still be logged ---


class _FailingRepo(_SlowRepo):
    """A repo whose `update_obligation_state` raises `to_raise` instead of succeeding, after `delay_s`
    -- stands in for a write that is still in flight (shielded, orphaned) when its caller's timeout
    fires, and then fails on its own once it finally runs."""

    def __init__(self, obligation: Obligation, delay_s: float, to_raise: Exception) -> None:
        super().__init__(obligation, delay_s)
        self._to_raise = to_raise

    async def update_obligation_state(
        self, obligation_id, *, to_state, reason_code, expected_version, at_risk=None
    ):
        await asyncio.sleep(self.delay_s)
        raise self._to_raise


async def test_an_orphaned_write_that_fails_is_logged_not_silently_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Previously nothing awaited the shielded task once its caller (the lifecycle timeout) moved on, so
    a failure there would only surface as asyncio's generic 'Task exception was never retrieved'
    warning. The done-callback must log it, attributed to the obligation, at error level."""
    obligation = _obligation()
    repo = _FailingRepo(obligation, delay_s=0.02, to_raise=RuntimeError("db connection reset"))
    trace = _RecordingTrace()

    with caplog.at_level(logging.INFO, logger="opengrid.contracts.lifecycle"):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                transition_obligation(repo, trace, obligation.obligation_id, "DELIVERING", reason_code=None),
                timeout=0.005,
            )
        await asyncio.sleep(0.05)  # let the orphaned write run to its failure

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    assert "shielded from cancellation" in error_records[0].message
    assert error_records[0].obligation_id == str(obligation.obligation_id)


async def test_an_orphaned_write_that_loses_the_cas_race_is_logged_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A losing CAS race is the ordinary outcome of two lifecycle passes overlapping (DISPATCH's note
    1) -- even when nobody is left awaiting the write, it must log at INFO, never ERROR."""
    obligation = _obligation()
    repo = _FailingRepo(
        obligation, delay_s=0.02, to_raise=ConcurrentUpdateError(obligation.obligation_id, obligation.version)
    )
    trace = _RecordingTrace()

    with caplog.at_level(logging.INFO, logger="opengrid.contracts.lifecycle"):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                transition_obligation(repo, trace, obligation.obligation_id, "DELIVERING", reason_code=None),
                timeout=0.005,
            )
        await asyncio.sleep(0.05)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("lost the optimistic-lock race" in r.message for r in caplog.records)
