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

from datetime import datetime

from opengrid.invariants.models import (
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_ORPHAN_RESERVATION,
    Violation,
)

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


def find_lock_violations(
    rows: list[tuple[str, datetime, datetime, float, float, bool]],
) -> list[Violation]:
    """K13: a committed obligation's realized (granted) kW dipped below its committed floor during the
    committed interval, with no allowed override/substitution reason code found in the trace for that
    obligation in that window.

    `rows`: `(obligation_id, interval_start, interval_end, committed_kw, min_granted_kw,
    has_allowed_reason)` -- `min_granted_kw` is the worst (lowest) grant seen for the obligation across
    the interval (a single low-water mark is enough: the lock is either held throughout or it is not);
    `has_allowed_reason` is a trace lookup already done in SQL (`invariants.queries.fetch_lock_candidates`
    joins `og.trace.reason_codes` against `opengrid.core.reasons.COMMIT_LOCK_OVERRIDE_REASONS |
    {R_AS_RELEASE, R_SUBSTITUTION}` -- 00-invariants.md K13's own exception list, never re-declared here).
    """
    violations = []
    for obligation_id, interval_start, interval_end, committed_kw, min_granted_kw, has_allowed_reason in rows:
        if min_granted_kw < committed_kw - _KW_TOLERANCE and not has_allowed_reason:
            violations.append(
                Violation(
                    scope={"obligation_id": obligation_id, "interval_start": interval_start.isoformat()},
                    detail={
                        "committed_kw": committed_kw,
                        "min_granted_kw": min_granted_kw,
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


#: Maps each check name to the `Violation`-producing function above (used by `invariants.__init__` to
#: keep the run loop generic rather than hand-listing every check by name twice).
CHECK_FUNCTIONS = {
    CHECK_K1_RESERVE_BREACH: find_reserve_breaches,
    CHECK_K2_DOUBLE_SOLD: find_double_sold,
    CHECK_K13_LOCK_VIOLATION: find_lock_violations,
    CHECK_ORPHAN_RESERVATION: find_orphan_reservations,
    CHECK_ORPHAN_COMMITMENT: find_orphan_commitments,
}
