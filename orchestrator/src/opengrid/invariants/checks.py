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

from opengrid.allocator.energy_hold import (
    DEFAULT_AS_DEPLOYMENT_H,
    hold_energy_kwh,
    margin_kwh,
    stored_above_reserve_kwh,
)
from opengrid.allocator.models import HubSnapshot
from opengrid.core.limits import continuous_power_kw
from opengrid.core.physics import BankParams, HubParams, bank_capability
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


def compute_bank_rated_capabilities_kw(
    hub_rows: list[tuple[str, float, float, float, float, float, float, float, float, str, int | None, bool]],
) -> dict[str, float]:
    """K2's ceiling: each bank's RATED capability -- the sum of its hubs' unit-capped continuous ratings
    (`opengrid.core.limits.continuous_power_kw`: 11 kW per unit, 20 kW dual-unit, one unit when unknown),
    capped by the bank's `kva_rating - reserve_kva` (`opengrid.core.physics.bank_capability`). Both
    formulas are reused, not re-derived.

    Deliberately NOT live capability (owner ruling 2026-09-26): K2 is "the same kW sold twice", decided by
    overlapping reservations against what the bank can ever deliver. A hub going offline or running low
    on SoC after commitment shrinks live capability without anything being sold twice -- that is a
    SHORTFALL (K13), counted there. Comparing against live capability counted every such loss as a double
    sale (dev stack: K2 rose 0 -> 750 kWh while all hubs were offline). `soc_kwh`/`health` are ignored.

    `hub_rows`: `(bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health,
    units, utility_scale)` -- one row per hub (`invariants.queries.fetch_bank_capability_inputs`). A utility-scale
    hub (its bank is an `og.asset` SUBSTATION) is rated at nameplate `p_kw` by `continuous_power_kw`, never the
    home unit cap. Every bank with a hub row appears in the result.
    """
    rated_kw_by_bank: dict[str, list[float]] = {}
    bank_params_by_bank: dict[str, BankParams] = {}
    for (
        bank_id,
        kva_rating,
        reserve_kva,
        e_kwh,
        r_kwh,
        p_kw,
        eta_c,
        eta_d,
        _soc,
        _health,
        units,
        utility_scale,
    ) in hub_rows:
        bank_params_by_bank.setdefault(bank_id, BankParams(kva_rating=kva_rating, reserve_kva=reserve_kva))
        hub_params = HubParams(
            e_kwh=e_kwh,
            r_kwh=r_kwh,
            p_kw=p_kw,
            eta_c=eta_c,
            eta_d=eta_d,
            units=units,
            utility_scale=utility_scale,
        )
        rated_kw_by_bank.setdefault(bank_id, []).append(continuous_power_kw(hub_params))
    return {
        bank_id: bank_capability(rated_kw_by_bank.get(bank_id, []), bank_params)
        for bank_id, bank_params in bank_params_by_bank.items()
    }


