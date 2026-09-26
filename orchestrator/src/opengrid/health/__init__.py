"""opengrid.health -- evaluator run inside og-settle (02b S6.4). Owner: health agent (BUILD.md S4).

Aggregates heartbeats, feed/hub freshness, cycle latency and breaker states; raises/clears `og.alert`
rows per the `ALR-*` rule set; derives the degraded mode(s) the engine reads (02b S6.5); and exposes the
health read model the API surfaces at `GET /og/api/health`.

Isolated package so it can move to its own systemd unit in MVP-J without a redesign (02b S1.2). I/O
lives in `health.queries` (DB) and `health.metrics_scrape` (cross-process `/metrics` reads); pure
classification/alert logic lives in `health.rules`; this module only wires them together.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Literal

from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import FeedStatus
from opengrid.feeds.scheduler import EIA_FALLBACK_PRODUCT
from opengrid.feeds.staleness import threshold_s_for_product
from opengrid.health import queries
from opengrid.health.metrics_scrape import (
    histogram_p99_from_buckets,
    parse_prometheus_text,
    scrape_metrics_text,
    sum_metric,
)
from opengrid.health.model import (
    HEALTH_OWNED_ALERT_RULES,
    CycleLatencySample,
    HealthSnapshot,
    HealthThresholds,
    HubHealthCounts,
)
from opengrid.health.rules import (
    aggregate_hub_counts,
    classify_all_processes,
    classify_hub_health,
    derive_degraded_modes,
    evaluate_cycle_latency_alert,
    evaluate_feed_alert,
    evaluate_guardian_timeout_alert,
    evaluate_hub_offline_ratio_alert,
    evaluate_process_down_alert,
    evaluate_reserve_breach_alert,
    evaluate_scada_overload_alert,
    evaluate_sim_offline_alert,
    is_fallback_feed_needed,
)
from opengrid.platform.config import Config
from opengrid.platform.process import run_forever

logger = logging.getLogger(__name__)

ProcessStatus = Literal["ok", "down"]

_CYCLE_LATENCY_HISTORY_LEN = 150  # ~5 min at a 2 s cycle (02b S6.4 "p99 over the last 5 minutes")

# `health` always runs inside `og-settle` (02b S1.2/S6.4) -- see `classify_all_processes`'s
# `self_process` docstring for why that entry never reads its own `og.heartbeat` row to decide status.
_SELF_PROCESS_NAME = "settle"

# Module-level wiring set by `configure()` at og-settle startup (BUILD.md S4: settle calls health's
# run()/evaluate_once() entry). Kept module-level, matching the other stub packages' no-argument
# public interface (INTERFACES.md), rather than threading pool/cfg through every call site.
_pool: AsyncConnectionPool | None = None
_thresholds: HealthThresholds = HealthThresholds()
_engine_metrics_url: str | None = None
_guardian_metrics_url: str | None = None
_scrape_timeout_s: float = 2.0
_cycle_p99_history: deque[float] = deque(maxlen=_CYCLE_LATENCY_HISTORY_LEN)
_cycle_p99_consecutive_breaches: int = 0
# `[feeds.staleness]` config, e.g. `eia_fresh_s`/`ercot_price_fresh_s`/`nws_fresh_s` -- resolved
# per-feed via `opengrid.feeds.staleness.threshold_s_for_product` (defect fix: ALR-FEED-STALE used to
# apply `health.heartbeat_down_after_s`, a heartbeat-scale threshold of a few seconds, to every feed
# regardless of its own posting cadence, which paged EIA -- a ~3h cadence source -- as "stale" within
# seconds of a fresh read, and made every feed alert over-eager).
_staleness_cfg: dict[str, float] = {}


def configure(pool: AsyncConnectionPool, cfg: Config) -> None:
    """Called once by `og-settle`'s startup before the first `run()`/`evaluate_once()` tick."""
    global _pool, _thresholds, _engine_metrics_url, _guardian_metrics_url, _scrape_timeout_s, _staleness_cfg
    _pool = pool
    _thresholds = HealthThresholds.from_config(cfg)
    _engine_metrics_url = cfg.get("health.engine_metrics_url")
    _guardian_metrics_url = cfg.get("health.guardian_metrics_url")
    _scrape_timeout_s = cfg.get("health.metrics_scrape_timeout_s", 2.0)
    _staleness_cfg = cfg.get("feeds.staleness", {})

    # opengrid.invariants (K1/K2/K13 + orphan bookkeeping + K11 trace verification) has no process
    # entry point of its own -- it piggybacks on og-settle's existing health cadence instead. Local
    # import: breaks the import cycle (invariants.trace_verify reads opengrid.health.queries/model),
    # and keeps this package's only touch on `health` to these two lines (BUILD.md task brief: "keep
    # your edit to a small hook").
    from opengrid import invariants

    invariants.configure(pool, cfg)


