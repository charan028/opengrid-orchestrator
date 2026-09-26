"""Re-nomination point handling (02a S1.7, S2.1; ES04-S04). A multi-day/tolling contract's lock
runs until its next declared re-nomination point -- an extra gate for that contract's obligation(s)
only. Between points the lock is absolute: this module is the only way `DELIVERING` obligations
re-enter selection, and only through a due, unexercised point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from opengrid.contracts.errors import IllegalTransitionError, RenominationError
from opengrid.contracts.lifecycle import transition_obligation
from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import RenominationPoint
from opengrid.trace import TraceStore

_VALID_OUTCOMES = frozenset({"RESELECTED", "CONFIRMED", "SKIPPED"})


async def due_renomination_points(
    repo: ContractsRepo, *, as_of: datetime | None = None
) -> list[RenominationPoint]:
    """Points with `scheduled_at <= as_of` and `exercised_at IS NULL` -- what `og-engine`'s gate
    scheduler polls to decide when to run a `RENOMINATION`-scoped selector gate (02a S3.1)."""
    return await repo.due_renomination_points(as_of or datetime.now(UTC))


async def exercise_renomination_point(
    repo: ContractsRepo,
    trace: TraceStore,
    renomination_point_id: UUID,
    outcome: str,
    *,
    plan_id: UUID | None = None,
) -> RenominationPoint:
    """Mark a re-nomination point exercised, and if `outcome == 'RESELECTED'`, drive the
    obligation's `DELIVERING -> DELIVERING` self-loop (`R-RENOM-GATE`, 02a S2.1) so the selector may
    re-decide that one obligation. `CONFIRMED`/`SKIPPED` only record that the gate was reached with
    no change -- K13 still holds for every other obligation, and for this one outside this call.
    """
    if outcome not in _VALID_OUTCOMES:
        raise RenominationError("R-RENOM-INVALID-OUTCOME")

    point = await repo.get_renomination_point(renomination_point_id)
    if point is None:
        raise RenominationError("R-RENOM-UNKNOWN-POINT")
    if point.exercised_at is not None:
        raise RenominationError("R-RENOM-ALREADY-EXERCISED")

    now = datetime.now(UTC)
    if outcome == "RESELECTED":
        if point.obligation_id is None:
            raise RenominationError("R-RENOM-NO-OBLIGATION")
        # The self-loop is DELIVERING -> DELIVERING only. Without this check a COMMITTED obligation took
        # the COMMITTED -> DELIVERING edge (no reason required) and started "delivering" hours before its
        # window (live 2026-09-26).
        obligation = await repo.get_obligation(point.obligation_id)
        if obligation is None or obligation.state != "DELIVERING":
            raise IllegalTransitionError(
                from_state=obligation.state if obligation else "UNKNOWN",
                to_state="DELIVERING",
                reason_code="R-RENOM-GATE",
            )
        await transition_obligation(
            repo,
            trace,
            point.obligation_id,
            "DELIVERING",
            reason_code="R-RENOM-GATE",
            payload={"renomination_point_id": str(renomination_point_id)},
        )

    updated = await repo.mark_renomination_exercised(
        renomination_point_id, outcome=outcome, plan_id=plan_id, exercised_at=now
    )
    await trace.append(
        f"contract-{point.contract_id}",
        "RENOMINATION",
        "RENOMINATION",
        {
            "renomination_point_id": str(renomination_point_id),
            "contract_id": str(point.contract_id),
            "obligation_id": str(point.obligation_id) if point.obligation_id else None,
            "outcome": outcome,
        },
        ["R-RENOM-GATE"] if outcome == "RESELECTED" else None,
    )
    return updated
