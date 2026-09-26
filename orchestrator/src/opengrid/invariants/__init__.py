"""opengrid.invariants -- independent, read-only measurement of 00-invariants.md K1 (reserve breach),
K2 (double-sold kWh), K13 (commitment-lock violation / outage gap), orphan reservation/commitment
bookkeeping, and K11 (scheduled trace-chain verification). Owner: invariants agent (BUILD.md S4).

Why this package exists: `og_reserve_breaches_total`/`og_double_sold_kwh_total`
(`opengrid.platform.metrics`) were declared in 02b S6.6 but never incremented anywhere, and the API's
`GET /og/api/health` / control-room SSE stream hard-coded their three invariant counters to 0 -- a
correct value for a healthy run, but not a MEASUREMENT (`api/routers/health.py`/`dispatch.py`'s own
prior docstrings said as much). This package makes those numbers real: it re-derives each invariant
from what actually landed in the database, on its own schedule, independently of the write paths
(`opengrid.core.limits`, `opengrid.ledger`) that already try to prevent the violation in the first place
-- so a bug in (or a write that bypassed) the preventive path is still caught, matching each invariant's
"independent check" column in `docs/orchestrator/07-delivery/00-invariants.md`.

Structure (BUILD.md S5a "pure logic separated from I/O", mirroring `opengrid.health`/`opengrid.trace`):
  * `models`      -- pure data shapes (`Violation`, `CheckOutcome`, `CheckState`, `InvariantsSummary`).
  * `checks`      -- pure violation-detection logic, no I/O, unit-testable against seeded fixtures.
  * `queries`     -- all Postgres I/O, including `read_summary()` (the API's read path) and the
                      `og.invariant_check`/`og.invariant_violation` persistence (migrations/
                      0014_invariant_checks.sql, 0015_invariant_violation_dedupe_and_trace_watermark.sql).
  * `trace_verify`-- scheduled K11 verification, built on `opengrid.trace.store.TraceStore.verify`.
  * this module   -- wiring: `configure()`/`run_due()`, called as a small hook from
                      `opengrid.health.configure()`/`evaluate_once()` (see that package's own docstring
                      for why: this checker piggybacks on `og-settle`'s existing health cadence rather
                      than getting its own process).

Every check is incremental where it can be (a durable watermark in `og.invariant_check`, or a per-stream
row for K11) and bounded where a true watermark doesn't apply (a rolling time window, or an
already-small active-rows-only subset) -- see `queries.py`'s module docstring. `run_due()` gates each
check family on its OWN interval via `opengrid.platform.process.Cadence`, independent of the ~5s cadence
it is actually called on. K1's own check additionally LOOPS its incremental fetch until caught up (or a
time budget elapses) -- a single un-looped batch per run falls behind indefinitely once a backlog grows
past one batch's size (adversarial-review fix, `_run_reserve_breach_check`).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.invariants import checks, queries
from opengrid.invariants import trace_verify as _trace_verify
from opengrid.invariants.models import (
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_K13_OUTAGE_GAP,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_ORPHAN_RESERVATION,
    CHECK_TRACE_VERIFY,
    CheckOutcome,
    InvariantsSummary,
    Violation,
)
from opengrid.invariants.queries import read_summary
from opengrid.platform import metrics
from opengrid.platform.config import Config
from opengrid.platform.process import Cadence
from opengrid.trace.pg_backend import PgTraceBackend
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 60.0  # K1/K2/K13/orphan checks
_DEFAULT_TRACE_VERIFY_INTERVAL_S = 300.0  # K11: "every N minutes" (task brief) -- default 5 min
_DEFAULT_K2_LOOKBACK_S = 3600.0  # K2's rolling aggregation window: 1h back from "now"
_DEFAULT_K1_TIME_BUDGET_S = 5.0  # K1's per-run loop cap -- see _run_reserve_breach_check
_DEFAULT_K1_SAFETY_MARGIN_S = 30.0  # K1's upper-bound holdback for late/buffered writes and clock skew
_K1_BATCH_LIMIT = 5_000  # rows per fetch inside K1's loop; matches queries' own default batch cap
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Module-level wiring, matching opengrid.health's own no-argument public interface (BUILD.md's stub
# packages convention): set once by configure(), called from opengrid.health.configure().
_pool: AsyncConnectionPool | None = None
_trace_store: TraceStore | None = None
_cadence: Cadence | None = None
_trace_cadence: Cadence | None = None
_k2_lookback_s: float = _DEFAULT_K2_LOOKBACK_S
_k13_max_grant_gap_s: float = checks.DEFAULT_K13_MAX_GRANT_GAP_S
_k1_time_budget_s: float = _DEFAULT_K1_TIME_BUDGET_S
_k1_safety_margin_s: float = _DEFAULT_K1_SAFETY_MARGIN_S


def configure(pool: AsyncConnectionPool, cfg: Config) -> None:
    """Wire the module-level singletons. Called once from `opengrid.health.configure()` (this package
    has no process entry point of its own -- it runs inside `og-settle` via `run_due()`)."""
    global \
        _pool, \
        _trace_store, \
        _cadence, \
        _trace_cadence, \
        _k2_lookback_s, \
        _k13_max_grant_gap_s, \
        _k1_time_budget_s, \
        _k1_safety_margin_s
    _pool = pool
    _trace_store = TraceStore(PgTraceBackend(pool))
    _cadence = Cadence(float(cfg.get("invariants.interval_s", _DEFAULT_INTERVAL_S)))
    _trace_cadence = Cadence(
        float(cfg.get("invariants.trace_verify_interval_s", _DEFAULT_TRACE_VERIFY_INTERVAL_S))
    )
    _k2_lookback_s = float(cfg.get("invariants.k2_lookback_s", _DEFAULT_K2_LOOKBACK_S))
    _k13_max_grant_gap_s = float(
        cfg.get("invariants.k13_max_grant_gap_s", checks.DEFAULT_K13_MAX_GRANT_GAP_S)
    )
    _k1_time_budget_s = float(cfg.get("invariants.k1_time_budget_s", _DEFAULT_K1_TIME_BUDGET_S))
    _k1_safety_margin_s = float(cfg.get("invariants.k1_safety_margin_s", _DEFAULT_K1_SAFETY_MARGIN_S))


def _require_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("opengrid.invariants.configure() must be called before use")
    return _pool


def _require_trace_store() -> TraceStore:
    if _trace_store is None:
        raise RuntimeError("opengrid.invariants.configure() must be called before use")
    return _trace_store


def _now() -> datetime:
    """Factored out so tests can inject a fixed instant (BUILD.md S5a: no flaky sleeps)."""
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


async def run_due() -> None:
    """The hook `opengrid.health.evaluate_once()` calls every health cycle (BUILD.md task brief: "add a
    call from the health evaluation loop ... keep your edit to a small hook"). Each `Cadence` gate
    independently decides whether enough wall-clock time has actually elapsed since it last ran --
    being invoked on health's ~5s cadence does not mean these checks run every ~5s.

    Not configured (module never wired, e.g. a unit test of `health` alone), or a DB/backend error inside
    either check family, is a logged no-op, never an exception raised into the health evaluator that
    called this hook -- this checker must never take down the health cycle it piggybacks on (K7)."""
    if _cadence is not None and _cadence.due():
        try:
            await run_once()
        except Exception:
            logger.exception("invariants.run_once failed")
    if _trace_cadence is not None and _trace_cadence.due():
        try:
            await run_trace_verify_once()
        except Exception:
            logger.exception("invariants.run_trace_verify_once failed")


async def run_once() -> dict[str, CheckOutcome]:
    """One pass of every DB-state check (K1, K2, K13's two classifications, the two orphan checks).
    Each check persists its own result and updates its own Prometheus metric independently, so one
    check's failure (a bad row, a transient DB hiccup) never blocks the others -- mirrors
    `opengrid.health.evaluate_alerts`' per-rule independence."""
    pool = _require_pool()
    now = _now()
    outcomes: dict[str, CheckOutcome] = {}
    for check_name, runner in (
        (CHECK_K1_RESERVE_BREACH, _run_reserve_breach_check),
        (CHECK_K2_DOUBLE_SOLD, _run_double_sold_check),
        (CHECK_ORPHAN_RESERVATION, _run_orphan_reservation_check),
        (CHECK_ORPHAN_COMMITMENT, _run_orphan_commitment_check),
    ):
        try:
            outcomes[check_name] = await runner(pool, now)
        except Exception:
            logger.exception("invariants check failed", extra={"check": check_name})
    try:
        outcomes.update(await _run_k13_checks(pool, now))
    except Exception:
        logger.exception("invariants check failed", extra={"check": "K13"})
    return outcomes


async def _persist_and_meter(
    pool: AsyncConnectionPool,
    check_name: str,
    outcome: CheckOutcome,
    *,
    prior_total: float,
    run_ms: int,
) -> list[Violation]:
    """Upsert this run's violations (idempotent on `(check_name, dedupe_key)`) and persist the check's
    durable state. Returns only the violations that were ACTUALLY NEW this run -- callers use this list,
    not `outcome.violations`, to advance their Prometheus counter, so a condition still true on the next
    run is counted once, not once per run (adversarial-review fix -- see `queries.insert_violations`)."""
    newly_inserted = await queries.insert_violations(pool, check_name, list(outcome.violations))
    new_magnitude = sum(v.magnitude for v in newly_inserted)
    total = prior_total + new_magnitude
    await queries.upsert_check_state(
        pool,
        check_name,
        ran_at=_now(),
        run_ms=run_ms,
        violation_count=outcome.count,
        total_violations=total,
        watermark=outcome.watermark,
    )
    metrics.invariant_check_last_run_timestamp_seconds.labels(check=check_name).set(_now().timestamp())
    metrics.invariant_check_duration_seconds.labels(check=check_name).observe(run_ms / 1000.0)
    return newly_inserted


async def _run_reserve_breach_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    """K1: loop the incremental fetch (compound `(ts, hub_id)` cursor, upper-bounded at
    `now - k1_safety_margin_s`) until either a fetch returns fewer than a full batch (caught up to the
    upper bound) or `k1_time_budget_s` elapses -- a single un-looped batch here falls behind indefinitely
    once discharging-hub volume exceeds one batch per run interval (adversarial-review fix). Reports its
    own lag (`og_invariant_check_lag_seconds`) so a check that IS falling behind is visible rather than
    silently accumulating backlog."""
    loop_start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_K1_RESERVE_BREACH)
    cursor_ts = _parse_ts(state.watermark.get("since_ts")) or _EPOCH
    cursor_hub_id = str(state.watermark.get("since_hub_id", ""))
    upper_bound = now - timedelta(seconds=_k1_safety_margin_s)

    all_violations: list[Violation] = []
    caught_up = True
    while True:
        rows, new_cursor = await queries.fetch_reserve_breach_candidates(
            pool,
            since_ts=cursor_ts,
            since_hub_id=cursor_hub_id,
            upper_bound=upper_bound,
            limit=_K1_BATCH_LIMIT,
        )
        all_violations.extend(checks.find_reserve_breaches(rows))
        if new_cursor is not None:
            cursor_ts, cursor_hub_id = new_cursor
        if len(rows) < _K1_BATCH_LIMIT:
            break  # caught up to upper_bound
        if time.perf_counter() - loop_start > _k1_time_budget_s:
            caught_up = False
            break

    outcome = CheckOutcome(
        CHECK_K1_RESERVE_BREACH,
        tuple(all_violations),
        {"since_ts": cursor_ts.isoformat(), "since_hub_id": cursor_hub_id},
    )
    run_ms = int((time.perf_counter() - loop_start) * 1000)
    newly_inserted = await _persist_and_meter(
        pool, CHECK_K1_RESERVE_BREACH, outcome, prior_total=state.total_violations, run_ms=run_ms
    )
    if newly_inserted:
        metrics.reserve_breaches_total.inc(len(newly_inserted))

    lag_s = max((upper_bound - cursor_ts).total_seconds(), 0.0)
    metrics.invariant_check_lag_seconds.labels(check=CHECK_K1_RESERVE_BREACH).set(lag_s)
    if not caught_up:
        logger.warning(
            "K1 reserve-breach check did not catch up within its time budget",
            extra={"lag_s": lag_s, "time_budget_s": _k1_time_budget_s},
        )
    return outcome


async def _run_double_sold_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    """K2: compares each bank/interval's summed reservations against that bank's TRUE capability
    (`checks.compute_bank_capabilities_kw`, reusing `opengrid.core.physics`'s hub/bank capability
    formula), not the bank's static `kva_rating` nameplate (adversarial-review fix)."""
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_K2_DOUBLE_SOLD)
    horizon_start = now - timedelta(seconds=_k2_lookback_s)
    rows = await queries.fetch_reservation_aggregates(pool, horizon_start=horizon_start)
    hub_rows = await queries.fetch_bank_capability_inputs(pool)
    capability_by_bank = checks.compute_bank_capabilities_kw(hub_rows)
    violations = checks.find_double_sold(rows, capability_by_bank)
    outcome = CheckOutcome(
        CHECK_K2_DOUBLE_SOLD, tuple(violations), {"horizon_start": horizon_start.isoformat()}
    )
    newly_inserted = await _persist_and_meter(
        pool,
        CHECK_K2_DOUBLE_SOLD,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    if newly_inserted:
        metrics.double_sold_kwh_total.inc(sum(v.magnitude for v in newly_inserted))
    return outcome


async def _run_k13_checks(pool: AsyncConnectionPool, now: datetime) -> dict[str, CheckOutcome]:
    """K13: enumerate elapsed commitment intervals since the watermark, find each one's worst delivery
    point across ALL of the obligation's banks combined (`checks.find_dip`), and -- only for the ones
    that actually dipped -- look up whether/how the trace already covers it
    (`checks.classify_dip`/`queries.fetch_covering_trace_info`). Splits the result into
    `K13_LOCK_VIOLATION` (grants were flowing, still below committed) and `K13_OUTAGE_GAP` (a total
    grant-activity gap) -- two persisted checks sharing this one scan, since the classification decision
    on each dip is made once, right here."""
    start = time.perf_counter()
    lock_state = await queries.get_check_state(pool, CHECK_K13_LOCK_VIOLATION)
    outage_state = await queries.get_check_state(pool, CHECK_K13_OUTAGE_GAP)
    since = _parse_ts(lock_state.watermark.get("since")) or _EPOCH
    candidates, new_end = await queries.fetch_lock_commitment_candidates(pool, since=since, now=now)
    need_basis_obligation_ids = await queries.fetch_need_basis_obligation_ids(
        pool, [obligation_id for obligation_id, *_ in candidates]
    )

    dip_rows: list[
        tuple[str, datetime, datetime, float, checks.DipResult, datetime | None, frozenset[str], bool]
    ] = []
    for obligation_id, interval_start, interval_end, committed_kw in candidates:
        cycles = await queries.fetch_grant_cycle_series(
            pool, obligation_id=obligation_id, window_start=interval_start, window_end=interval_end
        )
        dip = checks.find_dip(
            interval_start=interval_start,
            interval_end=interval_end,
            committed_kw=committed_kw,
            cycles=cycles,
            max_gap_s=_k13_max_grant_gap_s,
        )
        if dip is None:
            continue
        earliest_covering_at, covered_cycle_ids = await queries.fetch_covering_trace_info(
            pool, obligation_id=obligation_id, window_start=interval_start, window_end=interval_end
        )
        dip_rows.append(
            (
                str(obligation_id),
                interval_start,
                interval_end,
                committed_kw,
                dip,
                earliest_covering_at,
                covered_cycle_ids,
                str(obligation_id) in need_basis_obligation_ids,
            )
        )

    lock_violations, outage_gaps = checks.classify_lock_rows(dip_rows)
    watermark = {"since": (new_end or since).isoformat()}
    lock_outcome = CheckOutcome(CHECK_K13_LOCK_VIOLATION, tuple(lock_violations), watermark)
    outage_outcome = CheckOutcome(CHECK_K13_OUTAGE_GAP, tuple(outage_gaps), watermark)

    run_ms = int((time.perf_counter() - start) * 1000)
    lock_new = await _persist_and_meter(
        pool, CHECK_K13_LOCK_VIOLATION, lock_outcome, prior_total=lock_state.total_violations, run_ms=run_ms
    )
    outage_new = await _persist_and_meter(
        pool, CHECK_K13_OUTAGE_GAP, outage_outcome, prior_total=outage_state.total_violations, run_ms=run_ms
    )
    if lock_new:
        metrics.lock_violations_total.inc(len(lock_new))
    if outage_new:
        metrics.k13_outage_gap_total.inc(len(outage_new))
    return {CHECK_K13_LOCK_VIOLATION: lock_outcome, CHECK_K13_OUTAGE_GAP: outage_outcome}


async def _run_orphan_reservation_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_ORPHAN_RESERVATION)
    rows = await queries.fetch_orphan_reservations(pool)
    violations = checks.find_orphan_reservations(rows)
    outcome = CheckOutcome(CHECK_ORPHAN_RESERVATION, tuple(violations), {})
    await _persist_and_meter(
        pool,
        CHECK_ORPHAN_RESERVATION,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    metrics.orphan_reservations.set(len(violations))
    return outcome


async def _run_orphan_commitment_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_ORPHAN_COMMITMENT)
    rows = await queries.fetch_orphan_commitments(pool)
    violations = checks.find_orphan_commitments(rows)
    outcome = CheckOutcome(CHECK_ORPHAN_COMMITMENT, tuple(violations), {})
    await _persist_and_meter(
        pool,
        CHECK_ORPHAN_COMMITMENT,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    metrics.orphan_commitments.set(len(violations))
    return outcome


async def run_trace_verify_once() -> _trace_verify.TraceVerifyOutcome:
    """K11: discover any new trace stream since the last discovery cursor, verify every known stream's
    new segments since its OWN row's resume point (`og.invariant_trace_watermark`), and raise/clear
    `ALR-TRACE-VERIFY-FAILED` through the health alert mechanism on failure/recovery."""
    pool = _require_pool()
    trace_store = _require_trace_store()
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_TRACE_VERIFY)
    discovery_since = _parse_ts(state.watermark.get("discovery_since")) or _EPOCH
    run_started_at = _now()  # the NEXT discovery cursor: see verify_new_segments' docstring for why

    outcome = await _trace_verify.verify_new_segments(pool, trace_store, discovery_since=discovery_since)
    await _trace_verify.raise_or_clear_alert(pool, outcome)

    violations = [
        Violation(scope={"stream_id": stream_id}, dedupe_key=stream_id)
        for stream_id in outcome.failed_streams
    ]
    newly_inserted = await queries.insert_violations(pool, CHECK_TRACE_VERIFY, violations)
    run_ms = int((time.perf_counter() - start) * 1000)
    total = state.total_violations + len(newly_inserted)
    await queries.upsert_check_state(
        pool,
        CHECK_TRACE_VERIFY,
        ran_at=_now(),
        run_ms=run_ms,
        violation_count=len(outcome.failed_streams),
        total_violations=total,
        watermark={"discovery_since": run_started_at.isoformat()},
    )
    metrics.invariant_check_last_run_timestamp_seconds.labels(check=CHECK_TRACE_VERIFY).set(
        _now().timestamp()
    )
    metrics.invariant_check_duration_seconds.labels(check=CHECK_TRACE_VERIFY).observe(run_ms / 1000.0)
    if newly_inserted:
        metrics.trace_verify_failures_total.inc(len(newly_inserted))
    return outcome


__all__ = [
    "InvariantsSummary",
    "configure",
    "read_summary",
    "run_due",
    "run_once",
    "run_trace_verify_once",
]
