"""og-settle's delivery job (D-38): every `[delivery].interval_s`, verify each running or just-ended call
from telemetry, persist its `og.delivery_record`, raise/clear the live delivery alerts, flag a measured
shortfall AT_RISK, and trace the final record.

Observation only (K7): nothing here changes dispatch; a measured shortfall never stops a firm obligation
(D-17), it flags AT_RISK through `opengrid.contracts.set_obligation_at_risk` (the one writer) and alerts.
Mid-window SHORTFALL escalation stays the engine's (`opengrid.engine.escalation`), not duplicated here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.delivery import (
    DeliveryMetrics,
    DeliveryPolicy,
    MeterCheck,
    MeterStatus,
    corroborate_meter,
    verify_delivery,
)
from opengrid.core.models.platform import Alert
from opengrid.core.services import (
    ECRS_PRODUCT,
    NSPIN_PRODUCT,
    REGDN_PRODUCT,
    REGUP_PRODUCT,
    RRS_PRODUCT,
    TOLLING_PRODUCT,
    canonical_product,
)
from opengrid.delivery import store
from opengrid.delivery.models import CallSpec, DeliveryRecord, SeriesPoint
from opengrid.delivery.series import assemble_buckets, bucket_starts, read_baselines, read_slice
from opengrid.health.delivery_rules import (
    ALR_DELIVERY_METER_MISMATCH,
    DELIVERY_ALERT_RULES,
    SHORT_RULES,
    DeliveryAlertFacts,
    evaluate_delivery_alerts,
    evaluate_delivery_meter_mismatch_alert,
)
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

DELIVERY_TRACE_STREAM = "delivery"
DELIVERY_EVENT_CLASS = "DELIVERY_VERIFICATION"
#: AT_RISK reason while a call's measured delivery is short (live SHORTFALL / NONE alert open).
R_DELIVERY_MEASURED_SHORTFALL = "R-DELIVERY-MEASURED-SHORTFALL"
R_DELIVERY_RECOVERED = "R-DELIVERY-RECOVERED"
#: How far back a restarted og-settle looks for AT_RISK flags it set (calls are at most 4 h long).
AT_RISK_RECONCILE_HOURS = 24

#: Default ramp time per canonical product (`core.services`); `[delivery.ramp_time_s]` overrides.
DEFAULT_RAMP_TIME_S: dict[str, float] = {
    ECRS_PRODUCT: 600.0,
    RRS_PRODUCT: 600.0,
    REGUP_PRODUCT: 300.0,
    REGDN_PRODUCT: 300.0,
    NSPIN_PRODUCT: 1800.0,
    TOLLING_PRODUCT: 600.0,
    "MANUAL": 120.0,
}


@dataclass(frozen=True, slots=True)
class DeliverySettings:
    """`[delivery]` config: cadence, bucket, telemetry lag, windows and the tolerance policy."""

    enabled: bool = True
    interval_s: float = 15.0
    bucket_s: float = 30.0
    telemetry_lag_s: float = 30.0
    lookback_s: float = 3600.0
    baseline_s: float = 120.0
    max_slice_s: float = 900.0
    default_ramp_time_s: float = 600.0
    ramp_time_s: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_RAMP_TIME_S))
    meter_bank_ids: tuple[str, ...] = ()
    #: Retention (migration 0051): the per-bucket series of a final record is emptied after this many days.
    series_keep_days: float = 60.0
    series_prune_batch: int = 500
    policy: DeliveryPolicy = field(default_factory=DeliveryPolicy)

    @classmethod
    def from_config(cls, cfg: Config) -> DeliverySettings:
        default = cls()
        ramp = {
            str(canonical_product(str(k))): float(v)
            for k, v in (cfg.get("delivery.ramp_time_s", {}) or {}).items()
        }
        base_policy = DeliveryPolicy()
        policy = DeliveryPolicy(
            **{
                name: float(cfg.get(f"delivery.{name}", getattr(base_policy, name)))
                for name in DeliveryPolicy.__dataclass_fields__
                if name != "ramp_time_s"
            }
        )
        return cls(
            enabled=bool(cfg.get("delivery.enabled", default.enabled)),
            interval_s=float(cfg.get("delivery.interval_s", default.interval_s)),
            bucket_s=float(cfg.get("delivery.bucket_s", default.bucket_s)),
            telemetry_lag_s=float(cfg.get("delivery.telemetry_lag_s", default.telemetry_lag_s)),
            lookback_s=float(cfg.get("delivery.lookback_s", default.lookback_s)),
            baseline_s=float(cfg.get("delivery.baseline_s", default.baseline_s)),
            max_slice_s=float(cfg.get("delivery.max_slice_s", default.max_slice_s)),
            default_ramp_time_s=float(cfg.get("delivery.default_ramp_time_s", default.default_ramp_time_s)),
            ramp_time_s={**DEFAULT_RAMP_TIME_S, **ramp},
            meter_bank_ids=tuple(str(b) for b in cfg.get("delivery.meter_bank_ids", []) or []),
            series_keep_days=float(cfg.get("delivery.series_keep_days", default.series_keep_days)),
            series_prune_batch=int(cfg.get("delivery.series_prune_batch", default.series_prune_batch)),
            policy=policy,
        )

    def ramp_for(self, product: str | None) -> float:
        """The product's ramp time, keyed by its canonical name (`core.services.canonical_product`: NONSPIN
        and NON_SPIN are NSPIN); unlisted products get `default_ramp_time_s`."""
        return float(self.ramp_time_s.get(canonical_product(product) or "", self.default_ramp_time_s))


def build_record(
    spec: CallSpec,
    series: list[SeriesPoint],
    *,
    metrics: DeliveryMetrics,
    meter: MeterCheck,
    ramp_time_s: float,
    baselines: tuple[float | None, float | None],
    evaluated_to: datetime | None,
    final: bool,
    trace_id: UUID | None = None,
) -> DeliveryRecord:
    """Pure: the persisted record from the call, its series and the metrics."""
    last = series[-1] if series else None
    return DeliveryRecord(
        call_id=spec.call_id,
        call_kind=spec.call_kind,
        deployment_id=spec.deployment_id,
        dispatch_call_id=spec.dispatch_call_id,
        obligation_id=spec.obligation_id,
        contract_id=spec.contract_id,
        customer_id=spec.customer_id,
        utility_id=spec.utility_id,
        service_type=spec.service_type,
        product=spec.product,
        bank_ids=list(spec.bank_ids),
        meter_bank_ids=list(spec.meter_bank_ids),
        hub_ids=list(spec.hub_ids),
        window_start=spec.window_start,
        window_end=spec.window_end,
        stopped_at=spec.stopped_at,
        committed_kw=spec.committed_kw,
        commanded_kw_avg=metrics.commanded_kw_avg if series else None,
        delivered_kw_avg=metrics.delivered_kw_avg,
        delivered_kw_last=last.d if last else None,
        commanded_kw_last=last.m if last else None,
        ramp_time_s=ramp_time_s,
        time_to_target_s=metrics.time_to_target_s,
        sustained_pct=metrics.sustained_pct,
        lowest_kw=metrics.lowest_kw,
        lowest_at=metrics.lowest_at,
        lowest_run_s=metrics.lowest_run_s,
        discharged_kwh=metrics.discharged_kwh,
        committed_kwh=metrics.committed_kwh,
        stale_frac=metrics.stale_frac,
        result=metrics.result.value,
        reasons=[r.value for r in metrics.reasons],
        meter_status=meter.status.value,
        meter_mismatch_frac=meter.mismatch_frac,
        meter_baseline_kw=baselines[0],
        battery_baseline_kw=baselines[1],
        series=series,
        evaluated_to=evaluated_to,
        final=final,
        trace_id=trace_id,
    )


SetAtRisk = Callable[..., Awaitable[object]]


class DeliveryJob:
    """Runs one verification pass over the open calls (`run_once`)."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        trace: TraceStore,
        settings: DeliverySettings,
        *,
        set_at_risk: SetAtRisk | None = None,
    ) -> None:
        self._pool = pool
        self._trace = trace
        self._settings = settings
        self._set_at_risk = set_at_risk
        self._flagged: set[str] = set()
        self._reconciled = False

    async def run_once(self, now: datetime | None = None) -> int:
        """Verify every open call; returns how many records were written. One call's failure is logged
        and never stops the others (K7)."""
        now = now or datetime.now(UTC)
        async with self._pool.connection() as conn:
            specs = await store.open_calls(conn, now=now, lookback_s=self._settings.lookback_s)
            existing = await store.fetch_open_records(conn, [s.call_id for s in specs])
        open_alerts = [a for a in await fetch_open_alerts(self._pool) if a.rule in DELIVERY_ALERT_RULES]
        open_ids = {s.call_id for s in specs}
        if not self._reconciled:
            await self._reconcile_at_risk(open_alerts, open_ids, now)
        await self._clear_ended(open_alerts, open_ids, now)
        await self._prune(now)
        written = 0
        for spec in specs:
            try:
                record = await self._verify(spec, existing.get(spec.call_id), now)
                await self._alerts(record, open_alerts, now)
                written += 1
            except Exception:
                logger.exception("delivery verification failed for a call", extra={"call_id": spec.call_id})
        return written

    async def _verify(self, spec: CallSpec, previous: DeliveryRecord | None, now: datetime) -> DeliveryRecord:
        s = self._settings
        async with self._pool.connection() as conn:
            spec = await store.resolve_banks(conn, spec, extra_meter_banks=s.meter_bank_ids)
            if previous is not None and previous.meter_bank_ids == list(spec.meter_bank_ids):
                baselines = (previous.meter_baseline_kw, previous.battery_baseline_kw)
            else:
                baselines = await read_baselines(conn, spec, s.baseline_s)
            series = list(previous.series) if previous is not None else []
            a = previous.evaluated_to if previous and previous.evaluated_to else spec.window_start
            horizon = min(now - timedelta(seconds=s.telemetry_lag_s), spec.effective_end)
            b = min(horizon, a + timedelta(seconds=s.max_slice_s))
            starts = bucket_starts(spec.window_start, a, b, s.bucket_s)
            if starts:
                data = await read_slice(
                    conn, spec, starts[0], starts[-1] + timedelta(seconds=s.bucket_s), s.bucket_s
                )
                buckets = assemble_buckets(spec, data, starts, s.bucket_s, baselines)
                series.extend(SeriesPoint.of(bk) for bk in buckets)
            evaluated_to = series[-1].t + timedelta(seconds=series[-1].s) if series else a
            final = now - timedelta(seconds=s.telemetry_lag_s) >= spec.effective_end and not bucket_starts(
                spec.window_start, evaluated_to, spec.effective_end, s.bucket_s
            )
            ramp = s.ramp_for(spec.product)
            policy = _with_ramp(s.policy, ramp)
            buckets_all = [p.bucket() for p in series]
            metrics = verify_delivery(
                buckets_all,
                policy,
                call_start=spec.window_start,
                final=final,
                stopped=spec.stopped_at is not None,
            )
            meter = corroborate_meter(buckets_all, policy)
            trace_id = previous.trace_id if previous else None
            record = build_record(
                spec,
                series,
                metrics=metrics,
                meter=meter,
                ramp_time_s=ramp,
                baselines=baselines,
                evaluated_to=evaluated_to,
                final=final,
                trace_id=trace_id,
            )
            if final and trace_id is None:
                ref = await self._trace.append(
                    DELIVERY_TRACE_STREAM,
                    "DELIVERY_RECORD",
                    DELIVERY_EVENT_CLASS,
                    trace_payload(record),
                    [f"R-DELIVERY-{record.result}", *record.reasons, f"R-METER-{record.meter_status}"],
                )
                record = record.model_copy(update={"trace_id": ref.trace_id})
            await store.upsert_record(conn, record)
            await conn.commit()
        return record

    async def _alerts(self, record: DeliveryRecord, open_alerts: list[Alert], now: datetime) -> None:
        facts = alert_facts(record, self._settings.policy)
        wanted = {f.rule: f for f in evaluate_delivery_alerts(facts)}
        if record.final and record.meter_status == MeterStatus.UNCORROBORATED.value:
            finding = evaluate_delivery_meter_mismatch_alert(
                facts,
                mismatch_frac=record.meter_mismatch_frac,
                window_end=record.window_end,
                meter_bank_ids=record.meter_bank_ids,
            )
            wanted[finding.rule] = finding
        mine = [a for a in open_alerts if (a.detail or {}).get("call_id") == record.call_id]
        opened = {a.rule for a in mine}
        for rule, finding in wanted.items():
            if rule not in opened:
                await raise_alert(self._pool, finding, opened_at=now)
                await self._trace.append(
                    DELIVERY_TRACE_STREAM, "ALERT", "DELIVERY_ALERT", {**finding.detail, "rule": rule}, [rule]
                )
        for alert in mine:
            if (
                alert.rule not in wanted
                and alert.rule != ALR_DELIVERY_METER_MISMATCH
                and alert.id is not None
            ):
                await clear_alert(self._pool, alert.id, cleared_at=now)
        await self._flag_at_risk(record, short=bool(set(wanted) & SHORT_RULES))

    async def _prune(self, now: datetime) -> None:
        """Retention: empty old final records' per-bucket series (bounded per pass); never blocks a pass."""
        try:
            async with self._pool.connection() as conn:
                pruned = await store.prune_series(
                    conn,
                    now=now,
                    keep_days=self._settings.series_keep_days,
                    batch=self._settings.series_prune_batch,
                )
                await conn.commit()
        except Exception:
            logger.exception("delivery series retention failed this pass")
            return
        if pruned:
            logger.info("delivery series pruned", extra={"records": pruned})

    async def _reconcile_at_risk(self, open_alerts: list[Alert], open_ids: set[str], now: datetime) -> None:
        """Startup: the AT_RISK flags this job set live only in memory. Adopt those whose call is still open
        with a SHORTFALL/NONE alert (so they clear on recovery); clear the rest (call recovered or ended)."""
        short_calls = {str((a.detail or {}).get("call_id")) for a in open_alerts if a.rule in SHORT_RULES}
        async with self._pool.connection() as conn:
            flags = await store.delivery_at_risk_flags(
                conn, since=now - timedelta(hours=AT_RISK_RECONCILE_HOURS)
            )
        for obligation_id, call_id in flags:
            if call_id is not None and call_id in open_ids and call_id in short_calls:
                self._flagged.add(call_id)
                continue
            if self._set_at_risk is None:
                continue
            try:
                await self._set_at_risk(
                    obligation_id,
                    False,
                    reason_code=R_DELIVERY_RECOVERED,
                    payload={"cause": "measured_delivery", "call_id": call_id, "reconciled": True},
                )
            except Exception:
                logger.exception(
                    "could not clear a stale measured-delivery AT_RISK", extra={"call_id": call_id}
                )
        self._reconciled = True

    async def _clear_ended(self, open_alerts: list[Alert], open_ids: set[str], now: datetime) -> None:
        """Live alerts of calls that are no longer open (ended while og-settle was down) are cleared; a meter
        mismatch clears once the same meter agrees with battery telemetry on a later call."""
        mismatches: list[Alert] = []
        for alert in open_alerts:
            call_id = str((alert.detail or {}).get("call_id"))
            if alert.rule == ALR_DELIVERY_METER_MISMATCH:
                mismatches.append(alert)
            elif call_id not in open_ids and alert.id is not None:
                await clear_alert(self._pool, alert.id, cleared_at=now)
        for alert in mismatches:
            if await self._meter_agrees_again(alert) and alert.id is not None:
                await clear_alert(self._pool, alert.id, cleared_at=now)
                await self._trace.append(
                    DELIVERY_TRACE_STREAM,
                    "ALERT",
                    "DELIVERY_ALERT_CLEARED",
                    {
                        "rule": alert.rule,
                        "call_id": (alert.detail or {}).get("call_id"),
                        "cause": "meter_agrees",
                    },
                    [alert.rule],
                )

    async def _meter_agrees_again(self, alert: Alert) -> bool:
        detail = alert.detail or {}
        banks = [str(b) for b in detail.get("meter_bank_ids") or []]
        window_end = detail.get("window_end")
        if not banks or not window_end:
            return False
        since = datetime.fromisoformat(str(window_end))
        async with self._pool.connection() as conn:
            latest = await store.latest_meter_status(conn, banks, since=since)
        return bool(latest) and all(
            latest.get(bank, ("", since))[0] == MeterStatus.CORROBORATED.value for bank in banks
        )

    async def _flag_at_risk(self, record: DeliveryRecord, *, short: bool) -> None:
        """AT_RISK while a live SHORTFALL/NONE alert stands; cleared once (by this job) when it recovers or
        the call ends. Only a flag this job set is cleared (the engine's own AT_RISK causes are left)."""
        if self._set_at_risk is None or record.obligation_id is None:
            return
        at_risk = short and not record.final
        if at_risk == (record.call_id in self._flagged):
            return
        try:
            await self._set_at_risk(
                record.obligation_id,
                at_risk,
                reason_code=R_DELIVERY_MEASURED_SHORTFALL if at_risk else R_DELIVERY_RECOVERED,
                payload={"cause": "measured_delivery", "call_id": record.call_id},
            )
        except Exception:
            logger.exception("could not flag measured delivery AT_RISK", extra={"call_id": record.call_id})
            return
        if at_risk:
            self._flagged.add(record.call_id)
        else:
            self._flagged.discard(record.call_id)