def _require_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("opengrid.health.configure() must be called before use")
    return _pool


def _now() -> datetime:
    """The evaluator's clock, factored out so tests can inject a fixed instant (BUILD.md S5a "no flaky
    sleeps: use injected clocks") without changing any of this module's fixed no-argument signatures."""
    return datetime.now(UTC)


def _feed_staleness_threshold_s(feed_status: FeedStatus) -> float:
    """ALR-FEED-STALE's per-feed staleness budget (defect fix): reuses `opengrid.feeds.staleness`'s
    own `[feeds.staleness]` thresholds (`eia_fresh_s`, `ercot_price_fresh_s`, `nws_fresh_s`, ...)
    instead of duplicating them here (BUILD.md S1 "no duplicated functions"). Previously this module
    passed `health.heartbeat_down_after_s` -- a few-second, heartbeat-scale budget -- to every feed
    regardless of its real posting cadence, so a ~3h-cadence source like EIA was reported STALE within
    seconds of a fresh read, and every feed's staleness alert was over-eager."""
    return threshold_s_for_product(feed_status.source, feed_status.product, _staleness_cfg)


def _feeds_needing_stale_check(feed_statuses: list[FeedStatus], *, now: datetime) -> list[FeedStatus]:
    """Drops EIA's fallback `feed_status` row from ALR-FEED-STALE consideration while the ERCOT product
    it backs (`EIA_FALLBACK_PRODUCT`) is itself healthy (defect fix, see `is_fallback_feed_needed`'s
    docstring) -- an idle-and-therefore-old EIA row is then normal, not a problem. Every other feed
    passes through unchanged."""
    by_key = {(fs.source, fs.product): fs for fs in feed_statuses}
    result = []
    for feed_status in feed_statuses:
        if feed_status.source == "EIA":
            primary = by_key.get(("ERCOT", EIA_FALLBACK_PRODUCT))
            primary_threshold_s = (
                _feed_staleness_threshold_s(primary)
                if primary is not None
                else _thresholds.heartbeat_down_after_s
            )
            if not is_fallback_feed_needed(primary, now=now, primary_threshold_s=primary_threshold_s):
                continue
        result.append(feed_status)
    return result


async def evaluate_heartbeats() -> dict[str, ProcessStatus]:
    """Read every monitored `opengrid` process's `og.heartbeat` row (`ALL_PROCESSES`); a process is
    "down" after `health.heartbeat_miss_threshold` missed intervals (02b S6.4). `settle` (this evaluator's
    own host process) is always "ok" -- see `classify_all_processes`'s `self_process` docstring."""
    heartbeats = await queries.fetch_heartbeats(_require_pool())
    processes = classify_all_processes(
        heartbeats, now=_now(), thresholds=_thresholds, self_process=_SELF_PROCESS_NAME
    )
    return {p.process: p.status for p in processes}


async def evaluate_hub_health() -> None:
    """Classify each hub online/stale/offline from `hub_state.last_seen_at` (02b S6.4 thresholds) and
    write the classification back onto `hub_state.health` so `fleet.capability()` excludes it.

    Only hubs whose classification actually changed since the last cycle are written, in one batched
    statement (`queries.write_hub_health_batch`) under asynchronous commit -- not one single-row
    `UPDATE`+commit per hub (dispatch-live pass: ~2,000 hubs/cycle, most unchanged cycle-to-cycle, were
    contending with the engine's own writes for the base server's ~0.5s WAL fsync)."""
    pool = _require_pool()
    now = _now()
    changes: list[tuple[str, str]] = []
    for _zone, hub_id, last_seen_at, fault_code, current_health in await queries.fetch_hub_states(pool):
        state = classify_hub_health(
            last_seen_at=last_seen_at, fault_code=fault_code, now=now, thresholds=_thresholds
        )
        if state != current_health:
            changes.append((hub_id, state))
    await queries.write_hub_health_batch(pool, changes)


