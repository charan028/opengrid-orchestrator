"""K4 fail-safe: "VETO; re-solve without the vetoed items" (00-invariants.md K4, review R3).

The guardian signs only PASS: one vetoed item leaves the whole bank batch unsigned, and re-proposing the
same batch next cycle is vetoed again while the hubs' leases lapse. After proposing, the engine waits
briefly (`[allocator.veto_retry].wait_s`) for the verdicts. For each PARTLY_VETOED or VETOED bank whose
verdict names vetoed hubs, those hubs are excluded for `exclude_cycles` cycles (reason
R-HUB-VETO-EXCLUDED, traced) and the bank is re-proposed ONCE in the same cycle without them: the allocator
sees them as unavailable, so their kW moves to other hubs of the same obligation (substitution, K13), and
the command build gives them no item. A verdict that arrives after the wait is handled at the start of
the next cycle, the same way (without the same-cycle retry).

Which hubs a verdict vetoed is read from the guardian's GUARDIAN_VERDICT trace payload: its full
`vetoed_hub_ids` (each violation's `hub_id` is a secondary fallback for older verdicts). A veto with no hub (a batch-level rule such as G-03 or
G-13) cannot be fixed by excluding hubs: nothing is excluded or retried.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDE_CYCLES = 3
DEFAULT_VERDICT_WAIT_S = 0.4
_POLL_S = 0.05
VETO_OUTCOMES = frozenset({"PARTLY_VETOED", "VETOED"})

_VERDICTS_SQL = """
SELECT command_batch_id, outcome FROM og.verdict WHERE command_batch_id = ANY(%(ids)s::uuid[])
"""
_VERDICT_TRACE_SQL = """
SELECT payload FROM og.trace
WHERE stream_id = 'guardian' AND decision_type = 'GUARDIAN_VERDICT'
  AND payload->>'command_batch_id' = ANY(%(ids)s)
"""


class VerdictReader(Protocol):
    async def outcomes(self, batch_ids: list[UUID]) -> dict[UUID, str]: ...

    async def vetoed_hubs(self, batch_ids: list[UUID]) -> dict[UUID, set[str]]: ...


class PgVerdictReader:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def outcomes(self, batch_ids: list[UUID]) -> dict[UUID, str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_VERDICTS_SQL, {"ids": [str(b) for b in batch_ids]})
            rows = await cur.fetchall()
        return {UUID(str(b)): str(o) for b, o in rows}

    async def vetoed_hubs(self, batch_ids: list[UUID]) -> dict[UUID, set[str]]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_VERDICT_TRACE_SQL, {"ids": [str(b) for b in batch_ids]})
            rows = await cur.fetchall()
        out: dict[UUID, set[str]] = {}
        for (payload,) in rows:
            batch_id = UUID(str(payload["command_batch_id"]))
            out.setdefault(batch_id, set()).update(hubs_in_verdict(payload))
        return out


def hubs_in_verdict(payload: Mapping[str, Any]) -> set[str]:
    """The hubs a verdict's trace payload names as vetoed."""
    hubs = {str(h) for h in payload.get("vetoed_hub_ids") or [] if h}
    for violation in payload.get("violations") or []:
        hub_id = violation.get("hub_id") if isinstance(violation, Mapping) else None
        if hub_id:
            hubs.add(str(hub_id))
    return hubs


@dataclass
class HubVetoExclusions:
    """Hubs kept out of dispatch after a guardian veto, each for a number of cycles."""

    exclude_cycles: int = DEFAULT_EXCLUDE_CYCLES
    _remaining: dict[str, int] = field(default_factory=dict)

    def exclude(self, hub_ids: Iterable[str]) -> set[str]:
        """Exclude `hub_ids` (restarting their count); returns the hubs newly excluded."""
        new = {h for h in hub_ids if h not in self._remaining}
        for hub_id in hub_ids:
            self._remaining[hub_id] = self.exclude_cycles
        return new

    def next_cycle(self) -> None:
        for hub_id in list(self._remaining):
            self._remaining[hub_id] -= 1
            if self._remaining[hub_id] <= 0:
                del self._remaining[hub_id]

    def active(self) -> frozenset[str]:
        return frozenset(self._remaining)


async def wait_for_verdicts(
    reader: VerdictReader,
    batch_ids: list[UUID],
    *,
    wait_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[UUID, str]:
    """Poll for the verdicts of `batch_ids` for at most `wait_s`; returns those that arrived."""
    if not batch_ids:
        return {}
    deadline = clock() + wait_s
    found: dict[UUID, str] = {}
    while True:
        try:
            found.update(await reader.outcomes([b for b in batch_ids if b not in found]))
        except Exception:
            logger.warning("verdicts unreadable", exc_info=True)
            return found
        if len(found) == len(batch_ids) or clock() >= deadline:
            return found
        await sleep(_POLL_S)


async def vetoed_banks(
    reader: VerdictReader, outcomes: Mapping[UUID, str], bank_of: Mapping[UUID, str]
) -> dict[str, set[str]]:
    """`bank_id -> vetoed hubs` for every PARTLY_VETOED/VETOED batch that names hubs."""
    vetoed = [b for b, o in outcomes.items() if o in VETO_OUTCOMES]
    if not vetoed:
        return {}
    try:
        hubs = await reader.vetoed_hubs(vetoed)
    except Exception:
        logger.warning("verdict details unreadable; no hub excluded", exc_info=True)
        return {}
    out: dict[str, set[str]] = {}
    for batch_id in vetoed:
        named = hubs.get(batch_id, set())
        if named and batch_id in bank_of:
            out.setdefault(bank_of[batch_id], set()).update(named)
    return out
