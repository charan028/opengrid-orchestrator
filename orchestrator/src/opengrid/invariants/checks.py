"""Pure violation-detection logic for `opengrid.invariants` (00-invariants.md K1, K2, K13, plus orphan
reservation/commitment bookkeeping). No I/O -- every function here takes already-fetched rows (plain
tuples, matching `invariants.queries`' cursor output) and returns `Violation`s; this is what makes each
check unit-testable against a seeded-violation fixture without a database (BUILD.md S5a).

These are INDEPENDENT of the write paths that already enforce K1/K2/K13 at decision time
(`opengrid.core.limits`, `opengrid.ledger`): they read only what actually landed in the database
afterward, so a bug in the enforcing code path (or a write that bypassed it entirely) is still caught.
Per 00-invariants.md's "Proven by" column, this is the "live counter" half of each invariant's proof;
the property tests are the other half.
"""

from __future__ import annotations

import itertools
from datetime import datetime

from opengrid.invariants.models import Violation

# Float-compare tolerances, matching `opengrid.core.limits`' own 1e-9-scale epsilons (no separate,
# undocumented slop introduced here).
_SOC_TOLERANCE_KWH = 1e-6
_KW_TOLERANCE = 1e-6

#: Terminal obligation states a live commitment lock must never survive on (02a S1.5 state machine).
ORPHAN_COMMITMENT_OBLIGATION_STATES: frozenset[str] = frozenset({"REJECTED", "EXPIRED"})


def find_reserve_breaches(
    rows: list[tuple[str, datetime, float, float, float]],
) -> list[Violation]:
    """K1: a hub whose reported SoC is below its reserve floor while it is discharging.

    `rows`: `(hub_id, ts, soc_kwh, p_kw, r_kwh)` telemetry/hub_state samples (sign convention per
    `opengrid.core.physics`: `p_kw` negative is discharge). A hub sitting below reserve while idle or
    charging is not itself a K1 breach -- it is on its way back up, or was already below reserve before
    being commanded (a pre-existing physical condition, not a command that drove it there); only an
    active discharge below reserve is the invariant this function proves.
    """
    violations = []
    for hub_id, ts, soc_kwh, p_kw, r_kwh in rows:
        if p_kw < -_KW_TOLERANCE and soc_kwh < r_kwh - _SOC_TOLERANCE_KWH:
            violations.append(
                Violation(
                    scope={"hub_id": hub_id, "ts": ts.isoformat()},
                    detail={"soc_kwh": soc_kwh, "r_kwh": r_kwh, "p_kw": p_kw},
                )
            )
    return violations


def find_double_sold(
    rows: list[tuple[str, datetime, datetime, float, float]],
) -> list[Violation]:
    """K2: sum of active reservations on a bank/interval exceeding that interval's capability --
    "kWh sold twice". `rows`: `(bank_id, interval_start, interval_end, total_reserved_kw,
    capability_kw)`, already grouped by (bank_id, interval_start) in SQL (`invariants.queries`).

    `magnitude` is the excess reserved power converted to kWh over the interval's own duration --
    `og_double_sold_kwh_total`'s unit (02b S6.6) -- not a bare violation count, since a bank oversold by
    5 kW for 15 minutes and one oversold by 500 kW for 15 minutes are not the same size of problem.
    """
    violations = []
    for bank_id, interval_start, interval_end, total_kw, capability_kw in rows:
        excess_kw = total_kw - capability_kw
        if excess_kw > _KW_TOLERANCE:
            hours = max((interval_end - interval_start).total_seconds() / 3600.0, 0.0)
            violations.append(
                Violation(
                    scope={"bank_id": bank_id, "interval_start": interval_start.isoformat()},
                    detail={
                        "total_reserved_kw": total_kw,
                        "capability_kw": capability_kw,
                        "excess_kw": excess_kw,
                    },
                    magnitude=excess_kw * hours,
                )
            )
    return violations


#: A gap in grant activity longer than this many seconds is treated as an implicit zero-delivery sample
#: (see `find_dip`'s docstring). A named default so `opengrid.invariants` can override it via
#: `[invariants].k13_max_grant_gap_s` (BUILD.md S5a: no hard-coded thresholds) without this module
#: needing to know about config at all.
DEFAULT_K13_MAX_GRANT_GAP_S = 30.0


