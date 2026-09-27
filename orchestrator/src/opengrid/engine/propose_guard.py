"""Bounded hand-off to og-guardian: a propose phase that can never stall the dispatch cycle.

Prod 2026-09-27 00:17:55 CT (r3.4.1): one cycle's propose phase blocked for 25.6 s (a database stall that
also stopped the guardian's heartbeat), leaving a 31 s silence in `og.grant` that K13 flagged as an outage
gap. Two rules, both owned here:

1. **Bounded propose.** `propose_banks` gives the whole phase `[allocator].propose_timeout_s` (default
   2 s). Banks not proposed by then are cancelled and reported as timed out; the cycle moves on. A bank
   cancelled mid-way leaves at most a committed `RT_ALLOCATION` pre-image, or a batch row the guardian was
   not notified of -- both are safe under K10 (trace before act) and the batch's own lease (`expires_at`).
   Hubs keep their last signed command until its lease ends (hold, K7).
2. **Skip while the guardian is known unavailable.** `GuardianGate.available` reads the guardian heartbeat
   age with its own bound (`[allocator].guardian_check_timeout_s`, default 0.5 s; a timeout is
   "unavailable"). After a propose timeout the guardian counts as unavailable until it writes a heartbeat
   newer than that timeout -- the stall is known, so the next cycles hold instead of stalling again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("opengrid.engine")

DEFAULT_PROPOSE_TIMEOUT_S = 2.0
DEFAULT_GUARDIAN_CHECK_TIMEOUT_S = 0.5


@dataclass(frozen=True, slots=True)
class ProposeResult:
    """`failed`: every bank whose batch did not go out this cycle (an error or the timeout), in the
    caller's bank order. `timed_out`: the subset cut off by `timeout_s`."""

    failed: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()


def _drain(task: asyncio.Task[Any]) -> None:
    """Done-callback for a bank task left behind after the timeout: retrieve its outcome so a late error
    is logged, never "exception was never retrieved"."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("command batch proposal failed after the propose timeout", exc_info=exc)


async def propose_banks[G](
    grants_by_bank: dict[str, list[G]],
    propose: Callable[[str, list[G]], Coroutine[Any, Any, None]],
    *,
    concurrency: int,
    timeout_s: float | None = None,
) -> ProposeResult:
    """Propose every bank's batch, up to `concurrency` banks at once, the whole phase bounded by
    `timeout_s` (`None` = unbounded). A bank's failure is logged and isolated (K7); banks still pending at
    the timeout are cancelled without waiting for them to unwind, so the caller returns on time."""
    gate = asyncio.Semaphore(concurrency)

    async def _one(bank_id: str, bank_grants: list[G]) -> None:
        async with gate:
            await propose(bank_id, bank_grants)

    bank_ids = list(grants_by_bank)
    if not bank_ids:
        return ProposeResult()
    tasks = {bank_id: asyncio.create_task(_one(bank_id, grants_by_bank[bank_id])) for bank_id in bank_ids}
    try:
        _done, pending = await asyncio.wait(tasks.values(), timeout=timeout_s)
    except asyncio.CancelledError:
        for task in tasks.values():
            task.cancel()
        raise
    failed: list[str] = []
    timed_out: list[str] = []
    for bank_id, task in tasks.items():
        if task in pending:
            task.cancel()
            task.add_done_callback(_drain)
            timed_out.append(bank_id)
            failed.append(bank_id)
            continue
        if task.cancelled():
            raise asyncio.CancelledError
        exc = task.exception()
        if exc is not None:
            logger.error("command batch proposal failed", exc_info=exc, extra={"bank_id": bank_id})
            failed.append(bank_id)
    if timed_out:
        logger.warning(
            "propose timed out -- %d bank batch(es) not proposed this cycle, holding",
            len(timed_out),
            extra={"timeout_s": timeout_s, "bank_ids": timed_out},
        )
    return ProposeResult(failed=tuple(failed), timed_out=tuple(timed_out))


@dataclass(slots=True)
class GuardianGate:
    """Whether to hand batches to the guardian this cycle (see the module docstring)."""

    propose_timeout_s: float = DEFAULT_PROPOSE_TIMEOUT_S
    check_timeout_s: float = DEFAULT_GUARDIAN_CHECK_TIMEOUT_S
    #: Wall-clock time of the last propose timeout, until a newer guardian heartbeat clears it.
    stalled_at: datetime | None = None

    def note_stall(self, at: datetime | None = None) -> None:
        """Record a propose timeout (default: now), so only a heartbeat written after it clears it."""
        self.stalled_at = at if at is not None else datetime.now(UTC)

    async def available(
        self,
        heartbeat_age_s: Callable[[], Awaitable[float | None]],
        *,
        now: datetime,
        miss_threshold_s: float,
    ) -> bool:
        """`heartbeat_age_s`: the guardian heartbeat's age at `now` (None = never beat)."""
        try:
            age_s = await asyncio.wait_for(heartbeat_age_s(), timeout=self.check_timeout_s)
        except TimeoutError:
            logger.warning("guardian heartbeat read timed out -- treating the guardian as unavailable")
            return False
        if age_s is None or age_s > miss_threshold_s:
            return False
        if self.stalled_at is not None:
            if now - timedelta(seconds=age_s) <= self.stalled_at:
                return False  # no heartbeat since the stall: still known unavailable
            self.stalled_at = None
        return True