def alert_facts(record: DeliveryRecord, base_policy: DeliveryPolicy) -> DeliveryAlertFacts:
    """Pure: the live facts the alert rules need, from a record's series (provisional metrics)."""
    policy = _with_ramp(base_policy, record.ramp_time_s)
    buckets = [p.bucket() for p in record.series]
    metrics = verify_delivery(buckets, policy, call_start=record.window_start, final=False)
    elapsed = (record.evaluated_to - record.window_start).total_seconds() if record.evaluated_to else 0.0
    return DeliveryAlertFacts(
        call_id=record.call_id,
        call_kind=record.call_kind.value,
        obligation_id=record.obligation_id,
        utility_id=record.utility_id,
        committed_kw=record.committed_kw,
        delivered_kw=record.delivered_kw_last,
        active=not record.final,
        elapsed_s=elapsed,
        ramp_time_s=record.ramp_time_s,
        reached_target=metrics.reached_target,
        current_below_s=metrics.current_below_s,
        shortfall_alert_s=policy.shortfall_alert_s,
        commanded_without_delivery_s=metrics.commanded_without_delivery_s,
        none_alert_s=policy.none_alert_s,
    )


def _with_ramp(policy: DeliveryPolicy, ramp_time_s: float) -> DeliveryPolicy:
    return replace(policy, ramp_time_s=ramp_time_s)


def trace_payload(record: DeliveryRecord) -> dict[str, Any]:
    """The final record's summary for the trace (no series: the record row keeps it)."""
    return record.model_dump(mode="json", exclude={"series", "trace_id", "updated_at"})
