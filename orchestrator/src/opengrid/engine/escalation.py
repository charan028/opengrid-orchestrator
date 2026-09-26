"""Mid-window shortfall escalation (02a S2.1 `DELIVERING -> SHORTFALL` "or an L0/L1/L2/infeasible
exception reduced delivery during the window"; K13 exception list).

Until now `SHORTFALL` was only reached at window end (`R-SHORTFALL-THRESHOLD`). This module turns a
SUSTAINED exception into the lifecycle edge with the matching K13 reason code:

- the allocator's per-cycle `ShortfallReport`s -- an L2 utility instruction (`R-SHORTFALL-L2-INSTRUCTION`
  -> `R-COMMIT-LOCK-OVERRIDE-L2`) or no substitute / no bank capacity (`R-COMMIT-LOCK-INFEASIBLE`);
- the continuous energy-sufficiency check's AT_RISK with a negative margin after substitution
  (`R-COMMIT-LOCK-INFEASIBLE`).

A signal must persist for `sustain_cycles` consecutive cycles (default 30 = 60 s at 2 s) so a transient
dip never ends a delivery; one clean cycle resets the count. The obligation keeps its commitment row
(K13); settle bills the shortfall. Pure logic here; the engine applies the transition.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from opengrid.core.reasons import (
    R_COMMIT_LOCK_INFEASIBLE,
    R_COMMIT_LOCK_OVERRIDE_L2,
    R_SHORTFALL_BANK_CAPACITY,
    R_SHORTFALL_L2_INSTRUCTION,
    R_SHORTFALL_NO_SUBSTITUTE,
)

DEFAULT_SUSTAIN_CYCLES = 30

#: Allocator shortfall reason -> the K13 reason the `DELIVERING -> SHORTFALL` edge requires.
_LOCK_REASON_BY_SHORTFALL: dict[str, str] = {
    R_SHORTFALL_L2_INSTRUCTION: R_COMMIT_LOCK_OVERRIDE_L2,
    R_SHORTFALL_NO_SUBSTITUTE: R_COMMIT_LOCK_INFEASIBLE,
    R_SHORTFALL_BANK_CAPACITY: R_COMMIT_LOCK_INFEASIBLE,
}

#: When both apply in one cycle, the authority override wins (it is the stronger, externally-ordered one).
_PRECEDENCE = (R_COMMIT_LOCK_OVERRIDE_L2, R_COMMIT_LOCK_INFEASIBLE)


def lock_reason_for_shortfall(shortfall_reason: str) -> str | None:
    """The K13 reason for an allocator shortfall reason, or `None` if it is not an escalation cause."""
    return _LOCK_REASON_BY_SHORTFALL.get(shortfall_reason)


@dataclass
class ShortfallEscalator:
    """Counts consecutive cycles each obligation carries an exception signal."""

    sustain_cycles: int = DEFAULT_SUSTAIN_CYCLES
    _counts: dict[str, int] = field(default_factory=dict)
    _escalated: set[str] = field(default_factory=set)

    def observe(self, signals: dict[str, set[str]]) -> list[tuple[str, str]]:
        """`signals`: obligation_id -> K13 reasons seen this cycle. Returns `(obligation_id, reason)` for
        each obligation whose signal just reached `sustain_cycles` (each escalates once)."""
        for obligation_id in list(self._counts):
            if obligation_id not in signals:
                del self._counts[obligation_id]
        due: list[tuple[str, str]] = []
        for obligation_id, reasons in signals.items():
            if obligation_id in self._escalated or not reasons:
                continue
            self._counts[obligation_id] = self._counts.get(obligation_id, 0) + 1
            if self._counts[obligation_id] >= self.sustain_cycles:
                reason = next(r for r in _PRECEDENCE if r in reasons)
                self._escalated.add(obligation_id)
                due.append((obligation_id, reason))
        return due


def merge_signals(
    allocator_shortfalls: Iterable[tuple[str, str]], energy_infeasible: Iterable[str]
) -> dict[str, set[str]]:
    """One cycle's signals: `(obligation_id, allocator shortfall reason)` pairs plus obligations the
    energy check found infeasible after substitution."""
    signals: dict[str, set[str]] = {}
    for obligation_id, shortfall_reason in allocator_shortfalls:
        lock_reason = lock_reason_for_shortfall(shortfall_reason)
        if lock_reason is not None:
            signals.setdefault(obligation_id, set()).add(lock_reason)
    for obligation_id in energy_infeasible:
        signals.setdefault(obligation_id, set()).add(R_COMMIT_LOCK_INFEASIBLE)
    return signals
