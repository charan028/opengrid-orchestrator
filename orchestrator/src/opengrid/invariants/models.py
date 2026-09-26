"""Pure data shapes for `opengrid.invariants` (00-invariants.md K1/K2/K13, orphan bookkeeping, trace
verification). No I/O -- see `invariants.queries` for that, mirroring `opengrid.health`'s split
(BUILD.md S5a "pure logic separated from I/O")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --- canonical check names (og.invariant_check.check_name / metrics labels) -----------------------
CHECK_K1_RESERVE_BREACH = "K1_RESERVE_BREACH"
CHECK_K2_DOUBLE_SOLD = "K2_DOUBLE_SOLD"
CHECK_K13_LOCK_VIOLATION = "K13_LOCK_VIOLATION"
#: A K13 dip explained by a total grant-activity outage (an engine restart, a genuine SCADA/comms
#: silence) is still flagged -- the chaos harness needs to see it -- but classified separately from a
#: `K13_LOCK_VIOLATION` (an unexplained realized-below-committed dip while grants WERE flowing). Kept
#: distinct per the owner's policy call; the classification itself lives in one place
#: (`checks.classify_dip`).
CHECK_K13_OUTAGE_GAP = "K13_OUTAGE_GAP"
CHECK_ORPHAN_RESERVATION = "ORPHAN_RESERVATION"
CHECK_ORPHAN_COMMITMENT = "ORPHAN_COMMITMENT"
CHECK_TRACE_VERIFY = "TRACE_VERIFY"

ALL_CHECKS: tuple[str, ...] = (
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_K13_OUTAGE_GAP,
    CHECK_ORPHAN_RESERVATION,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_TRACE_VERIFY,
)


@dataclass(frozen=True, slots=True)
class Violation:
    """One instance of a check's condition found true. `scope` identifies what was affected (hub_id,
    bank_id, obligation_id, ...); `dedupe_key` is the check's own STABLE natural key for that same
    condition (e.g. `f"{bank_id}|{interval_start}"`, `f"{hub_id}|{ts}"`) -- `invariants.queries.
    insert_violations` upserts on `(check_name, dedupe_key)` so a condition that is still true on the
    next run is recognized as the SAME violation, not counted and stored again (verified live: K2's old
    unkeyed insert re-counted one over-sale roughly 120x across an hour's worth of 60s re-scans).
    `detail` carries the numbers a human or the audit trail needs (measured value, threshold, reason
    codes seen). `magnitude` is the check's own natural unit for its Prometheus counter (kWh for K2's
    "kWh sold twice", a bare count of 1 for every other check) -- see each `checks.py` function's
    docstring for what it means there.
    """

    scope: dict[str, Any]
    dedupe_key: str
    detail: dict[str, Any] = field(default_factory=dict)
    magnitude: float = 1.0


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One run of one check: what it found, and the watermark to resume from next time (empty dict for
    a check that always re-scans its whole, deliberately small, active-row subset rather than tracking
    a position -- see each check's docstring in `checks.py`)."""

    check_name: str
    violations: tuple[Violation, ...]
    watermark: dict[str, Any]

    @property
    def count(self) -> int:
        return len(self.violations)

    @property
    def total_magnitude(self) -> float:
        return sum(v.magnitude for v in self.violations)


@dataclass(frozen=True, slots=True)
class CheckState:
    """Row shape of `og.invariant_check` (migrations/0014_invariant_checks.sql): durable watermark and
    running totals, read back at start-up so a restarted process resumes an incremental scan and the
    Prometheus counters keep their monotonic history instead of resetting to 0."""

    check_name: str
    last_run_at: datetime | None
    last_run_ms: int | None
    last_violations: int
    #: A running sum of magnitude (K2: kWh; every other check: a plain count, since their violations
    #: default to `magnitude=1.0` -- see `Violation.magnitude`'s docstring).
    total_violations: float
    watermark: dict[str, Any]

    @staticmethod
    def empty(check_name: str) -> CheckState:
        return CheckState(
            check_name=check_name,
            last_run_at=None,
            last_run_ms=None,
            last_violations=0,
            total_violations=0,
            watermark={},
        )


@dataclass(frozen=True, slots=True)
class InvariantsSummary:
    """The measured read model the API surfaces (`GET /og/api/health`, the control-room SSE stream):
    a plain read of `og.invariant_check`, never a constant. `as_of` is the OLDEST `last_run_at` among
    the summarized checks (the most stale one), so the field truthfully reflects "as of when were ALL
    of these numbers actually measured" rather than the freshest one cherry-picked."""

    reserve_breaches: int
    double_sold_kwh: float
    lock_violations: int
    outage_gaps: int
    orphan_reservations: int
    orphan_commitments: int
    as_of: datetime | None

    @staticmethod
    def unavailable() -> InvariantsSummary:
        """The "not yet measured" value API callers fall back to when `og.invariant_check` can't be
        read (no pool wired, e.g. a unit test that never runs the real app lifespan; or a DB hiccup) --
        distinct from a healthy run's genuine all-zero result only in that `as_of` stays `None`."""
        return InvariantsSummary(
            reserve_breaches=0,
            double_sold_kwh=0.0,
            lock_violations=0,
            outage_gaps=0,
            orphan_reservations=0,
            orphan_commitments=0,
            as_of=None,
        )
