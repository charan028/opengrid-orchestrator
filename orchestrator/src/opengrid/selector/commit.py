"""Commit step of a selector gate (02a S2.1, S3.8): moves each selected candidate's obligation through
`OFFERED -> SELECTED -> COMMITTED` (or `SELECTED -> REJECTED`) around `ledger.reserve()`.

`reserve()` checks K2 against the bank capability read at commit time, which can be lower than the
snapshot the plan was solved on (hubs drop offline, SoC moves). Before rejecting, the obligation's
per-interval total is re-placed once on banks with live free headroom -- the "no substitute" condition
of the `SELECTED -> REJECTED` edge. One obligation's failure never stops the others (K7).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from opengrid import contracts, ledger
from opengrid.selector.types import CandidateOpportunity, ExtractedPlan

logger = logging.getLogger(__name__)

R_GATE_SELECT = "R-GATE-SELECT"
R_COMMIT_LOCK_ENTER = "R-COMMIT-LOCK-ENTER"
R_COMMIT_LOCK_INFEASIBLE = "R-COMMIT-LOCK-INFEASIBLE"


def selected_kw_by_interval_key(
    candidate: CandidateOpportunity, result: ExtractedPlan, horizon_start: datetime, interval_minutes: float
) -> dict[str, Decimal]:
    """The plan's positive per-(bank, interval) allocation for `candidate`, keyed for `ledger.reserve()`
    (`encode_interval_key`), in kW."""
    step = timedelta(minutes=interval_minutes)
    allocation = result.bank_interval_allocation
    selected: dict[str, Decimal] = {}
    for t in candidate.window_intervals:
        for bank_id in candidate.eligible_bank_ids:
            kw = allocation.get((candidate.opportunity_id, bank_id, t), 0.0)
            if kw > 0:
                start = horizon_start + step * t
                selected[ledger.encode_interval_key(bank_id, start, start + step)] = Decimal(str(kw))
    return selected


async def replace_on_live_headroom(
    candidate: CandidateOpportunity, selected_kw: dict[str, Decimal]
) -> dict[str, Decimal] | None:
    """Re-place each interval's total kW first-fit on the candidate's eligible banks by live
    `ledger.free_headroom`. Returns the new keyed allocation, or `None` if some interval cannot be
    covered in full (the obligation's quantity is never reduced here -- that would be a partial
    commitment the plan did not select)."""
    totals: dict[tuple[datetime, datetime], Decimal] = {}
    for key, kw in selected_kw.items():
        _bank_id, start, end = ledger.decode_interval_key(key)
        totals[start, end] = totals.get((start, end), Decimal(0)) + kw

    replaced: dict[str, Decimal] = {}
    for (start, end), need in sorted(totals.items()):
        for bank_id in candidate.eligible_bank_ids:
            if need <= 0:
                break
            take = min(max(await ledger.free_headroom(bank_id, start), Decimal(0)), need)
            if take > 0:
                replaced[ledger.encode_interval_key(bank_id, start, end)] = take
                need -= take
        if need > 0:
            return None
    return replaced


async def commit_candidate(
    candidate: CandidateOpportunity, selected_kw: dict[str, Decimal], plan_id: UUID
) -> bool:
    """Commit one selected candidate (02a S2.1). Returns True if it reached `COMMITTED`."""
    obligation_id = UUID(candidate.obligation_id)
    trace_payload: dict[str, object] = {"plan_id": str(plan_id), "opportunity_id": candidate.opportunity_id}
    try:
        await contracts.transition_obligation(
            obligation_id, "SELECTED", reason_code=R_GATE_SELECT, payload=trace_payload
        )
    except (contracts.IllegalTransitionError, contracts.ConcurrentUpdateError, LookupError):
        logger.warning("obligation no longer selectable", extra={"obligation_id": str(obligation_id)})
        return False

    if not await _reserve_with_substitute(candidate, obligation_id, selected_kw, plan_id):
        await contracts.transition_obligation(
            obligation_id, "REJECTED", reason_code=R_COMMIT_LOCK_INFEASIBLE, payload=trace_payload
        )
        logger.warning(
            "commitment infeasible at commit time; obligation rejected",
            extra={"obligation_id": str(obligation_id), "reason_code": R_COMMIT_LOCK_INFEASIBLE},
        )
        return False

    await contracts.transition_obligation(
        obligation_id, "COMMITTED", reason_code=R_COMMIT_LOCK_ENTER, payload=trace_payload
    )
    return True


async def _reserve_with_substitute(
    candidate: CandidateOpportunity, obligation_id: UUID, selected_kw: dict[str, Decimal], plan_id: UUID
) -> bool:
    try:
        await ledger.reserve(obligation_id, selected_kw, plan_id, variable_kind=candidate.variable_kind)
        return True
    except ledger.ReservationError as exc:
        _log_refusal(obligation_id, "planned", exc)
        if exc.reason_code != R_COMMIT_LOCK_INFEASIBLE:
            return False
    substitute = await replace_on_live_headroom(candidate, selected_kw)
    if substitute is None:
        logger.info("no live-headroom substitute", extra={"obligation_id": str(obligation_id)})
        return False
    try:
        await ledger.reserve(obligation_id, substitute, plan_id, variable_kind=candidate.variable_kind)
    except ledger.ReservationError as exc:
        _log_refusal(obligation_id, "substitute", exc)
        return False
    return True


def _log_refusal(obligation_id: UUID, attempt: str, exc: ledger.ReservationError) -> None:
    logger.info(
        "reservation refused",
        extra={
            "obligation_id": str(obligation_id),
            "attempt": attempt,
            "reason_code": exc.reason_code,
            **exc.detail,
        },
    )