async def _fetch_cycle_latency(now: datetime) -> CycleLatencySample:
    global _cycle_p99_consecutive_breaches
    if _engine_metrics_url is None:
        return CycleLatencySample(p99_s=None, consecutive_breaches=0)
    try:
        text = await scrape_metrics_text(_engine_metrics_url, timeout_s=_scrape_timeout_s)
    except Exception:
        logger.warning("cycle latency scrape failed", extra={"url": _engine_metrics_url})
        return CycleLatencySample(p99_s=None, consecutive_breaches=_cycle_p99_consecutive_breaches)

    samples = parse_prometheus_text(text)
    p99_s = histogram_p99_from_buckets(samples, "og_control_tick_duration_seconds")
    _cycle_p99_history.append(p99_s if p99_s is not None else 0.0)
    if p99_s is not None and p99_s > _thresholds.cycle_p99_budget_s:
        _cycle_p99_consecutive_breaches += 1
    else:
        _cycle_p99_consecutive_breaches = 0
    return CycleLatencySample(p99_s=p99_s, consecutive_breaches=_cycle_p99_consecutive_breaches)


async def _fetch_guardian_timeout_rate() -> float | None:
    if _guardian_metrics_url is None:
        return None
    try:
        text = await scrape_metrics_text(_guardian_metrics_url, timeout_s=_scrape_timeout_s)
    except Exception:
        logger.warning("guardian metrics scrape failed", extra={"url": _guardian_metrics_url})
        return None

    samples = parse_prometheus_text(text)
    total = sum_metric(samples, "og_guardian_verdicts_total")
    if total <= 0:
        return None
    timeouts = sum_metric(samples, "og_guardian_verdicts_total", label_filter='outcome="timeout"')
    return timeouts / total


async def _fetch_reserve_breach_count() -> float:
    if _engine_metrics_url is None:
        return 0.0
    try:
        text = await scrape_metrics_text(_engine_metrics_url, timeout_s=_scrape_timeout_s)
    except Exception:
        return 0.0

    samples = parse_prometheus_text(text)
    return sum_metric(samples, "og_reserve_breaches_total")


async def evaluate_alerts() -> None:
    """Run the MVP-S `ALR-*` alert rule set (feed stale, process down, hub offline ratio, cycle p99,
    guardian verdict timeout rate, reserve-breach counter) and insert `og.alert` rows (02b S6.4).

    Raises a new alert only when its `condition_key` has no currently-open row (TS-07-06: no duplicate
    storm), and clears any open alert whose condition no longer evaluates true -- but only for a rule in
    `HEALTH_OWNED_ALERT_RULES` (defect fix: this must never auto-clear an alert another module raised and
    owns, e.g. `ALR-SETTLE-STALLED`, `ALR-SELECTOR-GATE-FAILED`, `ALR-ENERGY-SHORTFALL-RISK`).
    """
    pool = _require_pool()
    now = _now()

    heartbeats = await queries.fetch_heartbeats(pool)
    processes = classify_all_processes(
        heartbeats, now=now, thresholds=_thresholds, self_process=_SELF_PROCESS_NAME
    )

    hub_rows = await queries.fetch_hub_states(pool)
    classified_hubs = [
        (zone, classify_hub_health(last_seen_at=seen, fault_code=fault, now=now, thresholds=_thresholds))
        for zone, _hub_id, seen, fault, _current_health in hub_rows
    ]
    hub_counts_by_zone = aggregate_hub_counts(classified_hubs)

    feed_statuses = await queries.fetch_feed_statuses(pool)
    cycle_latency = await _fetch_cycle_latency(now)
    guardian_timeout_rate = await _fetch_guardian_timeout_rate()
    reserve_breach_count = await _fetch_reserve_breach_count()
    bank_loads = await queries.fetch_bank_loads(pool)
    latest_fleet_seen_at = await queries.fetch_latest_hub_seen_at(pool)
    latest_scada_seen_at = await queries.fetch_latest_scada_obs_at(pool)

    findings = []
    for bank_id, kva_rating, load_kva in bank_loads:
        finding = evaluate_scada_overload_alert(bank_id, load_kva, kva_rating, thresholds=_thresholds)
        if finding:
            findings.append(finding)
    for feed_status in _feeds_needing_stale_check(feed_statuses, now=now):
        finding = evaluate_feed_alert(
            feed_status,
            now=now,
            thresholds=_thresholds,
            staleness_threshold_s=_feed_staleness_threshold_s(feed_status),
        )
        if finding:
            findings.append(finding)
    for process_health in processes:
        finding = evaluate_process_down_alert(process_health)
        if finding:
            findings.append(finding)
    for zone, counts in hub_counts_by_zone.items():
        finding = evaluate_hub_offline_ratio_alert(zone, counts, thresholds=_thresholds)
        if finding:
            findings.append(finding)
    cycle_finding = evaluate_cycle_latency_alert(
        cycle_latency.p99_s, cycle_latency.consecutive_breaches, thresholds=_thresholds
    )
    if cycle_finding:
        findings.append(cycle_finding)
    guardian_finding = evaluate_guardian_timeout_alert(guardian_timeout_rate, thresholds=_thresholds)
    if guardian_finding:
        findings.append(guardian_finding)
    reserve_finding = evaluate_reserve_breach_alert(reserve_breach_count)
    if reserve_finding:
        findings.append(reserve_finding)
    sim_offline_finding = evaluate_sim_offline_alert(
        latest_fleet_seen_at, latest_scada_seen_at, now=now, thresholds=_thresholds
    )
    if sim_offline_finding:
        findings.append(sim_offline_finding)

    open_alerts = await queries.fetch_open_alerts(pool)
    open_by_key = {queries.condition_key_for(a): a for a in open_alerts}
    live_keys = {f.condition_key for f in findings}

    for finding in findings:
        if finding.condition_key not in open_by_key:
            await queries.raise_alert(pool, finding, opened_at=now)

    for key, alert in open_by_key.items():
        # Defect fix: never auto-clear an alert this evaluator doesn't own the rule for -- see
        # `HEALTH_OWNED_ALERT_RULES`'s docstring. Other modules (settle, selector, the allocator/engine
        # energy hook, ...) raise and clear their own alerts; this pass must leave them alone.
        if alert.rule not in HEALTH_OWNED_ALERT_RULES:
            continue
        if key not in live_keys and alert.id is not None:
            await queries.clear_alert(pool, alert.id, cleared_at=now)


