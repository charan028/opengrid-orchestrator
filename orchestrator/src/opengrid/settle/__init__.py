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
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.reasons import R_AS_HOLD_SHORT
from opengrid.core.solar_share import SolarShare
from opengrid.market.capacity import regulated_capacity_payment
from opengrid.market.charging import regulated_charging_cost
from opengrid.market.config import DEFAULT_UTILITIES
from opengrid.settle.backend import SettleBackend
from opengrid.settle.baselines import METER_SOURCE_BY_SERVICE, compute_baseline_kwh
from opengrid.settle.billing import draft_invoice_lines, next_version
from opengrid.settle.metering import meter_interval as _meter_interval
from opengrid.settle.performance import compute_performance, is_need_basis_compliant
from opengrid.settle.profitability import compute_forgone_upside, compute_pnl
from opengrid.settle.services_extra import pjm_non_performance_charge
from opengrid.settle.tariffs import (
    TdspTariff,
    grid_charged_kwh_for_delivery,
    m1_delivery_charge,
    resolve_tariff,
    tdsp_for_zone,
)
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import run_retention_prune_job

_SECONDS_PER_HOUR = Decimal("3600")
#: M1 grid share: the trailing charging window ending at the interval (the same 24 h the charging-cost
#: proxy averages over, `pg_backend._FETCH_CHARGING_COST_PROXY_SQL`).
M1_GRID_SHARE_WINDOW = timedelta(hours=24)
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
_tdsp_tariffs: list[TdspTariff] | None = None
_zone_default_tdsp: dict[str, str] | None = None