def find_double_sold(
    rows: list[tuple[str, datetime, datetime, float]],
    capability_by_bank: dict[str, float],
) -> list[Violation]:
    """K2: sum of active reservations on a bank/interval exceeding the bank's RATED capability
    (`compute_bank_rated_capabilities_kw`) -- "kWh sold twice". A loss of live capability after
    commitment is K13's, never K2's. `rows`:
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
    #: the K13 floor in force at `at`: `committed_kw`, or for a capacity hold the deployed kW (see `HoldWindow`)
    floor_kw: float = 0.0


@dataclass(frozen=True, slots=True)
class HoldWindow:
    """One `og.as_deployment` window covering a capacity-hold obligation (`HOLD_SERVICE_TYPES`: ERCOT_AS,
    REGULATED_CAPACITY): `[start, end)` with `end = min(end_at, cancelled_at)`, and the kW it deployed --
    `|requested_kw|` capped at the commitment, or the full commitment when the deployment names none."""

    start: datetime
    end: datetime
    floor_kw: float


def hold_floor_kw(at: datetime, windows: tuple[HoldWindow, ...]) -> float:
    """K13 floor of a capacity hold at `at` (r3.4.3, DISPATCH's diagnosis): 0 kW outside every deployment
    window -- an undeployed hold is granted 0 kW with its reservation locked (R-GRANT-AS-HOLD), which is the
    commitment kept, not a dip -- and the deployed kW inside one (the largest when several overlap)."""
    return max((w.floor_kw for w in windows if w.start <= at < w.end), default=0.0)


def _hold_gap_floor(prev: datetime, nxt: datetime, windows: tuple[HoldWindow, ...]) -> tuple[float, datetime]:
    """The highest hold floor any deployment imposes during a silent stretch `[prev, nxt)`, and from when."""
    best = (0.0, prev)
    for w in windows:
        if w.start < nxt and w.end > prev and w.floor_kw > best[0]:
            best = (w.floor_kw, max(prev, w.start))
    return best


def find_dip(
    *,
    interval_start: datetime,
    interval_end: datetime,
    committed_kw: float,
    cycles: list[tuple[datetime, float, str]],
    max_gap_s: float = DEFAULT_K13_MAX_GRANT_GAP_S,
    hold_windows: tuple[HoldWindow, ...] | None = None,
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

    `hold_windows` (not None only for a capacity-hold obligation, `queries.fetch_hold_windows`): the floor is
    `hold_floor_kw` -- 0 outside every `og.as_deployment` window, the deployed kW inside one -- instead of
    `committed_kw` throughout, so an undeployed hold's 0 kW cycles (R-GRANT-AS-HOLD, no covering deployment)
    and its silent stretches are covered, while a deployed hold that under-delivers is still a dip. The worst
    point is the largest shortfall below the floor then in force; on a tie a real cycle sample is preferred
    over an implicit gap (it names the cycle, so the trace coverage can match it).
    """
    worst: DipResult | None = None
    worst_deficit = 0.0

    def _floor(at: datetime) -> float:
        return committed_kw if hold_windows is None else hold_floor_kw(at, hold_windows)

    def _consider(kw: float, at: datetime, cycle_id: str | None, floor: float, *, is_gap: bool) -> None:
        nonlocal worst, worst_deficit
        deficit = floor - kw
        if deficit <= _KW_TOLERANCE:
            return
        # samples are considered first, so a gap replaces one only when strictly worse (tie -> the sample)
        if worst is None or deficit > worst_deficit + 1e-9:
            worst, worst_deficit = DipResult(kw, at, cycle_id, is_gap, floor), deficit

    for ts, total_kw, cycle_id in cycles:
        _consider(total_kw, ts, cycle_id, _floor(ts), is_gap=False)

    boundary_points = [interval_start, *(ts for ts, _kw, _cid in cycles), interval_end]
    for prev, nxt in itertools.pairwise(boundary_points):
        if (nxt - prev).total_seconds() > max_gap_s:
            # nothing delivered: the worst possible point
            if hold_windows is None:
                _consider(0.0, prev, None, committed_kw, is_gap=True)
            else:
                floor, at = _hold_gap_floor(prev, nxt, hold_windows)
                _consider(0.0, at, None, floor, is_gap=True)

    return worst


@dataclass(frozen=True, slots=True)
class ShortfallEvent:
    """One traced SHORTFALL transition for an obligation (`opengrid.engine.gateways.
    EngineLedgerGateway.record_shortfalls`'s payload: `shortfall_kw`). `shortfall_kw <= 0` marks the
    constraint CLEARING -- the obligation should be back to its full commitment from that point."""

    at: datetime
    shortfall_kw: float
    cycle_id: str | None


def feasible_remainder_kw(committed_kw: float, shortfall_kw: float) -> float:
    """K13 best-effort shortfall (owner decision 2026-09-26): the maximum deliverable while a SHORTFALL's
    constraint holds -- the committed floor minus the traced shortfall, never negative or above
    `committed_kw` itself."""
    return min(max(committed_kw - max(shortfall_kw, 0.0), 0.0), committed_kw)


def _feasible_remainder_at(
    committed_kw: float, shortfall_events: list[ShortfallEvent], at: datetime
) -> float:
    """The feasible remainder in effect at `at`: the full commitment if no shortfall event has occurred
    yet at or before `at`, else the most recent one's remainder (`feasible_remainder_kw`)."""
    applicable = [e for e in shortfall_events if e.at <= at]
    if not applicable:
        return committed_kw
    latest = max(applicable, key=lambda e: e.at)
    return feasible_remainder_kw(committed_kw, latest.shortfall_kw)


def _dip_is_covered(
    dip: DipResult,
    *,
    earliest_covering_at: datetime | None,
    covered_cycle_ids: frozenset[str],
    committed_kw: float,
    shortfall_events: list[ShortfallEvent],
) -> bool:
    """K13 coverage policy -- the single place this rule lives, so it can change in one edit if the
    owner's policy changes (task brief). A delivery dip is an ALLOWED consequence of a committed
    obligation's SHORTFALL transition (K13's `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`/
    `R-COMMIT-LOCK-INFEASIBLE` reasons, or a recorded `R-SUBSTITUTION`/`R-AS-RELEASE`) if EITHER:
      * the dip's own `cycle_id` is one the trace already covers (`covered_cycle_ids`) -- the same-cycle
        epsilon, sidestepping the grant-before-trace write-order race entirely; or
      * a covering trace was recorded strictly AT OR BEFORE the dip's timestamp.
    A dip that happened BEFORE any such transition was traced, in an EARLIER, different cycle, is not
    excused by one recorded later.

    Owner decision 2026-09-26 (best-effort shortfall): a timestamp-covered dip during an active SHORTFALL
    is compliant ONLY if the delivered kW actually reached that moment's `feasible_remainder_kw` --
    "compliant only if they equal the feasible remainder", not just "any lower value". This refinement
    only applies when `shortfall_events` is non-empty (a SHORTFALL-sourced cover); a bare substitution or
    AS-release cover (no shortfall trace at all) is unaffected and stays a blanket cover, since those are
    not partial-delivery scenarios in the first place.
    """
    if dip.cycle_id is not None and dip.cycle_id in covered_cycle_ids:
        return True
    if earliest_covering_at is None or earliest_covering_at > dip.at:
        return False
    if shortfall_events:
        remainder = _feasible_remainder_at(committed_kw, shortfall_events, dip.at)
        return dip.kw >= remainder - _KW_TOLERANCE
    return True


#: Site-meter noise floor (owner decision 2026-09-26, K13 need-basis violation (b)): below this the site
#: isn't meaningfully drawing from the grid, so there is no real bridging need even if `p_kw` is nominally
#: positive.
DEFAULT_DATA_CENTER_NEED_THRESHOLD_KW = 1.0
#: Corridor AC mitigation is "needed" once induced current reaches this fraction of the corridor's own
#: limit -- comfortably before an actual breach, matching a proactive mitigation posture.
DEFAULT_PIPELINE_NEED_THRESHOLD_PCT = 0.9


@dataclass(frozen=True, slots=True)
class MeasuredNeedSample:
    """One customer-measured reading behind a service profile's `feedback_signal_ref`
    (`opengrid.site_ingest.latest.parse_feedback_ref`), resolved by `invariants.queries.
    fetch_measured_need_sample` for the K13 need-basis violation (b): "an under-delivery where the
    measured need exceeded delivery"."""

    kind: Literal["site_meter", "corridor"]
    field: str
    value: float
    limit: float | None  # corridor's own `limit_a`; None for site_meter
    quality: Literal["GOOD", "SUSPECT", "BAD"]


def measured_need_unmet(
    sample: MeasuredNeedSample | None,
    delivered_kw: float,
    *,
    data_center_threshold_kw: float = DEFAULT_DATA_CENTER_NEED_THRESHOLD_KW,
    pipeline_need_threshold_pct: float = DEFAULT_PIPELINE_NEED_THRESHOLD_PCT,
) -> bool:
    """K13 need-basis violation (b) (owner decision 2026-09-26): did the customer's own measured signal
    show real need for more than it actually got? A missing, non-`GOOD`, or unrecognised sample proves
    nothing either way and is never treated as a violation -- an unmet-need claim needs positive
    evidence, the mirror image of 06-service-profiles-and-power-quality.md S5.4's "missing is not
    compliant" (here, missing is also not a confirmed violation).

    DATA_CENTER (`site_meter`/`p_kw`): need is unmet if the site is still importing from the grid above
    the noise floor while delivery fell short of that import. PIPELINE_AC (`corridor`/`i_ac_a`): need is
    unmet if the induced current is within `pipeline_need_threshold_pct` of the corridor's own limit
    while essentially nothing was delivered (`delivered_kw` here is the dip's own kW, already below
    commitment by construction -- this function only decides whether that shortfall was justified).
    """
    if sample is None or sample.quality != "GOOD":
        return False
    if sample.kind == "site_meter" and sample.field == "p_kw":
        return sample.value > data_center_threshold_kw and delivered_kw < sample.value - _KW_TOLERANCE
    if (
        sample.kind == "corridor"
        and sample.field == "i_ac_a"
        and sample.limit is not None
        and sample.limit > 0
    ):
        ratio = sample.value / sample.limit
        return ratio >= pipeline_need_threshold_pct and delivered_kw <= _KW_TOLERANCE
    return False


def classify_dip(
    dip: DipResult,
    *,
    earliest_covering_at: datetime | None,
    covered_cycle_ids: frozenset[str],
    committed_kw: float = 0.0,
    shortfall_events: list[ShortfallEvent] | None = None,
    is_need_basis: bool = False,
    measured_need_is_unmet: bool = False,
) -> DipClassification:
    """The other single-place policy rule (task brief #4, extended by the owner's 2026-09-26 need-basis
    and best-effort-shortfall decisions, 00-invariants.md K13): a dip is `"covered"` (clean, no
    violation), else `"outage_gap"` (K13_OUTAGE_GAP: the dip came from a grant-activity GAP -- an engine
    restart or a genuine comms outage, not a realized-below-committed delivery), else `"lock_violation"`
    (K13_LOCK_VIOLATION: grants WERE flowing and still came in below the committed floor, unexplained).
    Both non-covered outcomes are still flagged -- the chaos harness and the commitment-lock proof both
    need to see an uncovered gap -- they are simply reported under different check names/counters.

    `is_need_basis` (owner decision): for an obligation whose service profile is `MEASURED_FEEDBACK`
    (DATA_CENTER, PIPELINE_AC), the committed kW is a RESERVED MAXIMUM, not a fixed schedule -- the
    customer's measured need sets delivery below it (`R-GRANT-CLOSED-LOOP`), and the reservation itself
    stays locked to the obligation the whole time. A need-basis dip is covered UNLESS
    `measured_need_is_unmet` (violation (b): `measured_need_unmet`) says the customer's own measured
    signal showed real need that wasn't served -- violation (a) ("reserved capacity granted to another
    obligation") is not re-derived here; K2's double-sold check already catches any reservation-level
    encroachment on the same bank/interval.
    """
    if _dip_is_covered(
        dip,
        earliest_covering_at=earliest_covering_at,
        covered_cycle_ids=covered_cycle_ids,
        committed_kw=committed_kw,
        shortfall_events=shortfall_events or [],
    ):
        return "covered"
    if is_need_basis and not measured_need_is_unmet:
        return "covered"
    return "outage_gap" if dip.is_gap else "lock_violation"


def classify_lock_rows(
    rows: list[
        tuple[
            str,
            datetime,
            datetime,
            float,
            DipResult,
            datetime | None,
            frozenset[str],
            list[ShortfallEvent],
            bool,
            bool,
        ]
    ],
) -> tuple[list[Violation], list[Violation]]:
    """K13: split already-detected dips (`find_dip`) into `K13_LOCK_VIOLATION`s and `K13_OUTAGE_GAP`s
    via `classify_dip` (the single place that decision is made), dropping covered ones entirely.

    `rows`: `(obligation_id, interval_start, interval_end, committed_kw, dip, earliest_covering_at,
    covered_cycle_ids, shortfall_events, is_need_basis, measured_need_is_unmet)` -- callers
    (`opengrid.invariants.__init__`) only include rows where `find_dip` already found a dip. Returns
    `(lock_violations, outage_gaps)`.
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
        shortfall_events,
        is_need_basis,
        measured_need_is_unmet,
    ) in rows:
        classification = classify_dip(
            dip,
            earliest_covering_at=earliest_covering_at,
            covered_cycle_ids=covered_cycle_ids,
            committed_kw=committed_kw,
            shortfall_events=shortfall_events,
            is_need_basis=is_need_basis,
            measured_need_is_unmet=measured_need_is_unmet,
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
                "is_need_basis": is_need_basis,
            },
        )
        (outage_gaps if classification == "outage_gap" else lock_violations).append(violation)
    return lock_violations, outage_gaps


def find_restore_lag_violations(
    rows: list[
        tuple[str, datetime, datetime, float, list[tuple[datetime, float, str]], list[ShortfallEvent]]
    ],
    *,
    restore_lag_cycles: int = 2,
) -> list[Violation]:
    """K13_RESTORE_LAG (owner decision 2026-09-26): once a SHORTFALL's constraint clears (a traced
    `shortfall_kw <= 0`), the commitment must be restored to its full `committed_kw` within
    `restore_lag_cycles` grant cycles -- "the owner requires restoring the full commitment as soon as
    possible". A clear with fewer than `restore_lag_cycles` grant samples after it in the window is not
    judged yet (not enough has elapsed to call it a lag); that is picked up on a later run once more
    cycles land, by the same watermark-driven re-scan every other K13 check uses.

    `rows`: `(obligation_id, interval_start, interval_end, committed_kw, cycles, shortfall_events)` --
    the SAME per-cycle grant series `find_dip` uses, and every traced SHORTFALL event in the window
    (`invariants.queries.fetch_shortfall_events`), for commitments that had at least one.
    """
    violations: list[Violation] = []
    for obligation_id, interval_start, _interval_end, committed_kw, cycles, shortfall_events in rows:
        clears = [e for e in shortfall_events if e.shortfall_kw <= _KW_TOLERANCE]
        for clear in clears:
            after = sorted((c for c in cycles if c[0] > clear.at), key=lambda c: c[0])
            window = after[:restore_lag_cycles]
            if len(window) < restore_lag_cycles:
                continue  # not enough cycles have elapsed yet to judge -- avoid a false positive
            if any(total_kw >= committed_kw - _KW_TOLERANCE for _ts, total_kw, _cid in window):
                continue
            violations.append(
                Violation(
                    scope={"obligation_id": obligation_id, "interval_start": interval_start.isoformat()},
                    dedupe_key=f"{obligation_id}|{interval_start.isoformat()}|{clear.at.isoformat()}",
                    detail={
                        "committed_kw": committed_kw,
                        "cleared_at": clear.at.isoformat(),
                        "cycles_checked": len(window),
                        "max_kw_in_window": max((c[1] for c in window), default=0.0),
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


_AS_HOLD_TOLERANCE_KWH = 1e-6


@dataclass(frozen=True, slots=True)
class HoldReservation:
    """One active POWER_KW reservation covering `now` (`invariants.queries.fetch_as_hold_inputs`).
    `deployment_end` is set when an active `og.as_deployment` covers this obligation now (its own row, or
    a fleet-wide NULL-obligation row for an ERCOT_AS award)."""

    obligation_id: str
    bank_id: str
    kw: float
    interval_start: datetime
    interval_end: datetime
    service_type: str
    duration_minutes: int | None
    deployment_id: str | None
    deployment_end: datetime | None

    @property
    def is_as(self) -> bool:
        return self.service_type == "ERCOT_AS"


def _energy_owed_kwh(res: HoldReservation, now: datetime, eta_d: float) -> float:
    """Stored kWh a reservation needs from its bank from `now` on (`allocator.energy_hold.hold_energy_kwh`,
    reused): a HELD AS award its full deployment (kW x product duration); a DEPLOYED award kW x the
    deployment's remaining window (never more than the product duration); any other reservation kW x the
    rest of its interval."""
    if res.is_as:
        duration_h = (res.duration_minutes / 60.0) if res.duration_minutes else DEFAULT_AS_DEPLOYMENT_H
        if res.deployment_end is not None:
            duration_h = min(duration_h, max((res.deployment_end - now).total_seconds() / 3600.0, 0.0))
    else:
        duration_h = max((res.interval_end - now).total_seconds() / 3600.0, 0.0)
    return hold_energy_kwh(res.kw, duration_h, eta_d)


def find_as_hold_violations(
    reservations: list[HoldReservation],
    hubs_by_bank: dict[str, list[HubSnapshot]],
    *,
    now: datetime,
) -> list[Violation]:
    """AS capacity hold compliance (00-invariants.md S2.6/S6, migration 0020; NPRR1282): every committed
    ERCOT_AS award must keep, on the banks it reserves, the stored energy its obligation needs -- measured
    the same way the allocator protects it (`opengrid.allocator.energy_hold`, reused, not re-derived):

    - HELD (no active deployment): `committed kW x product duration / eta_d` -- the case this check
      exists for: a hold that erodes while undeployed stays invisible until ERCOT calls it;
    - DEPLOYED: `kW x remaining deployment window / eta_d`;
    - per bank, the energy available is stored energy above reserve on healthy hubs with a live SoC,
      minus the G-01-ENERGY margin, minus what EVERY OTHER active reservation on that bank owes (other
      holds in full, firm reservations for the rest of their interval) -- another obligation's claim on
      the same kWh can never mask a shortfall.

    One violation per award short of its need across its banks. `dedupe_key`: the deployment id while
    deployed, else `held|<obligation>|<interval_start>` -- one ongoing condition, not one per run.
    """
    by_bank: dict[str, list[HoldReservation]] = {}
    for res in reservations:
        by_bank.setdefault(res.bank_id, []).append(res)

    available_by_bank: dict[str, tuple[float, float]] = {}  # bank -> (available stored kWh, eta_d)
    for bank_id in by_bank:
        live = [h for h in hubs_by_bank.get(bank_id, []) if h.soc_kwh is not None and h.is_healthy]
        eta_d = sum(h.eta_d for h in live) / len(live) if live else 1.0
        available_by_bank[bank_id] = (stored_above_reserve_kwh(live) - margin_kwh(live), eta_d)

    awards: dict[str, list[HoldReservation]] = {}
    for res in reservations:
        if res.is_as:
            awards.setdefault(res.obligation_id, []).append(res)

    violations = []
    for obligation_id, own in awards.items():
        required_kwh = 0.0
        available_kwh = 0.0
        for res in own:
            bank_available, eta_d = available_by_bank[res.bank_id]
            others = sum(
                _energy_owed_kwh(o, now, eta_d)
                for o in by_bank[res.bank_id]
                if o.obligation_id != obligation_id
            )
            required_kwh += _energy_owed_kwh(res, now, eta_d)
            available_kwh += max(bank_available - others, 0.0)
        if available_kwh < required_kwh - _AS_HOLD_TOLERANCE_KWH:
            first = own[0]
            deployed = first.deployment_id is not None
            violations.append(
                Violation(
                    scope={
                        "obligation_id": obligation_id,
                        "deployment_id": first.deployment_id,
                        "banks": sorted({r.bank_id for r in own}),
                    },
                    dedupe_key=(
                        str(first.deployment_id)
                        if deployed
                        else f"held|{obligation_id}|{first.interval_start.isoformat()}"
                    ),
                    detail={
                        "state": "DEPLOYED" if deployed else "HELD",
                        "committed_kw": sum(r.kw for r in own),
                        "duration_minutes": first.duration_minutes,
                        "required_kwh": required_kwh,
                        "available_kwh": available_kwh,
                    },
                )
            )
    return violations


_FLOW_LIMIT_TOLERANCE_KW = 1e-6


def find_flow_limit_violations(
    rows: list[tuple[str, str, float, float | None, float | None]],
) -> list[Violation]:
    """Discharge-flow limit (00-invariants.md S2.6/S6; 09-optimizer-dispatcher-update.md G-26..G-29/G-31):
    a measured net power exceeding its own physical/contractual ceiling, at any of the five levels the
    guardian itself enforces against (migrations 0027/0029) -- independently re-derived here, not reused
    from the guardian's own runtime.

    `rows`: `(scope_kind, scope_id, net_kw, forward_limit_kw, reverse_limit_kw)`, one row per
    scope instance, already aggregated in SQL (`invariants.queries.fetch_flow_limit_candidates`):
      * `"home_meter"`   -- one hub's `og.hub_state.meter_kw` vs. its own `og.hub.export_limit_kw`
                            (G-26; forward/import has no configured limit here, so `forward_limit_kw`
                            is always `None` for this scope).
      * `"hub_discharge_derate"` -- one hub's `p_kw` vs. its own telemetry-reported `p_dis_max_kw`
                            (0027; the BMS's OWN derated ceiling, not the hub's static nameplate `p_kw`
                            rating -- a hub can derate itself below nameplate and this must track that).
      * `"transformer"`  -- `SUM(p_kw)` of every hub naming a `og.service_transformer` row, vs. its
                            `rating_kva` (G-27; symmetric, same ceiling both directions).
      * `"feeder"`       -- `SUM(p_kw)` of every hub on a bank naming a `og.feeder_limit` row, vs. its
                            `thermal_kw` (forward/import) and `reverse_kw` (reverse/export) (G-28).
      * `"substation"`   -- `SUM(p_kw)` of every hub on a bank under a `og.substation_limit` row, vs.
                            its `rating_kva` (forward) and `reverse_kw` (reverse) (G-29).

    Sign convention matches the rest of this package: `net_kw` positive = import/charge (checked against
    `forward_limit_kw`), negative = export/discharge (checked against `reverse_limit_kw`, compared on
    magnitude). A `None` limit means that field is absent for that scope instance (no configured row, or
    a telemetry field a hub hasn't reported yet) -- schema-adaptive per the task brief: that direction is
    simply not checked, never treated as a zero ceiling. Transformer/feeder/substation P_max derating
    beyond `hub_discharge_derate`'s own per-hub telemetry is not separately measured -- 0027 ships no
    aggregate-level derate field, only the per-hub one already covered above.
    """
    violations = []
    for scope_kind, scope_id, net_kw, forward_limit_kw, reverse_limit_kw in rows:
        if (
            net_kw < 0
            and reverse_limit_kw is not None
            and -net_kw > reverse_limit_kw + _FLOW_LIMIT_TOLERANCE_KW
        ):
            violations.append(
                Violation(
                    scope={"scope_kind": scope_kind, "scope_id": scope_id, "direction": "reverse"},
                    dedupe_key=f"{scope_kind}|{scope_id}|reverse",
                    detail={"net_kw": net_kw, "reverse_limit_kw": reverse_limit_kw},
                )
            )
        elif (
            net_kw > 0
            and forward_limit_kw is not None
            and net_kw > forward_limit_kw + _FLOW_LIMIT_TOLERANCE_KW
        ):
            violations.append(
                Violation(
                    scope={"scope_kind": scope_kind, "scope_id": scope_id, "direction": "forward"},
                    dedupe_key=f"{scope_kind}|{scope_id}|forward",
                    detail={"net_kw": net_kw, "forward_limit_kw": forward_limit_kw},
                )
            )
    return violations


def find_anchor_staleness_violation(
    *,
    last_published_at: datetime | None,
    now: datetime,
    max_age_s: float,
    never_anchored_grace_until: datetime | None = None,
) -> Violation | None:
    """K11 external anchoring verification: the chain head hash must actually have been published
    (`opengrid.trace.anchoring.publish_anchor`) within `max_age_s` (the task brief's "every 15 min",
    passed with headroom by the caller -- see `invariants.__init__`'s own cadence constant). `None`
    (never anchored yet) is reported as stale rather than silently skipped -- a database that has NEVER
    anchored is exactly the fail-safe gap this check exists to catch, not a clean state -- but only once
    `never_anchored_grace_until` has passed: a freshly built database has had no chance to anchor yet
    (R2 rebuild, 2026-09-26: ANCHOR_FRESHNESS = 1 before the first publish), so the writer gets the same
    `max_age_s` to produce its first anchor that it gets between any two.
    """
    if last_published_at is None:
        if never_anchored_grace_until is not None and now < never_anchored_grace_until:
            return None
        return Violation(
            scope={"check": "anchor_freshness"},
            dedupe_key="never_anchored",
            detail={"last_published_at": None, "max_age_s": max_age_s},
        )
    age_s = (now - last_published_at).total_seconds()
    if age_s > max_age_s:
        return Violation(
            scope={"check": "anchor_freshness"},
            dedupe_key=f"stale|{last_published_at.isoformat()}",
            detail={
                "last_published_at": last_published_at.isoformat(),
                "age_s": age_s,
                "max_age_s": max_age_s,
            },
        )
    return None


def find_territory_violations(
    rows: list[tuple[str, str, str, str, str, tuple[str, ...], datetime]],
) -> list[Violation]:
    """K15 territory (00-invariants.md S2.6/S6, migration 0025's market model): a REGULATED obligation
    delivered (a real, non-headroom grant) from a bank whose zone is outside its utility's own
    `territory_zones`. A FREE-market obligation has no utility at all and never reaches this function
    (`invariants.queries.fetch_territory_candidates` joins `og.contract.market = 'REGULATED'` in SQL, so
    every row here already names a utility to check against).

    `rows`: `(grant_id, obligation_id, bank_id, zone, utility_id, territory_zones, created_at)` --
    pre-joined in SQL (`fetch_territory_candidates`); this function only decides membership, so it stays
    unit-testable without a database.

    `dedupe_key` is `f"{obligation_id}|{bank_id}"`, not the grant id: the same obligation/bank pairing
    delivering out-of-territory across many grant cycles is ONE ongoing territory violation, not one per
    cycle (mirrors K13/K2's "a persisting condition is the same violation" idempotency, avoiding a
    violation row per allocator cycle for a chronic misconfiguration)."""
    violations = []
    for grant_id, obligation_id, bank_id, zone, utility_id, territory_zones, created_at in rows:
        if zone not in territory_zones:
            violations.append(
                Violation(
                    scope={"obligation_id": obligation_id, "bank_id": bank_id, "utility_id": utility_id},
                    dedupe_key=f"{obligation_id}|{bank_id}",
                    detail={
                        "grant_id": grant_id,
                        "zone": zone,
                        "territory_zones": list(territory_zones),
                        "detected_at": created_at.isoformat(),
                    },
                )
            )
    return violations
