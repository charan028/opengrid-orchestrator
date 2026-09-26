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
    hub_rows: list[tuple[str, float, float, float, float, float, float, float, float, str, int | None]],
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

    `hub_rows`: `(bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health,
    units)` -- one row per hub. `units` (`og.hub.units`, migration 0032; `None` on a database that predates
    it) feeds `hub_capability`'s unit-capped rating, which fails closed to one unit when unknown. A hub whose persisted `health` (`opengrid.health`'s own classification) is not
    `"online"` contributes zero discharge capability, matching `opengrid.fleet.capability`'s exclusion
    rule; every bank appears in the result even if every one of its hubs is excluded (capability 0.0),
    so a caller's `.get(bank_id, ...)` never has to guess a bank's ceiling from having no rows for it.
    """
    discharge_kw_by_bank: dict[str, list[float]] = {}
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
        soc_kwh,
        health,
        units,
    ) in hub_rows:
        bank_params_by_bank.setdefault(bank_id, BankParams(kva_rating=kva_rating, reserve_kva=reserve_kva))
        if health != "online":
            continue
        hub_params = HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=p_kw, eta_c=eta_c, eta_d=eta_d, units=units)
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


def find_as_hold_violations(
    rows: list[tuple[str, str, float, int, float]],
) -> list[Violation]:
    """AS capacity hold compliance (00-invariants.md S2.6/S6; migration 0020's "an award keeps
    committed_kw x duration / eta_d above the reserve floor"): while an `og.as_deployment` window covers
    an ERCOT_AS obligation, the energy actually deliverable from its reserved banks (already net of the
    reserve floor and discharge efficiency -- `opengrid.core.physics.hub_available_energy_kwh`, summed in
    SQL) must stay at or above what sustaining `committed_kw` for the product's full `duration_minutes`
    would draw. A hold that has already fallen below its requirement (e.g. SoC drifted down between
    deployments, or a hub went offline) is exactly the condition 0020 exists to prevent from being
    invisible until ERCOT actually calls the award.

    `rows`: `(deployment_id, obligation_id, committed_kw, duration_minutes, held_kwh)` -- `held_kwh`
    already summed across every bank reserved for that obligation's active interval
    (`invariants.queries.fetch_as_hold_candidates`); this function only compares it to the requirement.
    `dedupe_key` is the deployment id: the SAME deployment window falling short across repeated scans is
    one ongoing violation, not one per run.
    """
    violations = []
    for deployment_id, obligation_id, committed_kw, duration_minutes, held_kwh in rows:
        required_kwh = committed_kw * (duration_minutes / 60.0)
        if held_kwh < required_kwh - _AS_HOLD_TOLERANCE_KWH:
            violations.append(
                Violation(
                    scope={"deployment_id": deployment_id, "obligation_id": obligation_id},
                    dedupe_key=deployment_id,
                    detail={
                        "committed_kw": committed_kw,
                        "duration_minutes": duration_minutes,
                        "required_kwh": required_kwh,
                        "held_kwh": held_kwh,
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
    *, last_published_at: datetime | None, now: datetime, max_age_s: float
) -> Violation | None:
    """K11 external anchoring verification: the chain head hash must actually have been published
    (`opengrid.trace.anchoring.publish_anchor`) within `max_age_s` (the task brief's "every 15 min",
    passed with headroom by the caller -- see `invariants.__init__`'s own cadence constant). `None`
    (never anchored yet) is reported as stale rather than silently skipped -- a database that has NEVER
    anchored is exactly the fail-safe gap this check exists to catch, not a clean state.
    """
    if last_published_at is None:
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
