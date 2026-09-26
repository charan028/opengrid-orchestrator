"""opengrid.settle -- process og-settle (02a S7): M&V, billing, profitability, trace pruning cadence.
Owner: settle agent (BUILD.md S4).

Sole owner of both M&V and profitability math (02b S12) -- the UI only reads `pnl`/`invoice_line`, it
never computes.

Public interface is fixed by `orchestrator/INTERFACES.md`: `settle(obligation_id, interval_start,
interval_end)` and `run_trace_pruning_cycle()` take no dependency-injection parameters, so the
process entry point (`opengrid.settle.main`) wires up the real `SettleBackend`/`TraceStore` once at
startup via `configure()`; tests call `configure()` with fakes. All the actual math is pure and lives
in `opengrid.settle.{metering,baselines,performance,profitability,billing}` -- this module only
orchestrates: fetch context -> compute -> persist idempotently -> trace.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.settle.backend import SettleBackend
from opengrid.settle.baselines import METER_SOURCE_BY_SERVICE, compute_baseline_kwh
from opengrid.settle.billing import draft_invoice_lines, next_version
from opengrid.settle.metering import meter_interval as _meter_interval
from opengrid.settle.performance import compute_performance
from opengrid.settle.profitability import compute_forgone_upside, compute_pnl
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import run_retention_prune_job

_SECONDS_PER_HOUR = Decimal("3600")
_SETTLE_STREAM_ID = "settle"
_DEFAULT_MAX_CONCURRENCY = 20  # BUILD.md S2: many customers/obligations settled concurrently

# Settle idempotency fix: `og.meter_interval.delivered_kwh` is `numeric(14,6)` and `og.pnl.net_value`
# is `numeric(18,6)` -- Postgres rounds to that precision on write, so a value fetched back is never
# bit-for-bit equal to a freshly recomputed, un-rounded `Decimal` (e.g. any calculation that divides by
# `eta_d = 0.9487` yields a long repeating decimal). A strict `!=` comparison against the DB-rounded
# value therefore looked "changed" on every re-run with IDENTICAL inputs, defeating idempotency and
# inserting a duplicate row each time. `opengrid.settle.billing.next_version` already gets this right
# (`_AMOUNT_EPSILON`); the same column-precision-tolerant comparison is used here for the same reason.
_KWH_EPSILON = Decimal("0.000001")  # numeric(14,6) column precision
_MONEY_EPSILON = Decimal("0.000001")  # numeric(18,6) column precision


def _decimal_changed(existing: Decimal, computed: Decimal, epsilon: Decimal) -> bool:
    return abs(existing - computed) >= epsilon


_logger = logging.getLogger(__name__)

_backend: SettleBackend | None = None
_trace_store: TraceStore | None = None
_trace_pool: AsyncConnectionPool | None = None


def configure(
    backend: SettleBackend, trace_store: TraceStore, *, trace_pool: AsyncConnectionPool | None = None
) -> None:
    """Wire up the real (or fake) I/O this process/test run uses. Called once by
    `opengrid.settle.main` at process startup, and by tests before exercising `settle()` or
    `run_trace_pruning_cycle()`.

    `trace_pool`, when given, lets `run_trace_pruning_cycle()` also run the canonical per-event-class
    retention job (`opengrid.trace.pg_backend.run_retention_prune_job`, health-owned, BUILD.md S4).
    Unit tests configure with no pool and get the pool-free seq-window prune only."""
    global _backend, _trace_store, _trace_pool
    _backend = backend
    _trace_store = trace_store
    _trace_pool = trace_pool


def _require_backend() -> SettleBackend:
    if _backend is None:
        raise RuntimeError("opengrid.settle.configure() must be called before settle()")
    return _backend


def _require_trace_store() -> TraceStore:
    if _trace_store is None:
        raise RuntimeError("opengrid.settle.configure() must be called before run_trace_pruning_cycle()")
    return _trace_store


def _duration_hours(interval_start: datetime, interval_end: datetime) -> Decimal:
    seconds = Decimal(str((interval_end - interval_start).total_seconds()))
    return seconds / _SECONDS_PER_HOUR


async def settle(obligation_id: UUID, interval_start: datetime, interval_end: datetime) -> None:
    """Post `meter_interval`, `performance`, `invoice_line` and `pnl` rows for one obligation-interval
    (02a S7.1-S7.4), using the service-specific M&V baseline and the shared penalty/objective formulas
    (never re-derived -- see 02a S7.4's profitability formula).

    Idempotent: calling this twice with identical telemetry for the same
    `(obligation_id, interval_start)` inserts nothing the second time (billing.next_version /
    the `pnl` net-value comparison below both short-circuit); calling it again after telemetry
    changed (a correction) inserts new, insert-only versioned rows referencing the originals.
    """
    backend = _require_backend()
    trace_store = _require_trace_store()

    ctx = await backend.fetch_context(obligation_id, interval_start)
    duration_hours = _duration_hours(interval_start, interval_end)
    interval_minutes = int(duration_hours * Decimal("60"))

    samples = await backend.fetch_power_samples(obligation_id, interval_start, interval_end)
    source = METER_SOURCE_BY_SERVICE[ctx.service_type]
    metering = _meter_interval(samples, interval_minutes=interval_minutes, source=source)

    baseline_kwh = compute_baseline_kwh(ctx.service_type, ctx.committed_kw, duration_hours)
    penalty_theta = ctx.penalty.theta if ctx.penalty is not None else None
    performance = compute_performance(metering.delivered_kwh, baseline_kwh, penalty_theta)

    # --- meter_interval: insert-only, versioned by delivered_kwh changing -----------------------
    existing_meter = await backend.fetch_active_meter_interval(obligation_id, interval_start)
    meter_changed = existing_meter is None or _decimal_changed(
        existing_meter.delivered_kwh, metering.delivered_kwh, _KWH_EPSILON
    )
    if meter_changed:
        await backend.insert_meter_interval(
            obligation_id=obligation_id,
            interval_start=interval_start,
            interval_end=interval_end,
            delivered_kwh=metering.delivered_kwh,
            baseline_kwh=baseline_kwh,
            source=metering.source,
            quality_flag=metering.quality_flag,
            version=(existing_meter.version + 1) if existing_meter else 1,
            supersedes=existing_meter.meter_interval_id if existing_meter else None,
        )
        await backend.insert_performance(
            obligation_id=obligation_id,
            interval_start=interval_start,
            interval_end=interval_end,
            compliance_pct=performance.compliance_pct,
            passed_threshold=performance.passed_threshold,
        )

    # --- profitability -----------------------------------------------------------------------
    committed_kwh = ctx.committed_kw * duration_hours
    shortfall_kwh = max(Decimal("0"), committed_kwh - metering.delivered_kwh)
    pnl = compute_pnl(
        delivered_kwh=metering.delivered_kwh,
        price_per_kwh=ctx.price_per_kwh,
        wholesale_price_per_kwh=ctx.wholesale_price_per_kwh,
        eta_d=ctx.eta_d,
        degradation_cost_per_kwh=ctx.degradation_cost_per_kwh,
        shortfall_kwh=shortfall_kwh,
        committed_kwh=committed_kwh,
        penalty=ctx.penalty,
    )

    rule_baseline_kwh = await backend.fetch_rule_baseline_delivered_kwh(
        obligation_id, interval_start, interval_end
    )
    rule_baseline_value: Decimal | None = None
    if rule_baseline_kwh is not None:
        rule_baseline_pnl = compute_pnl(
            delivered_kwh=rule_baseline_kwh,
            price_per_kwh=ctx.price_per_kwh,
            wholesale_price_per_kwh=ctx.wholesale_price_per_kwh,
            eta_d=ctx.eta_d,
            degradation_cost_per_kwh=ctx.degradation_cost_per_kwh,
            shortfall_kwh=max(Decimal("0"), committed_kwh - rule_baseline_kwh),
            committed_kwh=committed_kwh,
            penalty=ctx.penalty,
        )
        rule_baseline_value = rule_baseline_pnl.net_value

    best_competing_value = await backend.fetch_best_competing_value_per_kwh(
        obligation_id, interval_start, interval_end
    )
    forgone_upside = compute_forgone_upside(
        committed_kw=ctx.committed_kw,
        duration_hours=duration_hours,
        obligation_value_per_kwh=ctx.price_per_kwh,
        best_competing_value_per_kwh=best_competing_value,
    )

    # --- pnl: insert-only, versioned exactly like meter_interval (02a S1's insert-only-table rule;
    # a DB partial unique index on (obligation_id, interval_start) WHERE superseded_by IS NULL is the
    # actual guard against a concurrent double-insert, not just this read-then-compare check) --------
    existing_pnl = await backend.fetch_active_pnl(obligation_id, interval_start)
    pnl_changed = existing_pnl is None or _decimal_changed(
        existing_pnl.net_value, pnl.net_value, _MONEY_EPSILON
    )
    if pnl_changed:
        await backend.insert_pnl(
            obligation_id=obligation_id,
            interval_start=interval_start,
            interval_end=interval_end,
            revenue=pnl.revenue,
            energy_cost=pnl.energy_cost,
            degradation_cost=pnl.degradation_cost,
            penalty=pnl.penalty,
            net_value=pnl.net_value,
            rule_baseline_value=rule_baseline_value,
            forgone_upside=forgone_upside,
            version=(existing_pnl.version + 1) if existing_pnl else 1,
            supersedes=existing_pnl.pnl_id if existing_pnl else None,
        )

    # --- invoice lines: insert-only, versioned per (contract, obligation, period, line_type) ----
    performance_factor = (
        performance.compliance_pct if performance.compliance_pct is not None else Decimal("1")
    )
    drafts = draft_invoice_lines(
        service_type=ctx.service_type,
        delivered_kwh=metering.delivered_kwh,
        committed_kwh=committed_kwh,
        price_per_kwh=ctx.price_per_kwh,
        revenue=pnl.revenue,
        performance_factor=performance_factor,
        penalty_amount=pnl.penalty,
    )
    any_line_posted = False
    for draft in drafts:
        existing_line = await backend.fetch_active_invoice_line(
            ctx.contract_id, obligation_id, ctx.period_start, ctx.period_end, draft.line_type
        )
        decision = next_version(draft, existing_line, quality_flag=metering.quality_flag)
        if decision is None:
            continue
        line_draft, version, supersedes, status = decision
        await backend.insert_invoice_line(
            contract_id=ctx.contract_id,
            obligation_id=obligation_id,
            period_start=ctx.period_start,
            period_end=ctx.period_end,
            draft=line_draft,
            status=status,  # type: ignore[arg-type]
            version=version,
            supersedes=supersedes,
        )
        any_line_posted = True

    if meter_changed or pnl_changed or any_line_posted:
        await trace_store.append(
            _SETTLE_STREAM_ID,
            "SETTLEMENT",
            "SETTLEMENT",
            {
                "obligation_id": str(obligation_id),
                "interval_start": interval_start.isoformat(),
                "interval_end": interval_end.isoformat(),
                "delivered_kwh": str(metering.delivered_kwh),
                "net_value": str(pnl.net_value),
                "quality_flag": metering.quality_flag,
                "wholesale_price_per_kwh": str(ctx.wholesale_price_per_kwh),
                "wholesale_price_flag": ctx.wholesale_price_flag,
            },
        )


async def run_settle_cycle(*, max_concurrency: int = _DEFAULT_MAX_CONCURRENCY) -> int:
    """Settle every obligation-interval the backend reports as pending (`SettleBackend.
    fetch_pending_intervals`), bounded to `max_concurrency` concurrent obligations at a time
    (BUILD.md S2: "several customers and obligations concurrently"). One obligation's failure is
    logged and does not stop the others (K7: degrade, don't trip). Returns the count settled
    successfully. Called by `opengrid.settle.main`'s tick; not by `settle()` itself, so unit tests can
    call `settle()` directly for a single obligation-interval without a full backend."""
    backend = _require_backend()
    pending = await backend.fetch_pending_intervals()
    semaphore = asyncio.Semaphore(max_concurrency)
    settled_count = 0

    async def _settle_one(obligation_id: UUID, interval_start: datetime, interval_end: datetime) -> bool:
        async with semaphore:
            try:
                await settle(obligation_id, interval_start, interval_end)
                return True
            except Exception:
                _logger.exception(
                    "settlement failed for one obligation-interval",
                    extra={"obligation_id": str(obligation_id), "interval_start": interval_start.isoformat()},
                )
                return False

    results = await asyncio.gather(*(_settle_one(o, s, e) for o, s, e in pending))
    settled_count = sum(1 for ok in results if ok)
    return settled_count


R_SETTLED = "R-SETTLED"


async def close_settled_obligations() -> int:
    """02a S2.1 `FULFILLED/SHORTFALL -> SETTLED`, once every interval of the obligation's window is
    metered and its P&L (and, except HOME, invoice) rows are posted (`fetch_settleable_obligations`).
    Idempotent: a settled obligation no longer matches the query, and a lost optimistic-lock race is
    skipped until the next cycle. Traced by `opengrid.contracts` (SETTLEMENT, `R-SETTLED`). Returns the
    number settled."""
    from opengrid import contracts

    settled = 0
    for obligation_id in await _require_backend().fetch_settleable_obligations():
        try:
            await contracts.transition_obligation(obligation_id, "SETTLED", reason_code=R_SETTLED)
        except Exception:
            _logger.exception("could not settle obligation", extra={"obligation_id": str(obligation_id)})
            continue
        settled += 1
    return settled


async def run_trace_pruning_cycle() -> dict[str, int]:
    """Calls `opengrid.trace.TraceStore.checkpoint()` then `.prune()` on settle's cadence (02a S8.3,
    02b S1.2: "trace pruning ... runs on og-settle's cadence"), then, when a trace pool is configured,
    the canonical per-event-class retention job (`opengrid.trace.pg_backend.run_retention_prune_job`,
    health-owned) so `og.retention_policy`'s per-class days are honored, not just the seq-window
    backstop `TraceStore.prune()` provides."""
    trace_store = _require_trace_store()
    await trace_store.checkpoint()
    deleted = await trace_store.prune()
    if _trace_pool is not None:
        deleted_by_class = await run_retention_prune_job(_trace_pool)
        deleted.update(deleted_by_class)
    return deleted
