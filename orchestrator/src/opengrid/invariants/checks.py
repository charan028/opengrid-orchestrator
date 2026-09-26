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
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from opengrid.core.physics import BankParams, HubParams, bank_capability, hub_capability
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
                    dedupe_key=f"{hub_id}|{ts.isoformat()}",
                    detail={"soc_kwh": soc_kwh, "r_kwh": r_kwh, "p_kw": p_kw},
                )
            )
    return violations


def compute_bank_capabilities_kw(
    hub_rows: list[tuple[str, float, float, float, float, float, float, float, float, str]],
) -> dict[str, float]:
    """K2's ceiling: the SAME discharge-capability formula the ledger's admission check uses
    (`opengrid.core.physics.hub_capability`/`bank_capability`, the pure functions
    `opengrid.fleet.capability()`/`opengrid.ledger`'s `CapabilityProvider` are themselves built on) --
    reused here, not re-derived, and NOT the bank's static `kva_rating` nameplate. Verified live: nameplate
    is always >= the true admitted capability once any hub is offline/low-SoC, so a double-sale that stays
    under nameplate but over the real capability went completely undetected by the old check.

    `opengrid.fleet`'s own in-memory capability singleton lives in `og-engine`'s process, not
    `og-settle`'s (where this package runs) -- this recomputes the identical formula independently from
    persisted state (`og.hub_state`/`og.hub`/`og.bank`) instead of reaching into another process's
    private runtime, which `opengrid.invariants`' whole design (independent, read-only, DB-only) requires
    anyway.

    `hub_rows`: `(bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health)` --
    one row per hub. A hub whose persisted `health` (`opengrid.health`'s own classification) is not
    `"online"` contributes zero discharge capability, matching `opengrid.fleet.capability`'s exclusion
    rule; every bank appears in the result even if every one of its hubs is excluded (capability 0.0),
    so a caller's `.get(bank_id, ...)` never has to guess a bank's ceiling from having no rows for it.
    """
    discharge_kw_by_bank: dict[str, list[float]] = {}
    bank_params_by_bank: dict[str, BankParams] = {}
    for bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health in hub_rows:
        bank_params_by_bank.setdefault(bank_id, BankParams(kva_rating=kva_rating, reserve_kva=reserve_kva))
        if health != "online":
            continue
        hub_params = HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=p_kw, eta_c=eta_c, eta_d=eta_d)
        discharge_kw, _charge_kw = hub_capability(soc_kwh, hub_params)
        discharge_kw_by_bank.setdefault(bank_id, []).append(discharge_kw)
    return {
        bank_id: bank_capability(discharge_kw_by_bank.get(bank_id, []), bank_params)
        for bank_id, bank_params in bank_params_by_bank.items()
    }