def find_dip(
    *,
    interval_start: datetime,
    interval_end: datetime,
    committed_kw: float,
    cycles: list[tuple[datetime, float]],
    max_gap_s: float = DEFAULT_K13_MAX_GRANT_GAP_S,
) -> tuple[float, datetime] | None:
    """K13: the worst (lowest) delivery point for one committed obligation-interval, comparing the
    OBLIGATION's total delivery (already summed across every bank it holds -- see
    `invariants.queries.fetch_grant_cycle_series`'s docstring) against `committed_kw`.

    A stretch with NO grant activity at all is not "no evidence of a problem" -- it is the most severe
    possible dip, since nothing was delivered -- so a gap between two samples (or between a window
    boundary and its nearest sample) longer than `max_gap_s` counts as an implicit zero-kW sample at the
    gap's start. Verified live 2026-09-26: a mid-window silence produced no flagged violation because a
    plain `MIN()` over only the cycles that DID report looked fine, missing the silent stretch entirely.

    `cycles` must be sorted by timestamp ascending. Returns `(dip_kw, dip_at)` for the single worst point
    found (a low-water mark is enough: the lock is either held throughout the interval or it is not), or
    `None` if the interval never fell below `committed_kw` -- including no gap large enough to count --
    at all.
    """
    worst_kw: float | None = None
    worst_at: datetime | None = None

    def _consider(kw: float, at: datetime) -> None:
        nonlocal worst_kw, worst_at
        if worst_kw is None or kw < worst_kw:
            worst_kw, worst_at = kw, at

    boundary_points = [interval_start, *(ts for ts, _kw in cycles), interval_end]
    for prev, nxt in itertools.pairwise(boundary_points):
        if (nxt - prev).total_seconds() > max_gap_s:
            _consider(0.0, prev)  # nothing delivered for this stretch: the worst possible point

    for ts, total_kw in cycles:
        _consider(total_kw, ts)

    if worst_kw is None or worst_at is None or worst_kw >= committed_kw - _KW_TOLERANCE:
        return None
    return worst_kw, worst_at


def _dip_is_covered(dip_at: datetime, earliest_covering_at: datetime | None) -> bool:
    """K13 coverage policy -- the single place this rule lives, so it can change in one edit if the
    owner's policy changes (task brief). A delivery dip is an ALLOWED consequence of a committed
    obligation's SHORTFALL transition (K13's `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`/
    `R-COMMIT-LOCK-INFEASIBLE` reasons, or a recorded `R-SUBSTITUTION`/`R-AS-RELEASE`) for as long as
    that transition was already on record AT OR BEFORE the dip -- the rest of the window after a
    mid-window SHORTFALL is its recorded consequence, not a fresh, unexplained violation. A dip that
    happened BEFORE any such transition was traced is not excused by one recorded later.
    """
    return earliest_covering_at is not None and earliest_covering_at <= dip_at


def find_lock_violations(
    rows: list[tuple[str, datetime, datetime, float, float, datetime, datetime | None]],
) -> list[Violation]:
    """K13: a committed obligation's realized (granted) kW dipped below its committed floor during the
    committed interval (`invariants.find_dip`), and that dip is not covered by an already-traced K13
    exception (`_dip_is_covered`).

    `rows`: `(obligation_id, interval_start, interval_end, committed_kw, dip_kw, dip_at,
    earliest_covering_at)` -- callers (`opengrid.invariants.__init__`) only include rows where
    `find_dip` already found a dip; `earliest_covering_at` is `invariants.queries.
    fetch_earliest_covering_trace_at`'s result for that obligation/window, or `None`.
    """
    violations = []
    for (
        obligation_id,
        interval_start,
        interval_end,
        committed_kw,
        dip_kw,
        dip_at,
        earliest_covering_at,
    ) in rows:
        if _dip_is_covered(dip_at, earliest_covering_at):
            continue
        violations.append(
            Violation(
                scope={"obligation_id": obligation_id, "interval_start": interval_start.isoformat()},
                detail={
                    "committed_kw": committed_kw,
                    "dip_kw": dip_kw,
                    "dip_at": dip_at.isoformat(),
                    "interval_end": interval_end.isoformat(),
                },
            )
        )
    return violations


def find_orphan_reservations(
    rows: list[tuple[str, str, str, datetime]],
) -> list[Violation]:
    """An active reservation (`released_at IS NULL`) whose obligation holds no matching active
    commitment row for the same interval -- headroom held for an obligation the ledger no longer
    considers committed (the same condition `opengrid.ledger.release_uncommitted` fixes at `og-engine`
    start-up; this check only reports it, on its own schedule, independently of whether that start-up
    sweep ran or caught it). `rows`: `(reservation_id, obligation_id, bank_id, interval_start)`,
    pre-filtered to exactly this condition in SQL (`invariants.queries.fetch_orphan_reservations`).
    """
    return [
        Violation(
            scope={"reservation_id": reservation_id, "obligation_id": obligation_id, "bank_id": bank_id},
            detail={"interval_start": interval_start.isoformat()},
        )
        for reservation_id, obligation_id, bank_id, interval_start in rows
    ]


def find_orphan_commitments(
    rows: list[tuple[str, str, datetime, str]],
) -> list[Violation]:
    """An active commitment row (`supersedes IS NULL`) left on an obligation that has since reached a
    terminal, non-fulfilling state (`ORPHAN_COMMITMENT_OBLIGATION_STATES`) -- the commitment-lock
    bookkeeping was never released even though nothing will ever deliver against it. `rows`:
    `(commitment_id, obligation_id, interval_start, obligation_state)`, pre-filtered to exactly this
    condition in SQL (`invariants.queries.fetch_orphan_commitments`).
    """
    return [
        Violation(
            scope={"commitment_id": commitment_id, "obligation_id": obligation_id},
            detail={"interval_start": interval_start.isoformat(), "obligation_state": obligation_state},
        )
        for commitment_id, obligation_id, interval_start, obligation_state in rows
    ]
