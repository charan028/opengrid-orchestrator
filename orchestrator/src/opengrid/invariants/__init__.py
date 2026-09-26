"""opengrid.invariants -- independent, read-only measurement of 00-invariants.md K1 (reserve breach),
K2 (double-sold kWh), K13 (commitment-lock violation), orphan reservation/commitment bookkeeping, and
K11 (scheduled trace-chain verification). Owner: invariants agent (BUILD.md S4).

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
                      0014_invariant_checks.sql).
  * `trace_verify`-- scheduled K11 verification, built on `opengrid.trace.store.TraceStore.verify`.
  * this module   -- wiring: `configure()`/`run_due()`, called as a small hook from
                      `opengrid.health.configure()`/`evaluate_once()` (see that package's own docstring
                      for why: this checker piggybacks on `og-settle`'s existing health cadence rather
                      than getting its own process).

Every check is incremental where it can be (a durable watermark in `og.invariant_check`) and bounded
where a true watermark doesn't apply (a rolling time window, or an already-small active-rows-only
subset) -- see `queries.py`'s module docstring. `run_due()` gates each check family on its OWN interval
via `opengrid.platform.process.Cadence`, independent of the ~5s cadence it is actually called on.
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
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Module-level wiring, matching opengrid.health's own no-argument public interface (BUILD.md's stub
# packages convention): set once by configure(), called from opengrid.health.configure().
_pool: AsyncConnectionPool | None = None
_trace_store: TraceStore | None = None
_cadence: Cadence | None = None
_trace_cadence: Cadence | None = None
_k2_lookback_s: float = _DEFAULT_K2_LOOKBACK_S
_k13_max_grant_gap_s: float = checks.DEFAULT_K13_MAX_GRANT_GAP_S


def configure(pool: AsyncConnectionPool, cfg: Config) -> None:
    """Wire the module-level singletons. Called once from `opengrid.health.configure()` (this package
    has no process entry point of its own -- it runs inside `og-settle` via `run_due()`)."""
    global _pool, _trace_store, _cadence, _trace_cadence, _k2_lookback_s, _k13_max_grant_gap_s
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
    """One pass of every DB-state check (K1, K2, K13, the two orphan checks). Each check persists its
    own result and updates its own Prometheus metric independently, so one check's failure (a bad row,
    a transient DB hiccup) never blocks the others -- mirrors `opengrid.health.evaluate_alerts`'
    per-rule independence."""
    pool = _require_pool()
    now = _now()
    outcomes: dict[str, CheckOutcome] = {}
    for check_name, runner in (
        (CHECK_K1_RESERVE_BREACH, _run_reserve_breach_check),
        (CHECK_K2_DOUBLE_SOLD, _run_double_sold_check),
        (CHECK_K13_LOCK_VIOLATION, _run_lock_violation_check),
        (CHECK_ORPHAN_RESERVATION, _run_orphan_reservation_check),
        (CHECK_ORPHAN_COMMITMENT, _run_orphan_commitment_check),
    ):
        try:
            outcomes[check_name] = await runner(pool, now)
        except Exception:
            logger.exception("invariants check failed", extra={"check": check_name})
    return outcomes


async def _persist_and_meter(
    pool: AsyncConnectionPool,
    check_name: str,
    outcome: CheckOutcome,
    *,
    prior_total: float,
    run_ms: int,
) -> None:
    # `total_magnitude`, not `count`: K2's total is a running sum of kWh (`Violation.magnitude`);
    # every other check's violations default to `magnitude=1.0`, so their total is numerically a count.
    total = prior_total + outcome.total_magnitude
    await queries.insert_violations(pool, check_name, list(outcome.violations))
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


async def _run_reserve_breach_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_K1_RESERVE_BREACH)
    since = _parse_ts(state.watermark.get("since")) or _EPOCH
    rows, new_ts = await queries.fetch_reserve_breach_candidates(pool, since=since)
    violations = checks.find_reserve_breaches(rows)
    outcome = CheckOutcome(
        CHECK_K1_RESERVE_BREACH, tuple(violations), {"since": (new_ts or since).isoformat()}
    )
    await _persist_and_meter(
        pool,
        CHECK_K1_RESERVE_BREACH,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    if violations:
        metrics.reserve_breaches_total.inc(len(violations))
    return outcome


async def _run_double_sold_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_K2_DOUBLE_SOLD)
    horizon_start = now - timedelta(seconds=_k2_lookback_s)
    rows = await queries.fetch_reservation_aggregates(pool, horizon_start=horizon_start)
    violations = checks.find_double_sold(rows)
    outcome = CheckOutcome(
        CHECK_K2_DOUBLE_SOLD, tuple(violations), {"horizon_start": horizon_start.isoformat()}
    )
    await _persist_and_meter(
        pool,
        CHECK_K2_DOUBLE_SOLD,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    if violations:
        metrics.double_sold_kwh_total.inc(outcome.total_magnitude)
    return outcome


async def _run_lock_violation_check(pool: AsyncConnectionPool, now: datetime) -> CheckOutcome:
    """K13: enumerate elapsed commitment intervals since the watermark, find each one's worst delivery
    point across ALL of the obligation's banks combined (`checks.find_dip`), and -- only for the ones
    that actually dipped -- look up whether the trace already covers it (`checks.
    _dip_is_covered`/`queries.fetch_earliest_covering_trace_at`). The per-obligation lookups only run for
    candidates that dipped, typically a small subset of the batch."""
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_K13_LOCK_VIOLATION)
    since = _parse_ts(state.watermark.get("since")) or _EPOCH
    candidates, new_end = await queries.fetch_lock_commitment_candidates(pool, since=since, now=now)

    dip_rows: list[tuple[str, datetime, datetime, float, float, datetime, datetime | None]] = []
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
        dip_kw, dip_at = dip
        earliest_covering_at = await queries.fetch_earliest_covering_trace_at(
            pool, obligation_id=obligation_id, window_start=interval_start, window_end=interval_end
        )
        dip_rows.append(
            (
                str(obligation_id),
                interval_start,
                interval_end,
                committed_kw,
                dip_kw,
                dip_at,
                earliest_covering_at,
            )
        )

    violations = checks.find_lock_violations(dip_rows)
    outcome = CheckOutcome(
        CHECK_K13_LOCK_VIOLATION, tuple(violations), {"since": (new_end or since).isoformat()}
    )
    await _persist_and_meter(
        pool,
        CHECK_K13_LOCK_VIOLATION,
        outcome,
        prior_total=state.total_violations,
        run_ms=int((time.perf_counter() - start) * 1000),
    )
    if violations:
        metrics.lock_violations_total.inc(len(violations))
    return outcome


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
    """K11: verify every trace stream's new segments since this check's own watermark, raising/clearing
    `ALR-TRACE-VERIFY-FAILED` through the health alert mechanism on failure/recovery."""
    pool = _require_pool()
    trace_store = _require_trace_store()
    start = time.perf_counter()
    state = await queries.get_check_state(pool, CHECK_TRACE_VERIFY)
    outcome = await _trace_verify.verify_new_segments(pool, trace_store, watermark=state.watermark)
    await _trace_verify.raise_or_clear_alert(pool, outcome)

    failed_count = len(outcome.failed_streams)
    if failed_count:
        await queries.insert_violations(
            pool,
            CHECK_TRACE_VERIFY,
            [Violation(scope={"stream_id": stream_id}) for stream_id in outcome.failed_streams],
        )
    run_ms = int((time.perf_counter() - start) * 1000)
    total = state.total_violations + failed_count
    await queries.upsert_check_state(
        pool,
        CHECK_TRACE_VERIFY,
        ran_at=_now(),
        run_ms=run_ms,
        violation_count=failed_count,
        total_violations=total,
        watermark=outcome.new_watermark,
    )
    metrics.invariant_check_last_run_timestamp_seconds.labels(check=CHECK_TRACE_VERIFY).set(
        _now().timestamp()
    )
    metrics.invariant_check_duration_seconds.labels(check=CHECK_TRACE_VERIFY).observe(run_ms / 1000.0)
    if failed_count:
        metrics.trace_verify_failures_total.inc(failed_count)
    return outcome


__all__ = [
    "InvariantsSummary",
    "configure",
    "read_summary",
    "run_due",
    "run_once",
    "run_trace_verify_once",
]