def find_double_sold(
    rows: list[tuple[str, datetime, datetime, float]],
    capability_by_bank: dict[str, float],
) -> list[Violation]:
    """K2: sum of active reservations on a bank/interval exceeding that interval's TRUE capability
    (`compute_bank_capabilities_kw`, not the bank's nameplate `kva_rating`) -- "kWh sold twice". `rows`:
    `(bank_id, interval_start, interval_end, total_reserved_kw)`, already grouped by (bank_id,
    interval_start) in SQL (`invariants.queries`). A bank missing from `capability_by_bank` (no hub rows
    at all) is treated as 0 kW capability -- any reservation there is fully oversold.

    `magnitude` is the excess reserved power converted to kWh over the interval's own duration --
    `og_double_sold_kwh_total`'s unit (02b S6.6) -- not a bare violation count, since a bank oversold by
    5 kW for 15 minutes and one oversold by 500 kW for 15 minutes are not the same size of problem.
    """
    violations = []
    for bank_id, interval_start, interval_end, total_kw in rows:
        capability_kw = capability_by_bank.get(bank_id, 0.0)
        excess_kw = total_kw - capability_kw
        if excess_kw > _KW_TOLERANCE:
            hours = max((interval_end - interval_start).total_seconds() / 3600.0, 0.0)
            violations.append(
                Violation(
                    scope={"bank_id": bank_id, "interval_start": interval_start.isoformat()},
                    dedupe_key=f"{bank_id}|{interval_start.isoformat()}",
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

#: Coverage's cycle-id epsilon (adversarial-review fix): `opengrid.allocator.__init__.run_cycle`
#: persists a cycle's grants (`ledger.persist_grants`) BEFORE it records that cycle's shortfall trace
#: (`ledger.record_shortfalls`) -- both timestamped `now()` at their own write, so the trace's
#: `created_at` is a few milliseconds AFTER the grant's for the exact cycle that started the shortfall.
#: A strict "covering trace at or before the dip" wall-clock rule then flags every legitimate
#: exception's FIRST cycle. Matching by `cycle_id` (both the grant series and the trace payload carry
#: it) sidesteps write-order/wall-clock entirely for the common same-cycle case; the timestamp rule
#: below still covers every LATER cycle in the same shortfall.
DipClassification = Literal["covered", "lock_violation", "outage_gap"]


@dataclass(frozen=True, slots=True)
class DipResult:
    """One committed interval's worst (lowest) delivery point (`find_dip`). `cycle_id` is the allocator
    cycle that reported it, or `None` when the worst point is an implicit-zero GAP rather than an actual
    reported sample (a gap sits between cycles, or between a window boundary and its nearest cycle, so
    it was never itself part of any one cycle's trace)."""

    kw: float
    at: datetime
    cycle_id: str | None
    is_gap: bool


def find_dip(
    *,
    interval_start: datetime,
    interval_end: datetime,
    committed_kw: float,
    cycles: list[tuple[datetime, float, str]],
    max_gap_s: float = DEFAULT_K13_MAX_GRANT_GAP_S,
) -> DipResult | None:
    """K13: the worst (lowest) delivery point for one committed obligation-interval, comparing the
    OBLIGATION's total delivery (already summed across every bank it holds -- see
    `invariants.queries.fetch_grant_cycle_series`'s docstring) against `committed_kw`.

    A stretch with NO grant activity at all is not "no evidence of a problem" -- it is the most severe
    possible dip, since nothing was delivered -- so a gap between two samples (or between a window
    boundary and its nearest sample) longer than `max_gap_s` counts as an implicit zero-kW sample at the
    gap's start. Verified live 2026-09-26: a mid-window silence produced no flagged violation because a
    plain `MIN()` over only the cycles that DID report looked fine, missing the silent stretch entirely.

    `cycles` must be sorted by timestamp ascending, each `(ts, total_kw, cycle_id)`. Returns the single
    worst point found (a low-water mark is enough: the lock is either held throughout the interval or it
    is not), or `None` if the interval never fell below `committed_kw` -- including no gap large enough
    to count -- at all.
    """
    worst: DipResult | None = None

    def _consider(kw: float, at: datetime, cycle_id: str | None, *, is_gap: bool) -> None:
        nonlocal worst
        if worst is None or kw < worst.kw:
            worst = DipResult(kw=kw, at=at, cycle_id=cycle_id, is_gap=is_gap)

    boundary_points = [interval_start, *(ts for ts, _kw, _cid in cycles), interval_end]
    for prev, nxt in itertools.pairwise(boundary_points):
        if (nxt - prev).total_seconds() > max_gap_s:
            _consider(0.0, prev, None, is_gap=True)  # nothing delivered: the worst possible point

    for ts, total_kw, cycle_id in cycles:
        _consider(total_kw, ts, cycle_id, is_gap=False)

    if worst is None or worst.kw >= committed_kw - _KW_TOLERANCE:
        return None
    return worst


def _dip_is_covered(
    dip: DipResult, *, earliest_covering_at: datetime | None, covered_cycle_ids: frozenset[str]
) -> bool:
    """K13 coverage policy -- the single place this rule lives, so it can change in one edit if the
    owner's policy changes (task brief). A delivery dip is an ALLOWED consequence of a committed
    obligation's SHORTFALL transition (K13's `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`/
    `R-COMMIT-LOCK-INFEASIBLE` reasons, or a recorded `R-SUBSTITUTION`/`R-AS-RELEASE`) if EITHER:
      * the dip's own `cycle_id` is one the trace already covers (`covered_cycle_ids`) -- the same-cycle
        epsilon, sidestepping the grant-before-trace write-order race entirely; or
      * a covering trace was recorded strictly AT OR BEFORE the dip's timestamp -- covers every later
        cycle of the same shortfall, including ones with no `cycle_id` at all (an implicit-zero gap).
    A dip that happened BEFORE any such transition was traced, in an EARLIER, different cycle, is not
    excused by one recorded later.
    """
    if dip.cycle_id is not None and dip.cycle_id in covered_cycle_ids:
        return True
    return earliest_covering_at is not None and earliest_covering_at <= dip.at


def classify_dip(
    dip: DipResult,
    *,
    earliest_covering_at: datetime | None,
    covered_cycle_ids: frozenset[str],
    is_need_basis: bool = False,
) -> DipClassification:
    """The other single-place policy rule (task brief #4, extended by the owner's 2026-09-26 need-basis
    decision, 00-invariants.md K13 "commitments are over a period"): a dip is `"covered"` (clean, no
    violation), else `"outage_gap"` (K13_OUTAGE_GAP: the dip came from a grant-activity GAP -- an engine
    restart or a genuine comms outage, not a realized-below-committed delivery), else `"lock_violation"`
    (K13_LOCK_VIOLATION: grants WERE flowing and still came in below the committed floor, unexplained).
    Both non-covered outcomes are still flagged -- the chaos harness and the commitment-lock proof both
    need to see an uncovered gap -- they are simply reported under different check names/counters.

    `is_need_basis` (owner decision): for an obligation whose service profile is `MEASURED_FEEDBACK`
    (DATA_CENTER, PIPELINE_AC), the committed kW is a RESERVED MAXIMUM, not a fixed schedule -- the
    customer's measured need sets delivery below it (`R-GRANT-CLOSED-LOOP`), and the reservation itself
    stays locked to the obligation the whole time. A need-basis dip is therefore compliant by definition,
    covered before the ordinary trace-based check even runs.

    Scope note (not yet implemented here, tracked for a follow-up): the owner's decision also names two
    STILL-a-violation cases this function does not yet distinguish -- (a) the reserved capacity being
    granted to a DIFFERENT obligation (today's K2 double-sold check already catches any reservation-level
    encroachment on the same bank/interval, so this is not silently unguarded, just not re-derived here)
    and (b) delivery below MEASURED need with no override reason, which needs the customer's own measured
    signal (`og.customer_site_meter_reading`/`og.corridor_current_reading`) correlated against delivered
    kW -- a genuinely different quantity comparison from "dip below committed", deliberately deferred
    rather than rushed. The K13_RESTORE_LAG check (grants not returned to the feasible remainder within 2
    cycles of a cleared SHORTFALL) is deferred with it, for the same reason.
    """
    if is_need_basis:
        return "covered"
    if _dip_is_covered(dip, earliest_covering_at=earliest_covering_at, covered_cycle_ids=covered_cycle_ids):
        return "covered"
    return "outage_gap" if dip.is_gap else "lock_violation"


def classify_lock_rows(
    rows: list[tuple[str, datetime, datetime, float, DipResult, datetime | None, frozenset[str], bool]],
) -> tuple[list[Violation], list[Violation]]:
    """K13: split already-detected dips (`find_dip`) into `K13_LOCK_VIOLATION`s and `K13_OUTAGE_GAP`s
    via `classify_dip` (the single place that decision is made), dropping covered ones entirely.

    `rows`: `(obligation_id, interval_start, interval_end, committed_kw, dip, earliest_covering_at,
    covered_cycle_ids, is_need_basis)` -- callers (`opengrid.invariants.__init__`) only include rows
    where `find_dip` already found a dip. Returns `(lock_violations, outage_gaps)`.
    """
    lock_violations: list[Violation] = []
    outage_gaps: list[Violation] = []
    for (
        obligation_id,
        interval_start,
        interval_end,
        committed_kw,
        dip,
        earliest_covering_at,
        covered_cycle_ids,
        is_need_basis,
    ) in rows:
        classification = classify_dip(
            dip,
            earliest_covering_at=earliest_covering_at,
            covered_cycle_ids=covered_cycle_ids,
            is_need_basis=is_need_basis,
        )
        if classification == "covered":
            continue
        violation = Violation(
            scope={"obligation_id": obligation_id, "interval_start": interval_start.isoformat()},
            dedupe_key=f"{obligation_id}|{interval_start.isoformat()}",
            detail={
                "committed_kw": committed_kw,
                "dip_kw": dip.kw,
                "dip_at": dip.at.isoformat(),
                "interval_end": interval_end.isoformat(),
                "is_gap": dip.is_gap,
                "cycle_id": dip.cycle_id,
            },
        )
        (outage_gaps if classification == "outage_gap" else lock_violations).append(violation)
    return lock_violations, outage_gaps


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
            dedupe_key=reservation_id,
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
            dedupe_key=commitment_id,
            detail={"interval_start": interval_start.isoformat(), "obligation_state": obligation_state},
        )
        for commitment_id, obligation_id, interval_start, obligation_state in rows
    ]
