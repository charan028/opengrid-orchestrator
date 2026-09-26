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
    heartbeats: Iterable[Heartbeat], *, now: datetime, thresholds: HealthThresholds
) -> tuple[ProcessHealth, ...]:
    """Classify all 7 processes, including ones that never reported (`heartbeat is None` -> DOWN)."""
    by_process = {hb.process: hb for hb in heartbeats}
    return tuple(
        ProcessHealth(
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


def evaluate_process_down_alert(process_health: ProcessHealth) -> AlertFinding | None:
    """ALR-PROCESS-DOWN (critical) -- any of the 7 processes."""
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