def configure(
    backend: SettleBackend,
    trace_store: TraceStore,
    *,
    trace_pool: AsyncConnectionPool | None = None,
    tdsp_tariffs: list[TdspTariff] | None = None,
    zone_default_tdsp: dict[str, str] | None = None,
) -> None:
    """Wire up the real (or fake) I/O this process/test run uses. Called once by
    `opengrid.settle.main` at process startup, and by tests before exercising `settle()` or
    `run_trace_pruning_cycle()`.

    `trace_pool`, when given, lets `run_trace_pruning_cycle()` also run the canonical per-event-class
    retention job (`opengrid.trace.pg_backend.run_retention_prune_job`, health-owned, BUILD.md S4).
    Unit tests configure with no pool and get the pool-free seq-window prune only.

    `tdsp_tariffs`/`zone_default_tdsp` (09 D5's M1 delivery charge, `opengrid.settle.tariffs`): both
    `None` (the default) disables M1 entirely (`delivery_charge` settles as 0) rather than guessing at
    a tariff -- `opengrid.settle.main` loads `tdsp_tariffs.toml` once at startup and passes both in."""
    global _backend, _trace_store, _trace_pool, _tdsp_tariffs, _zone_default_tdsp
    _backend = backend
    _trace_store = trace_store
    _trace_pool = trace_pool
    _tdsp_tariffs = tdsp_tariffs
    _zone_default_tdsp = zone_default_tdsp


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
    committed_kwh = ctx.committed_kw * duration_hours
    penalty_theta = ctx.penalty.theta if ctx.penalty is not None else None
    performance = compute_performance(metering.delivered_kwh, baseline_kwh, penalty_theta)

    # --- D-18 need-basis settlement (00-invariants.md K13 "commitments are over a period"): a
    # MEASURED_FEEDBACK obligation's committed kWh is a reserved maximum, and delivery below it that
    # follows the customer's measured need is compliant, never a shortfall or a penalty. Only checked
    # when there IS a dip below committed (an exact/over delivery needs no need-basis reasoning) and
    # only queries the site meter for a need-basis obligation (never an extra query otherwise). -----
    need_basis_compliant = False
    if ctx.is_need_basis and metering.delivered_kwh < committed_kwh:
        measured_need_kwh = await backend.fetch_measured_need_kwh(obligation_id, interval_start, interval_end)
        need_basis_compliant = is_need_basis_compliant(
            metering.delivered_kwh, committed_kwh, measured_need_kwh
        )
        if need_basis_compliant:
            performance = replace(performance, passed_threshold=True)

    # --- ERCOT_AS is a capacity HOLD, not a delivery schedule (commit d43da06, og.as_deployment): an
    # award is granted 0 kW unless a deployment covers the interval, so "delivered/committed" is not a
    # meaningful compliance ratio -- its performance metric is AVAILABILITY (was the held capacity
    # available, with energy above reserve per the hold rule, and delivered when actually deployed),
    # not delivered-vs-committed kWh. Full hold-availability compliance (HOLD_COMPLIANCE, SOC >=
    # H_k*r/eta_d, 09 D6/G-32) is a guardian-side check out of scope here; settle has no independent
    # signal that the hold ever failed, so it defaults to available/compliant -- the same "innocent
    # until shown otherwise" stance as the need-basis fix just above. `og.performance.passed_threshold`
    # is the ONLY field opengrid.engine.lifecycle.close_target reads to decide FULFILLED vs SHORTFALL
    # at window-close (`NOT p.passed_threshold` in any interval), so this alone stops a held AS window
    # from ever closing as SHORTFALL -- no engine-side lifecycle change is needed.
    # REGULATED_CAPACITY is likewise a capacity hold (08 S3, 09 D1/D2): the utility pays for kW
    # committed, not kWh delivered, so it gets the identical availability treatment as ERCOT_AS.
    is_capacity_hold_service = ctx.service_type in ("ERCOT_AS", "REGULATED_CAPACITY")
    if is_capacity_hold_service:
        # og.performance.compliance_pct is NOT NULL (0001_init.sql) -- it must always be a number.
        # 1.0 (100%) while held and available (our default, absent an independent unavailability
        # signal); a genuinely deployed-but-under-delivered interval would need its own availability
        # figure (og.performance.availability_pct, unused today) once G-32's hold-availability check
        # exists. LIVE BUG FIX 2026-09-26: this used to write compliance_pct=NULL, violating the
        # column's NOT NULL constraint -- every AS settlement failed from the 13:00 deploy until this
        # fix (obligation 8e1cdcde's 12:45 interval and any other AS interval since).
        performance = replace(performance, compliance_pct=Decimal("1"), passed_threshold=True)

    # --- ERCOT_AS hold flag (review finding, 2026-09-26): while ALR-ENERGY-SHORTFALL-RISK was open for the
    # award, the held capacity may not have been fully available, so the interval is FLAGGED
    # (R-AS-HOLD-SHORT on the settlement trace, `as_hold_short` in its payload). The payment is unchanged:
    # there is no owner decision on an AS hold penalty yet. --------------------------------------------
    settlement_reasons: list[str] = []
    if ctx.service_type == "ERCOT_AS" and await backend.fetch_shortfall_risk_open(
        obligation_id, interval_start, interval_end
    ):
        settlement_reasons.append(R_AS_HOLD_SHORT)

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
            # og.performance.compliance_pct is NOT NULL (0001_init.sql): a service with no baseline
            # (HOME; compute_compliance_pct returns None) is vacuously compliant -- 1.0 (100%), the
            # same value passes_threshold(None, ...) already treats it as, never NULL.
            compliance_pct=(
                performance.compliance_pct if performance.compliance_pct is not None else Decimal("1")
            ),
            passed_threshold=performance.passed_threshold,
        )

    # --- profitability -----------------------------------------------------------------------
    # A need-basis-compliant dip, or an AS capacity hold, is never a shortfall (D-18 / AS hold fix):
    # zero it before penalty/pnl math, not just before billing, so compute_penalty never prices it.
    shortfall_kwh = (
        Decimal("0")
        if need_basis_compliant or is_capacity_hold_service
        else max(Decimal("0"), committed_kwh - metering.delivered_kwh)
    )
    # --- 08/09 two-market model: REGULATED (a utility territory) vs FREE (ERCOT competitive area).
    # M1 (the TDSP delivery charge) is a FREE-market-only concept (09 S1.6: a regulated utility's own
    # charging terms already include its delivery cost) -- a REGULATED contract instead prices
    # charging via `opengrid.market.charging.regulated_charging_cost` (TOU-aware) and, for
    # REGULATED_CAPACITY, its capacity payment via `opengrid.market.capacity.
    # regulated_capacity_payment`. Both are the MARKET-MODEL agent's canonical functions (BUILD.md S1
    # no-duplication) -- settle only resolves the `og.utility` row and passes the already-computed
    # dollar figures into `compute_pnl`/`draft_invoice_lines`.
    is_regulated_market = ctx.market == "REGULATED"
    charging_cost_per_kwh = ctx.charging_cost_per_kwh
    delivery_charge = Decimal("0")
    m1_solar_share: SolarShare | None = None  # D-28 share behind M1, recorded on the trace when used
    regulated_capacity_amount: Decimal | None = None
    if is_regulated_market and ctx.utility_id is not None:
        utility = await backend.fetch_utility(ctx.utility_id)
        if utility is None:
            # No live og.utility row yet (dev/seed/market_model_seed.sql not applied) -- fall back to
            # opengrid.market.config's own planning defaults rather than guess independently.
            utility = DEFAULT_UTILITIES.get(ctx.utility_id)
        if utility is not None:
            charging_cost_per_kwh = regulated_charging_cost(
                utility, ctx.zone or "", interval_start
            ).blended_usd_per_kwh
            if ctx.service_type == "REGULATED_CAPACITY":
                regulated_capacity_amount = regulated_capacity_payment(
                    committed_kw=ctx.committed_kw,
                    # "price None -> 0" handled here, not inside opengrid.market.capacity.
                    price_usd_per_kw=utility.capacity_price_usd_per_kw or Decimal("0"),
                    basis=utility.payment_basis,
                    hours=duration_hours,
                )
        else:
            _logger.warning(
                "settle: no og.utility row or planning default for utility_id, "
                "regulated charging cost/capacity payment settle as 0",
                extra={"obligation_id": str(obligation_id), "utility_id": ctx.utility_id},
            )
    elif _tdsp_tariffs is not None and _zone_default_tdsp is not None:
        # --- 09 D5's M1 TDSP delivery charge: kWh drawn from the grid to charge, ERCOT competitive
        # area only. Disabled (delivery_charge = 0) unless opengrid.settle.main loaded
        # tdsp_tariffs.toml at startup (configure()'s tdsp_tariffs/zone_default_tdsp) -- never a
        # guessed rate. A zone with no TDSP (regulated LZ_AEN/LZ_CPS, LCRA/co-op, unmapped) is never
        # queried and settles M1 at 0. Owner decision: the FULL charge on grid-drawn charging energy,
        # i.e. the energy this delivery had to be charged with, times the zone's grid share of charging
        # over the trailing window -- D-28's solar share (`opengrid.core.solar_share`, the same rule the
        # selector plans with): measured hub telemetry, else ERCOT's solar share, else 30%. -------------
        tdsp = tdsp_for_zone(_zone_default_tdsp, ctx.zone)
        tariff = resolve_tariff(_tdsp_tariffs, tdsp, interval_start.date())
        if tariff is not None and ctx.zone is not None and metering.delivered_kwh > 0:
            # Hour-aligned so every interval (and obligation) of the zone in that hour shares one scan.
            window_end = interval_end.replace(minute=0, second=0, microsecond=0)
            m1_solar_share = await backend.fetch_zone_solar_share(
                ctx.zone, window_end - M1_GRID_SHARE_WINDOW, window_end
            )
            grid_charged_kwh = grid_charged_kwh_for_delivery(
                metering.delivered_kwh,
                eta_c=ctx.eta_d,  # og.hub carries eta_c = eta_d (0.9487 each); the context holds eta_d only
                eta_d=ctx.eta_d,
                grid_share=m1_solar_share.grid_share,
            )
            delivery_charge = m1_delivery_charge(grid_charged_kwh, tariff)

    pnl = compute_pnl(
        service_type=ctx.service_type,
        delivered_kwh=metering.delivered_kwh,
        price_per_kwh=ctx.price_per_kwh,
        charging_cost_per_kwh=charging_cost_per_kwh,
        discharge_spp_per_kwh=ctx.wholesale_price_per_kwh,
        eta_d=ctx.eta_d,
        degradation_cost_per_kwh=ctx.degradation_cost_per_kwh,
        shortfall_kwh=shortfall_kwh,
        committed_kwh=committed_kwh,
        penalty=ctx.penalty,
        delivery_charge=delivery_charge,
        regulated_capacity_amount=regulated_capacity_amount,
    )

    # --- PJM_CAPACITY non-performance charge (opengrid.settle.services_extra, SERVICES agent):
    # billed only for a shortfall DURING a declared emergency performance hour, on top of (never in
    # place of) the ordinary CAPACITY_PAYMENT x performance_factor line. Folded into pnl.penalty/
    # net_value and the single LD_PENALTY invoice line (rather than a second LD_PENALTY draft): og.
    # invoice_line's insert-only versioning is keyed one row per (contract, obligation, period,
    # line_type), so two independently-versioned LD_PENALTY drafts for the same interval would race
    # each other's next_version() lookup -- one combined amount is the safe, documented choice.
    if ctx.service_type == "PJM_CAPACITY":
        pjm_rate = await backend.fetch_pjm_emergency_rate(obligation_id, interval_start, interval_end)
        if pjm_rate is not None:
            delivered_kw = metering.delivered_kwh / duration_hours if duration_hours != 0 else Decimal("0")
            pjm_extra_penalty = pjm_non_performance_charge(
                committed_kw=ctx.committed_kw,
                delivered_kw=delivered_kw,
                duration_hours=duration_hours,
                non_performance_rate_per_kwh=pjm_rate,
                is_emergency_performance_hour=True,
            )
            if pjm_extra_penalty > 0:
                pnl = replace(
                    pnl,
                    penalty=pnl.penalty + pjm_extra_penalty,
                    net_value=pnl.net_value - pjm_extra_penalty,
                )

    rule_baseline_kwh = await backend.fetch_rule_baseline_delivered_kwh(
        obligation_id, interval_start, interval_end
    )
    rule_baseline_value: Decimal | None = None
    if rule_baseline_kwh is not None:
        rule_baseline_pnl = compute_pnl(
            service_type=ctx.service_type,
            delivered_kwh=rule_baseline_kwh,
            price_per_kwh=ctx.price_per_kwh,
            charging_cost_per_kwh=charging_cost_per_kwh,
            discharge_spp_per_kwh=ctx.wholesale_price_per_kwh,
            eta_d=ctx.eta_d,
            degradation_cost_per_kwh=ctx.degradation_cost_per_kwh,
            shortfall_kwh=max(Decimal("0"), committed_kwh - rule_baseline_kwh),
            committed_kwh=committed_kwh,
            penalty=ctx.penalty,
            regulated_capacity_amount=regulated_capacity_amount,
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
            delivery_charge=pnl.delivery_charge,
            net_value=pnl.net_value,
            rule_baseline_value=rule_baseline_value,
            forgone_upside=forgone_upside,
            version=(existing_pnl.version + 1) if existing_pnl else 1,
            supersedes=existing_pnl.pnl_id if existing_pnl else None,
        )

    # --- invoice lines: insert-only, versioned per (contract, obligation, period, line_type) ----
    # A need-basis-compliant dip bills the full reserved-capacity payment (D-18: the customer pays for
    # the reservation, not for how much of it they happened to need this interval) -- never scaled
    # down by the true, lower compliance_pct.
    performance_factor = (
        Decimal("1")
        if need_basis_compliant
        else (performance.compliance_pct if performance.compliance_pct is not None else Decimal("1"))
    )
    drafts = draft_invoice_lines(
        service_type=ctx.service_type,
        delivered_kwh=metering.delivered_kwh,
        committed_kwh=committed_kwh,
        price_per_kwh=ctx.price_per_kwh,
        revenue=pnl.revenue,
        performance_factor=performance_factor,
        penalty_amount=pnl.penalty,
        regulated_capacity_amount=regulated_capacity_amount,
        duration_hours=duration_hours,
        discharge_spp_per_kwh=ctx.wholesale_price_per_kwh,
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
                "charging_cost_per_kwh": str(ctx.charging_cost_per_kwh),
                "charging_cost_flag": ctx.charging_cost_flag,
                "price_per_kwh": str(ctx.price_per_kwh),
                "price_flag": ctx.price_flag,
                "wholesale_price_per_kwh": str(ctx.wholesale_price_per_kwh),
                "wholesale_price_flag": ctx.wholesale_price_flag,
                "need_basis_compliant": need_basis_compliant,
                "delivery_charge": str(pnl.delivery_charge),
                "m1_solar_share": str(m1_solar_share.share) if m1_solar_share is not None else None,
                "m1_solar_share_source": m1_solar_share.source if m1_solar_share is not None else None,
                "as_hold_short": R_AS_HOLD_SHORT in settlement_reasons,
            },
            reason_codes=settlement_reasons or None,
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
