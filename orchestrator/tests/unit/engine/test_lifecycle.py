"""Clock-driven obligation lifecycle (02a S2.1): regression for the live 2026-09-26 finding that no
obligation ever left `COMMITTED`, so `og-settle` (which meters `DELIVERING`/`FULFILLED`/`SHORTFALL`)
never produced a single meter/pnl/invoice row."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from opengrid.contracts import IllegalTransitionError
from opengrid.engine.lifecycle import ClosingObligation, advance_obligations

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


class _Backend:
    def __init__(self, due: list[UUID], closing: list[ClosingObligation]) -> None:
        self._due = due
        self._closing = closing

    async def obligations_due_for_delivery(self, now: datetime) -> list[UUID]:
        return self._due

    async def obligations_due_for_close(self, now: datetime) -> list[ClosingObligation]:
        return self._closing


class _Transitions:
    def __init__(self, fail_for: set[UUID] | None = None) -> None:
        self.calls: list[tuple[UUID, str, str | None]] = []
        self._fail_for = fail_for or set()

    async def __call__(self, obligation_id: UUID, to_state: str, *, reason_code: str | None) -> None:
        if obligation_id in self._fail_for:
            raise IllegalTransitionError(from_state="SETTLED", to_state=to_state, reason_code=reason_code)
        self.calls.append((obligation_id, to_state, reason_code))


async def _expire_none(*, now: datetime) -> list[UUID]:
    return []


async def test_ts_04_committed_obligation_starts_delivering_at_window_start() -> None:
    obligation_id = uuid4()
    transitions = _Transitions()

    counts = await advance_obligations(_Backend([obligation_id], []), transitions, _expire_none, NOW)

    assert transitions.calls == [(obligation_id, "DELIVERING", None)]
    assert counts["DELIVERING"] == 1


async def test_ts_04_window_end_closes_fulfilled_or_shortfall_from_performance() -> None:
    ok, short = uuid4(), uuid4()
    transitions = _Transitions()
    closing = [ClosingObligation(ok, any_interval_failed=False), ClosingObligation(short, True)]

    counts = await advance_obligations(_Backend([], closing), transitions, _expire_none, NOW)

    assert transitions.calls == [
        (ok, "FULFILLED", "R-FULFILLED"),
        (short, "SHORTFALL", "R-SHORTFALL-THRESHOLD"),
    ]
    assert counts["FULFILLED"] == 1
    assert counts["SHORTFALL"] == 1


async def test_one_failed_transition_does_not_block_the_others() -> None:
    bad, good = uuid4(), uuid4()
    transitions = _Transitions(fail_for={bad})

    counts = await advance_obligations(_Backend([bad, good], []), transitions, _expire_none, NOW)

    assert transitions.calls == [(good, "DELIVERING", None)]
    assert counts["DELIVERING"] == 1


async def test_unselected_offers_are_expired() -> None:
    expired = [uuid4(), uuid4()]

    async def _expire(*, now: datetime) -> list[UUID]:
        assert now == NOW
        return expired

    counts = await advance_obligations(_Backend([], []), _Transitions(), _expire, NOW)

    assert counts["EXPIRED"] == 2
