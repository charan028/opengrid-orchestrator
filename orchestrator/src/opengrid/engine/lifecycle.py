"""Time-driven obligation lifecycle edges (02a S2.1), run by og-engine each tick.

The selector moves obligations up to `COMMITTED`; nothing else advanced them afterwards, so no
obligation ever reached `DELIVERING` (which `og-settle` meters) or closed at window end. This module
drives the clock-triggered edges only, always through `opengrid.contracts.transition_obligation` (the
single writer of `og.obligation.state`):

- `COMMITTED -> DELIVERING` once `window_start` is reached (K13 lock stays in force);
- `DELIVERING -> FULFILLED` (`R-FULFILLED`) or `-> SHORTFALL` (`R-SHORTFALL-THRESHOLD`) once
  `window_end` is reached, from settle's per-interval `performance.passed_threshold` rows;
- `OFFERED -> EXPIRED` via `contracts.expire_unselected` (window started, never selected).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

R_FULFILLED = "R-FULFILLED"
R_SHORTFALL_THRESHOLD = "R-SHORTFALL-THRESHOLD"


@dataclass(frozen=True, slots=True)
class ClosingObligation:
    """A `DELIVERING` obligation whose window has ended; `any_interval_failed` is True if settle
    recorded at least one interval below the contract threshold."""

    obligation_id: UUID
    any_interval_failed: bool


class LifecycleBackend(Protocol):
    async def obligations_due_for_delivery(self, now: datetime) -> list[UUID]:
        """`COMMITTED` obligations whose `window_start <= now`."""
        ...

    async def obligations_due_for_close(self, now: datetime) -> list[ClosingObligation]:
        """`DELIVERING` obligations whose `window_end <= now`."""
        ...


Transition = Callable[..., Awaitable[object]]


def close_target(closing: ClosingObligation) -> tuple[str, str]:
    """`(to_state, reason_code)` for a window-end close (02a S2.1)."""
    if closing.any_interval_failed:
        return "SHORTFALL", R_SHORTFALL_THRESHOLD
    return "FULFILLED", R_FULFILLED


async def advance_obligations(
    backend: LifecycleBackend,
    transition: Transition,
    expire_unselected: Callable[..., Awaitable[list[UUID]]],
    now: datetime,
) -> dict[str, int]:
    """Apply every clock-triggered edge due at `now`. One obligation's failure (e.g. a lost optimistic
    lock) is logged and skipped; the rest still advance. Returns counts per target state."""
    counts = {"DELIVERING": 0, "FULFILLED": 0, "SHORTFALL": 0, "EXPIRED": 0}
    for obligation_id in await backend.obligations_due_for_delivery(now):
        if await _apply(transition, obligation_id, "DELIVERING", None):
            counts["DELIVERING"] += 1
    for closing in await backend.obligations_due_for_close(now):
        to_state, reason_code = close_target(closing)
        if await _apply(transition, closing.obligation_id, to_state, reason_code):
            counts[to_state] += 1
    counts["EXPIRED"] = len(await expire_unselected(now=now))
    return counts


async def _apply(transition: Transition, obligation_id: UUID, to_state: str, reason_code: str | None) -> bool:
    try:
        await transition(obligation_id, to_state, reason_code=reason_code)
    except Exception:
        logger.exception(
            "obligation lifecycle transition failed",
            extra={"obligation_id": str(obligation_id), "to_state": to_state},
        )
        return False
    return True
