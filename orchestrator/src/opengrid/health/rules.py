"""Pure classification and alert-rule logic for the health evaluator (02b S6.4-S6.5). No I/O -- every
function here takes already-fetched rows/values and `datetime.now(UTC)` injected as `now`, so it is
testable without a database or a clock (BUILD.md S5a: "no flaky sleeps: use injected clocks").
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from opengrid.core.models.platform import FeedStatus, Heartbeat
from opengrid.core.timeutil import is_stale
from opengrid.health.model import (
    ALL_PROCESSES,
    AlertFinding,
    AlertSeverity,
    DegradedMode,
    HealthThresholds,
    HubHealthCounts,
    HubHealthState,
    ProcessHealth,
    ProcessStatus,
)


def classify_process_status(
    heartbeat: Heartbeat | None, *, now: datetime, thresholds: HealthThresholds
) -> ProcessStatus:
    """A process is DOWN if it never sent a heartbeat, or its last one is older than
    `heartbeat_miss_threshold` missed intervals (02b S6.4)."""
    last_seen = heartbeat.ts if heartbeat else None
    return "down" if is_stale(last_seen, thresholds.heartbeat_down_after_s, now=now) else "ok"


def classify_all_processes(
    heartbeats: Iterable[Heartbeat],
    *,
    now: datetime,
    thresholds: HealthThresholds,
    self_process: str | None = None,
) -> tuple[ProcessHealth, ...]:
    """Classify every `ALL_PROCESSES` entry, including ones that never reported (`heartbeat is None` ->
    DOWN).

    `self_process`, when given, names the process this code is itself executing inside right now (health
    always runs inside `og-settle`, 02b S1.2/S6.4): that entry is always "ok" with `last_seen_at=now`
    regardless of its `og.heartbeat` row, because reaching this call at all proves it is alive (defect
    fix: a stale/momentarily-missing `settle` heartbeat write -- e.g. a slow disk or a transient DB
    hiccup on the very write this same process makes -- must never make a live process misreport itself
    as down; nothing external needs to observe `settle`'s own heartbeat to know settle is running this
    evaluation).
    """
    by_process = {hb.process: hb for hb in heartbeats}
    return tuple(
        ProcessHealth(process=process, status="ok", last_seen_at=now)
        if process == self_process
        else ProcessHealth(
            process=process,
            status=classify_process_status(by_process.get(process), now=now, thresholds=thresholds),
            last_seen_at=by_process[process].ts if process in by_process else None,
        )
        for process in ALL_PROCESSES
    )


def classify_hub_health(
    *, last_seen_at: datetime | None, fault_code: str | None, now: datetime, thresholds: HealthThresholds
) -> HubHealthState:
    """02b S6.4: `fault` wins outright (hub-reported, independent of timing); otherwise online/stale/
    offline from `hub_state.last_seen_at` age."""
    if fault_code:
        return "fault"
    if is_stale(last_seen_at, thresholds.hub_offline_s, now=now):
        return "offline"
    if is_stale(last_seen_at, thresholds.hub_online_s, now=now):
        return "stale"
    return "online"


def aggregate_hub_counts(
    classified: Iterable[tuple[str, HubHealthState]],
) -> dict[str, HubHealthCounts]:
    """Roll per-hub `(zone, state)` pairs up into per-zone counts for the health read model and
    `og_hubs`/`og_telemetry_fresh_ratio` metrics."""
    counts: dict[str, dict[str, int]] = {}
    for zone, state in classified:
        zone_counts = counts.setdefault(zone, {"online": 0, "stale": 0, "offline": 0, "fault": 0})
        zone_counts[state] += 1
    return {zone: HubHealthCounts(**c) for zone, c in counts.items()}


def derive_degraded_modes(
    *,
    feed_stale: bool,
    process_health: Iterable[ProcessHealth],
    dist_deferral_scada_silent: bool = False,
) -> frozenset[DegradedMode]:
    """02b S6.5: the current degraded mode(s) the engine reads. Independent triggers can combine."""
    modes: set[DegradedMode] = set()
    if feed_stale:
        modes.add("NO_NEW_COMMITMENTS")
    by_process = {p.process: p.status for p in process_health}
    if by_process.get("engine") == "down":
        modes.add("HOLD_LOCAL_AUTONOMY")
    if by_process.get("guardian") == "down":
        modes.add("HOLD")
    if dist_deferral_scada_silent:
        modes.add("DIST_DEFERRAL_OPEN_LOOP")
    return frozenset(modes)


def evaluate_feed_alert(
    feed_status: FeedStatus, *, now: datetime, thresholds: HealthThresholds, staleness_threshold_s: float
) -> AlertFinding | None:
    """ALR-FEED-STALE (warning) / ALR-FEED-LGV-EXHAUSTED (critical, breaker open with no fallback)."""
    key = f"{feed_status.source}:{feed_status.product}"
    if feed_status.breaker_open:
        return AlertFinding(
            rule="ALR-FEED-LGV-EXHAUSTED",
            severity="critical",
            summary=f"Feed {key} circuit breaker open, last-good-value window exhausted",
            condition_key=f"ALR-FEED-LGV-EXHAUSTED:{key}",
            detail={"source": feed_status.source, "product": feed_status.product},
        )
    if is_stale(feed_status.last_value_at, staleness_threshold_s, now=now):
        return AlertFinding(
            rule="ALR-FEED-STALE",
            severity="warning",
            summary=f"Feed {key} stale for over {staleness_threshold_s:.0f}s",
            condition_key=f"ALR-FEED-STALE:{key}",
            detail={"source": feed_status.source, "product": feed_status.product},
        )
    return None


def is_fallback_feed_needed(
    primary_feed_status: FeedStatus | None, *, now: datetime, primary_threshold_s: float
) -> bool:
    """ALR-FEED-STALE fallback-suppression (defect fix): EIA backs ERCOT's system-load product
    (`np6-345-cd`) only while ERCOT's own breaker is open or its own reading is stale --
    `opengrid.feeds.scheduler._poll_eia_fallback` runs only from inside `_poll_ercot_product` when its
    breaker blocks the request, so EIA is never even polled while ERCOT is healthy. An old/stale EIA
    `feed_status` row is then normal, not a problem, and must not raise `ALR-FEED-STALE` on its own; it
    only matters once the primary it backs is itself unavailable.

    `primary_feed_status=None` (ERCOT's own row has never been seen at all) can't be distinguished from
    "primary is fine", so this returns `True` (don't suppress) rather than risk hiding a real gap.
    """
    if primary_feed_status is None:
        return True
    if primary_feed_status.breaker_open:
        return True
    return is_stale(primary_feed_status.last_value_at, primary_threshold_s, now=now)


def evaluate_sim_offline_alert(
    latest_fleet_seen_at: datetime | None,
    latest_scada_seen_at: datetime | None,
    *,
    now: datetime,
    thresholds: HealthThresholds,
) -> AlertFinding | None:
    """ALR-SIM-OFFLINE (critical): `ogsim` (the integration simulators) is an external system that shares
    no code with `opengrid` (BUILD.md S1) and never writes an `og.heartbeat` row (see `ALL_PROCESSES`'s
    docstring), so its liveness is inferred from its own MQTT-driven writes instead -- the freshest hub
    telemetry (`og.hub_state.last_seen_at`, written by `ogsim.fleet`) and the freshest SCADA reading
    (`og.feed_obs` where `source='scada'`, written by `ogsim.scada`).

    Neither signal existing yet (a cold start, before any telemetry has ever arrived) is not evidence of
    an offline sim -- only "used to be fresh, now isn't" is, so this returns `None` when both are `None`.
    """
    candidates = [ts for ts in (latest_fleet_seen_at, latest_scada_seen_at) if ts is not None]
    if not candidates:
        return None
    freshest = max(candidates)
    if not is_stale(freshest, thresholds.sim_offline_s, now=now):
        return None
    return AlertFinding(
        rule="ALR-SIM-OFFLINE",
        severity="critical",
        summary=f"Integration simulators silent for over {thresholds.sim_offline_s:.0f}s "
        "(no fleet telemetry or SCADA reading)",
        condition_key="ALR-SIM-OFFLINE",
        detail={
            "latest_fleet_seen_at": latest_fleet_seen_at.isoformat() if latest_fleet_seen_at else None,
            "latest_scada_seen_at": latest_scada_seen_at.isoformat() if latest_scada_seen_at else None,
        },
    )


def evaluate_process_down_alert(process_health: ProcessHealth) -> AlertFinding | None:
    """ALR-PROCESS-DOWN (critical) -- any monitored `opengrid` process (`ALL_PROCESSES`)."""
    if process_health.status != "down":
        return None
    return AlertFinding(
        rule="ALR-PROCESS-DOWN",
        severity="critical",
        summary=f"Process {process_health.process} heartbeat missing",
        condition_key=f"ALR-PROCESS-DOWN:{process_health.process}",
        detail={"process": process_health.process},
    )


def evaluate_hub_offline_ratio_alert(
    zone: str, counts: HubHealthCounts, *, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-HUB-OFFLINE-RATIO: warning > 5%, critical > 20% offline+fault in a zone (02b S6.4)."""
    ratio = counts.offline_ratio
    severity: AlertSeverity
    if ratio > thresholds.hub_offline_ratio_critical:
        severity = "critical"
    elif ratio > thresholds.hub_offline_ratio_warning:
        severity = "warning"
    else:
        return None
    return AlertFinding(
        rule="ALR-HUB-OFFLINE-RATIO",
        severity=severity,
        summary=f"Zone {zone} hub offline ratio {ratio:.1%}",
        condition_key=f"ALR-HUB-OFFLINE-RATIO:{zone}",
        detail={"zone": zone, "ratio": ratio, "total": counts.total},
    )


def evaluate_cycle_latency_alert(
    p99_s: float | None, consecutive_breaches: int, *, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-CYCLE-P99: warning if p99 > budget for `cycle_p99_breach_cycles` consecutive cycles."""
    if p99_s is None or p99_s <= thresholds.cycle_p99_budget_s:
        return None
    if consecutive_breaches < thresholds.cycle_p99_breach_cycles:
        return None
    return AlertFinding(
        rule="ALR-CYCLE-P99",
        severity="warning",
        summary=f"RT cycle p99 {p99_s * 1000:.0f} ms over {thresholds.cycle_p99_budget_s * 1000:.0f} ms budget",
        condition_key="ALR-CYCLE-P99",
        detail={"p99_s": p99_s, "consecutive_breaches": consecutive_breaches},
    )


def evaluate_cycle_latency_warning_alert(
    p99_s: float | None, *, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-CYCLE-P99-APPROACHING (warning, R2 pre-limit alert): fires the instant p99 crosses
    `cycle_p99_warn_ratio` of the budget (80% by default) -- unlike `evaluate_cycle_latency_alert`, no
    `cycle_p99_breach_cycles` consecutive-cycle requirement, so operators get an early warning before the
    hard breach. Mutually exclusive with `ALR-CYCLE-P99`: this only fires strictly below the full budget,
    the harder rule owns everything at or past it."""
    if p99_s is None:
        return None
    warn_at_s = thresholds.cycle_p99_budget_s * thresholds.cycle_p99_warn_ratio
    if p99_s < warn_at_s or p99_s > thresholds.cycle_p99_budget_s:
        return None
    return AlertFinding(
        rule="ALR-CYCLE-P99-APPROACHING",
        severity="warning",
        summary=(
            f"RT cycle p99 {p99_s * 1000:.0f} ms approaching "
            f"{thresholds.cycle_p99_budget_s * 1000:.0f} ms budget "
            f"({thresholds.cycle_p99_warn_ratio:.0%})"
        ),
        condition_key="ALR-CYCLE-P99-APPROACHING",
        detail={"p99_s": p99_s, "budget_s": thresholds.cycle_p99_budget_s},
    )


def evaluate_guardian_timeout_alert(
    timeout_rate: float | None, *, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-GUARDIAN-TIMEOUT-RATE: critical if guardian verdict timeout rate > 1%."""
    if timeout_rate is None or timeout_rate <= thresholds.guardian_timeout_rate_critical:
        return None
    return AlertFinding(
        rule="ALR-GUARDIAN-TIMEOUT-RATE",
        severity="critical",
        summary=f"Guardian verdict timeout rate {timeout_rate:.1%}",
        condition_key="ALR-GUARDIAN-TIMEOUT-RATE",
        detail={"timeout_rate": timeout_rate},
    )


def evaluate_scada_overload_alert(
    bank_id: str, load_kva: float | None, kva_rating: float, *, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-SCADA-OVERLOAD: a bank's latest reported SCADA apparent-power load over its `kva_rating`
    (the anomaly catalogue's "bank_overload" injection target, `integration-sims/src/ogsim/control/
    catalogue.py`). `load_kva=None` (no SCADA reading has ever arrived for this bank) is not an
    overload -- that is `ALR-FEED-STALE`'s/a comms-loss concern, not this rule's."""
    if load_kva is None or kva_rating <= 0:
        return None
    ratio = load_kva / kva_rating
    severity: AlertSeverity
    if ratio > thresholds.bank_kva_overload_critical_pct:
        severity = "critical"
    elif ratio > thresholds.bank_kva_overload_warning_pct:
        severity = "warning"
    else:
        return None
    return AlertFinding(
        rule="ALR-SCADA-OVERLOAD",
        severity=severity,
        summary=f"Bank {bank_id} SCADA load {load_kva:.1f} kVA over rating {kva_rating:.1f} kVA ({ratio:.0%})",
        condition_key=f"ALR-SCADA-OVERLOAD:{bank_id}",
        detail={"bank_id": bank_id, "load_kva": load_kva, "kva_rating": kva_rating, "ratio": ratio},
    )


def evaluate_energy_shortfall_risk_alert(
    *,
    obligation_id: str,
    customer_id: str | None,
    margin_kwh: float,
    time_to_depletion_h: float | None,
) -> AlertFinding:
    """ALR-ENERGY-SHORTFALL-RISK (K1, continuous per-obligation energy-sufficiency check): a committed
    obligation's eligible hubs no longer hold enough energy above reserve to sustain its remaining
    delivery window. Always `critical` -- unlike the other alert rules here, this one is raised
    directly by the allocator/engine energy-sufficiency hook the instant AT_RISK is detected (not
    polled for by the health evaluator), so there is no severity threshold to size here; this function
    exists so the health module owns exactly one place that names the rule/severity/summary shape,
    matching every other `ALR-*` rule in this file (BUILD.md S1 "no duplicated functions")."""
    return AlertFinding(
        rule="ALR-ENERGY-SHORTFALL-RISK",
        severity="critical",
        summary=(
            f"Obligation {obligation_id} energy margin {margin_kwh:.2f} kWh"
            + (f", depletes in {time_to_depletion_h:.2f}h" if time_to_depletion_h is not None else "")
        ),
        condition_key=f"ALR-ENERGY-SHORTFALL-RISK:{obligation_id}",
        detail={
            "obligation_id": obligation_id,
            "customer_id": customer_id,
            "margin_kwh": margin_kwh,
            "time_to_depletion_h": time_to_depletion_h,
        },
    )


def evaluate_reserve_breach_alert(reserve_breach_count: float) -> AlertFinding | None:
    """ALR-RESERVE-BREACH: critical, page-equivalent (A10: must stay 0)."""
    if reserve_breach_count <= 0:
        return None
    return AlertFinding(
        rule="ALR-RESERVE-BREACH",
        severity="critical",
        summary=f"Reserve-floor breach counter at {reserve_breach_count:.0f} (must be 0, A10)",
        condition_key="ALR-RESERVE-BREACH",
        detail={"count": reserve_breach_count},
    )