async def evaluate_once() -> HealthSnapshot:
    """Run one full health-evaluation cycle (heartbeats, hub health, alerts) and return the read model
    the API's `GET /og/api/health` and `stream/health` expose. This is the entry og-settle's process
    loop calls every cycle (BUILD.md S4 "health" row)."""
    pool = _require_pool()
    now = _now()

    await evaluate_hub_health()
    await evaluate_alerts()

    # opengrid.invariants' own hook: it gates its actual check cadence internally (Cadence, independent
    # of health's ~5s loop), so calling it every cycle here does not mean it runs every cycle.
    from opengrid import invariants

    await invariants.run_due()

    heartbeats = await queries.fetch_heartbeats(pool)
    processes = classify_all_processes(
        heartbeats, now=now, thresholds=_thresholds, self_process=_SELF_PROCESS_NAME
    )

    hub_rows = await queries.fetch_hub_states(pool)
    classified_hubs = [
        (zone, classify_hub_health(last_seen_at=seen, fault_code=fault, now=now, thresholds=_thresholds))
        for zone, _hub_id, seen, fault, _current_health in hub_rows
    ]
    hub_counts_by_zone = aggregate_hub_counts(classified_hubs)

    feed_statuses = await queries.fetch_feed_statuses(pool)
    any_feed_stale = any(
        evaluate_feed_alert(
            fs, now=now, thresholds=_thresholds, staleness_threshold_s=_feed_staleness_threshold_s(fs)
        )
        is not None
        for fs in _feeds_needing_stale_check(feed_statuses, now=now)
    )
    cycle_latency = await _fetch_cycle_latency(now)
    degraded_modes = derive_degraded_modes(feed_stale=any_feed_stale, process_health=processes)
    open_alerts = await queries.fetch_open_alerts(pool)

    return HealthSnapshot(
        evaluated_at=now,
        processes=processes,
        hub_counts_by_zone=hub_counts_by_zone or {},
        cycle_latency=cycle_latency,
        degraded_modes=degraded_modes,
        open_alert_count=len(open_alerts),
    )


async def run(pool: AsyncConnectionPool, cfg: Config, *, interval_s: float | None = None) -> None:
    """Entry point og-settle's process wiring calls: `configure()` then loop `evaluate_once()` on
    `health.heartbeat_interval_s` (or `interval_s` if given) until shutdown (02b S1.2/S1.3)."""
    configure(pool, cfg)
    tick_interval = interval_s if interval_s is not None else _thresholds.heartbeat_interval_s

    async def _tick() -> None:
        await evaluate_once()

    await run_forever(_tick, interval_s=tick_interval, process_name="health")


__all__ = [
    "HealthSnapshot",
    "HealthThresholds",
    "HubHealthCounts",
    "configure",
    "evaluate_alerts",
    "evaluate_heartbeats",
    "evaluate_hub_health",
    "evaluate_once",
    "run",
]
