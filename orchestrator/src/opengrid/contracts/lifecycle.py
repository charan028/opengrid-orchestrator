"""Obligation state-machine transitions, traced (02a S2.1 table). This is the only place that
writes `og.obligation.state` -- `opengrid.selector`, `opengrid.ledger` and `opengrid.settle` call
`transition_obligation` instead of updating the row themselves, so the legality check
(`state_machine.validate_transition`) and the trace write can never be skipped (K10: no state
change without a durable trace pre-image).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from opengrid.contracts.errors import ConcurrentUpdateError
from opengrid.contracts.repository import ContractsRepo
from opengrid.contracts.state_machine import validate_transition
from opengrid.core.models.engine import DecisionType, Obligation, ObligationState
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

#: decision_type/event_class to trace each target state under (02a S2.1's "Trace event" column).
_TRACE_FOR_TARGET: dict[ObligationState, tuple[DecisionType, str]] = {
    "SELECTED": ("COMMITMENT", "SELECTION"),
    "COMMITTED": ("COMMITMENT", "COMMITMENT"),
    "REJECTED": ("ADMISSION", "ADMISSION"),
    "EXPIRED": ("ADMISSION", "ADMISSION"),
    "DELIVERING": ("RT_ALLOCATION", "RT_ALLOCATION"),
    "FULFILLED": ("SHORTFALL", "SHORTFALL"),
    "SHORTFALL": ("SHORTFALL", "SHORTFALL"),
    "SETTLED": ("SETTLEMENT", "SETTLEMENT"),
}


def _stream_id(obligation_id: UUID) -> str:
    return f"obligation-{obligation_id}"


async def transition_obligation(
    repo: ContractsRepo,
    trace: TraceStore,
    obligation_id: UUID,
    to_state: ObligationState,
    *,
    reason_code: str | None,
    payload: dict[str, object] | None = None,
    at_risk: bool | None = None,
) -> Obligation:
    """Validate `current.state -> to_state` (02a S2.1), persist it with an optimistic-lock CAS on
    `version`, and append the transition to the trace before returning. Raises
    `IllegalTransitionError` (state_machine) or `LookupError` (unknown obligation) or
    `ConcurrentUpdateError` (lost the CAS race -- caller should re-read and retry) without writing
    anything on failure.

    R3.4.1 (engine's lifecycle step now runs as a backgrounded, timed-out task -- `engine.lifecycle`):
    the state UPDATE (`repo.update_obligation_state`) and the trace append are two separate commits,
    not one transaction, so a plain `asyncio.wait_for` timeout landing between them would cancel this
    coroutine mid-way and could leave a state change with no trace row (K10 violation). The write pair
    below is wrapped in `asyncio.shield` so a caller's cancellation (timeout or otherwise) only stops
    US from waiting on it -- the shielded task keeps running to completion, so `update_obligation_state`
    and `trace.append` always land together or (if cancelled before either starts) not at all.
    """
    current = await repo.get_obligation(obligation_id)
    if current is None:
        raise LookupError(f"no such obligation: {obligation_id}")

    validate_transition(current.state, to_state, reason_code)

    async def _write() -> Obligation:
        updated = await repo.update_obligation_state(
            obligation_id,
            to_state=to_state,
            reason_code=reason_code,
            expected_version=current.version,
            at_risk=at_risk,
        )

        decision_type, event_class = _TRACE_FOR_TARGET.get(to_state, ("ADMISSION", "ADMISSION"))
        trace_payload: dict[str, object] = {
            "obligation_id": str(obligation_id),
            "from_state": current.state,
            "to_state": to_state,
        }
        if payload:
            trace_payload.update(payload)
        reason_codes = [reason_code] if reason_code else None
        await trace.append(_stream_id(obligation_id), decision_type, event_class, trace_payload, reason_codes)
        return updated

    # Built as an explicit Task (not a bare coroutine passed to `shield`) so a done-callback can be
    # attached: if OUR caller is the one cancelled (the lifecycle timeout), nobody ever awaits this task
    # again, and an exception it raises would otherwise vanish into asyncio's "Task exception was never
    # retrieved" warning instead of a normal, attributed log line.
    task: asyncio.Task[Obligation] = asyncio.ensure_future(_write())
    task.add_done_callback(lambda t: _log_orphaned_write_failure(t, obligation_id, to_state))
    return await asyncio.shield(task)


def _log_orphaned_write_failure(
    task: asyncio.Task[Obligation], obligation_id: UUID, to_state: ObligationState
) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    if isinstance(exc, ConcurrentUpdateError):
        # Expected, not exceptional (see `engine.lifecycle._apply`'s matching info-level log): the
        # lifecycle pass that abandoned this write on timeout can race a fresh pass's retry of the same
        # obligation. The CAS makes one of them win; this is the loser, logged quietly.
        logger.info(
            "obligation write lost the optimistic-lock race after its caller stopped waiting on it",
            extra={"obligation_id": str(obligation_id), "to_state": to_state},
        )
        return
    logger.error(
        "obligation write failed after its caller stopped waiting on it (shielded from cancellation)",
        exc_info=exc,
        extra={"obligation_id": str(obligation_id), "to_state": to_state},
    )


async def expire_unselected(
    repo: ContractsRepo, trace: TraceStore, *, now: datetime | None = None
) -> list[UUID]:
    """Sweep `OFFERED` opportunities/obligations whose window has started with no selection
    (02a S2.1's `OFFERED -> EXPIRED`). Returns the obligation ids expired. Idempotent: an opportunity
    already moved out of `OFFERED` by a concurrent selector gate is silently skipped."""
    now = now or datetime.now(UTC)
    expired: list[UUID] = []
    for opportunity in await repo.unselected_offered_before(now):
        obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
        if obligation is None or obligation.state != "OFFERED":
            continue
        try:
            await transition_obligation(
                repo, trace, obligation.obligation_id, "EXPIRED", reason_code="R-EXPIRED-UNSELECTED"
            )
        except ConcurrentUpdateError:
            continue
        await repo.update_opportunity_state(
            opportunity.opportunity_id,
            state="EXPIRED",
            reason_code="R-EXPIRED-UNSELECTED",
            decided_at=now,
        )
        expired.append(obligation.obligation_id)
    return expired
