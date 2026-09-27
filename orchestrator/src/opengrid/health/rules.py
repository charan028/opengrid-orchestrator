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
    normalize_feed_key,
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
    # Structured scope (migration 0024), additive alongside the existing "source"/"product" keys
    # `condition_key_for` already reads -- see `opengrid.health.queries.raise_alert`'s docstring.
    scope = {"scope_kind": "FEED", "scope_ref": key}
    if feed_status.breaker_open:
        return AlertFinding(
            rule="ALR-FEED-LGV-EXHAUSTED",
            severity="critical",
            summary=f"Feed {key} circuit breaker open, last-good-value window exhausted",
            condition_key=f"ALR-FEED-LGV-EXHAUSTED:{key}",
            detail={"source": feed_status.source, "product": feed_status.product, **scope},
        )
    if is_stale(feed_status.last_value_at, staleness_threshold_s, now=now):
        return AlertFinding(
            rule="ALR-FEED-STALE",
            severity="warning",
            summary=f"Feed {key} stale for over {staleness_threshold_s:.0f}s",
            condition_key=f"ALR-FEED-STALE:{key}",
            detail={"source": feed_status.source, "product": feed_status.product, **scope},
        )
    return None


def _normalized_firm_blocking_feeds(thresholds: HealthThresholds) -> frozenset[str]:
    """`thresholds.firm_blocking_feeds` re-normalised at comparison time: `HealthThresholds.from_config`
    already normalises via `_resolve_firm_blocking_feeds`, but a directly-constructed `HealthThresholds`
    (e.g. in tests, or a future caller) may not have -- normalising here too means membership checks are
    correct either way, not just when the config path was used."""
    return frozenset(normalize_feed_key(*entry.split(":", 1)) for entry in thresholds.firm_blocking_feeds)


def is_firm_blocking_feed(feed_status: FeedStatus, *, thresholds: HealthThresholds) -> bool:
    """R3 hotfix: whether `feed_status` is one of the feeds `NO_NEW_COMMITMENTS` (02b S6.5 row 1) may
    gate on -- `thresholds.firm_blocking_feeds`, keyed by `normalize_feed_key(source, product)`. Every
    feed still raises its own `ALR-FEED-STALE`/`ALR-FEED-LGV-EXHAUSTED` regardless of this check (see
    `evaluate_feed_alert`, called unconditionally); this only narrows which stale feeds are allowed to
    block new commitments -- previously ANY stale feed did, including ERCOT system-load ACTUALS
    (np6-345-cd), NWS and the EIA fallback, none of which feed firm pricing, which blocked production
    commitments for no reason."""
    key = normalize_feed_key(feed_status.source, feed_status.product)
    return key in _normalized_firm_blocking_feeds(thresholds)


def missing_firm_blocking_feeds(
    feed_statuses: Iterable[FeedStatus], *, thresholds: HealthThresholds
) -> frozenset[str]:
    """R3 review fix (MEDIUM): the `NO_NEW_COMMITMENTS` feed-staleness check used to only iterate
    EXISTING `feed_status` rows, so a blocking feed with NO row at all (a fresh database, or a deleted
    row) was silently treated as fine -- it blocked nothing, disabling the very protection
    `firm_blocking_feeds` exists for. Returns the subset of `thresholds.firm_blocking_feeds` that have no
    matching `feed_status` row; a non-empty result must be treated the same as a stale reading."""
    present = {normalize_feed_key(fs.source, fs.product) for fs in feed_statuses}
    return _normalized_firm_blocking_feeds(thresholds) - present


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


def is_scada_silent(
    latest_scada_seen_at: datetime | None, *, now: datetime, thresholds: HealthThresholds
) -> bool:
    """DM-09 / ES07-S02 / `DegradedMode.DIST_DEFERRAL_OPEN_LOOP`: true once no SCADA bank reading
    (`og.feed_obs` where `source='scada'`) has arrived for over `scada_silent_s`.

    A cold start (`latest_scada_seen_at=None`, no SCADA reading has ever arrived) is NOT silent -- the
    same rule as `evaluate_sim_offline_alert`'s "no data yet is not evidence of an offline sim": there is
    no evidence either way yet, so this must not trip the instant og-settle starts, before the first
    reading has had a chance to land.
    """
    if latest_scada_seen_at is None:
        return False
    return is_stale(latest_scada_seen_at, thresholds.scada_silent_s, now=now)


def evaluate_scada_silent_alert(
    latest_scada_seen_at: datetime | None, *, now: datetime, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-SCADA-SILENT (critical, DM-09 / ES07-S02): no SCADA bank reading for over `scada_silent_s` --
    SCADA-dependent dispatch loops must HOLD then SCHEDULE (07 S6.8) until it recovers. Clears
    automatically once a fresh reading arrives (this function simply stops returning a finding; the usual
    raise-once/clear-on-resolve wiring in `evaluate_alerts()` does the rest). See `is_scada_silent`'s
    docstring for the cold-start exception."""
    if not is_scada_silent(latest_scada_seen_at, now=now, thresholds=thresholds):
        return None
    return AlertFinding(
        rule="ALR-SCADA-SILENT",
        severity="critical",
        summary=f"No SCADA bank reading for over {thresholds.scada_silent_s:.0f}s (DM-09)",
        condition_key="ALR-SCADA-SILENT",
        detail={
            "latest_scada_seen_at": latest_scada_seen_at.isoformat() if latest_scada_seen_at else None,
        },
    )


def evaluate_per_bank_scada_silent_alert(
    bank_id: str, latest_seen_at: datetime | None, *, now: datetime, thresholds: HealthThresholds
) -> AlertFinding | None:
    """ALR-SCADA-SILENT-BANK (warning, DM-09 / ES07-S02, R3 review fix): a single bank's own SCADA
    reading has gone silent for over `scada_silent_s`, even while OTHER banks keep reporting.

    The fleet-wide `evaluate_scada_silent_alert` (critical) only looks at the FRESHEST reading across the
    whole fleet, so one bank's feed silently dying while every other bank keeps reporting never moved that
    freshest timestamp and was invisible -- this per-bank check is independent and stays alongside the
    fleet-wide one (both can be open at once). Cold start (`latest_seen_at=None`, this bank has never
    reported) is not silent, same rule as `is_scada_silent`.

    Rate-limited for free: `condition_key` is scoped per bank (`ALR-SCADA-SILENT-BANK:{bank_id}`), so
    `evaluate_alerts()`'s usual raise-once/clear-on-resolve wiring only raises it once per bank per
    silence episode, not every cycle.
    """
    if latest_seen_at is None or not is_stale(latest_seen_at, thresholds.scada_silent_s, now=now):
        return None
    return AlertFinding(
        rule="ALR-SCADA-SILENT-BANK",
        severity="warning",
        summary=f"Bank {bank_id} SCADA silent for over {thresholds.scada_silent_s:.0f}s (DM-09)",
        condition_key=f"ALR-SCADA-SILENT-BANK:{bank_id}",
        detail={
            "bank_id": bank_id,
            "scope_kind": "BANK",
            "scope_ref": bank_id,
            "latest_seen_at": latest_seen_at.isoformat(),
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
        detail={
            "process": process_health.process,
            "scope_kind": "PROCESS",
            "scope_ref": process_health.process,
        },
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
        detail={"zone": zone, "ratio": ratio, "total": counts.total, "scope_kind": "ZONE", "scope_ref": zone},
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
        detail={
            "bank_id": bank_id,
            "load_kva": load_kva,
            "kva_rating": kva_rating,
            "ratio": ratio,
            "scope_kind": "BANK",
            "scope_ref": bank_id,
        },
    )


def evaluate_command_bad_signature_alert(
    bank_id: str, hub_ids: list[str], rejected_count: int, latest_at: datetime
) -> AlertFinding | None:
    """ALR-COMMAND-BAD-SIGNATURE (critical, #43 B3): hubs on `bank_id` rejected `rejected_count`
    command batches as BAD_SIGNATURE within `command_bad_signature_window_s` (`queries.
    fetch_bad_signature_acks_by_bank` applies the window). Every legitimate batch is guardian-signed, so a
    signature rejection means a forged or tampered command reached a hub and was refused (A3) -- the
    `demo-04-tampered-command` self-test's REJECTED ack lands here. Scoped per bank so the usual
    raise-once/clear-on-resolve wiring applies; it clears once the window passes with no new rejection."""
    if rejected_count <= 0:
        return None
    hubs = ", ".join(sorted(hub_ids))
    return AlertFinding(
        rule="ALR-COMMAND-BAD-SIGNATURE",
        severity="critical",
        summary=(
            f"Bank {bank_id}: {rejected_count} command batch(es) rejected by hub(s) {hubs} "
            "(BAD_SIGNATURE: forged or tampered, never applied)"
        ),
        condition_key=f"ALR-COMMAND-BAD-SIGNATURE:{bank_id}",
        detail={
            "bank_id": bank_id,
            "hub_ids": sorted(hub_ids),
            "rejected_count": rejected_count,
            "latest_at": latest_at.isoformat(),
            "reject_reason": "BAD_SIGNATURE",
            "scope_kind": "BANK",
            "scope_ref": bank_id,
        },
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
            "scope_kind": "OBLIGATION",
            "scope_ref": obligation_id,
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


def evaluate_limit_proximity_alert(
    *,
    rule: str,
    scope_kind: str,
    scope_ref: str,
    value: float | None,
    limit: float,
    unit: str,
    consecutive_cycles: int,
    warn_ratio: float,
    required_consecutive_cycles: int,
) -> AlertFinding | None:
    """Generic "sustained limit proximity" rule (R2 item 3, prepared ahead of FLEET-SIM landing meter
    export / temperature telemetry -- see `evaluate_meter_export_limit_alert`/
    `evaluate_temperature_limit_alert`, its two named wrappers below). Warning once a reading has stayed
    at or above `warn_ratio` of its limit for `required_consecutive_cycles` consecutive cycles in a row --
    mirrors `evaluate_cycle_latency_alert`'s consecutive-breach pattern, so a momentary spike doesn't
    raise an alert but a sustained one does. `value=None` (no reading yet) never alerts, same rule as
    `evaluate_scada_overload_alert`'s "no reading yet is not an overload"."""
    if value is None or limit <= 0:
        return None
    ratio = value / limit
    if ratio < warn_ratio or consecutive_cycles < required_consecutive_cycles:
        return None
    return AlertFinding(
        rule=rule,
        severity="warning",
        summary=(
            f"{scope_kind} {scope_ref} {value:.1f}{unit} at {ratio:.0%} of limit {limit:.1f}{unit}, "
            f"sustained {consecutive_cycles} cycles"
        ),
        condition_key=f"{rule}:{scope_ref}",
        detail={
            "scope_kind": scope_kind,
            "scope_ref": scope_ref,
            "value": value,
            "limit": limit,
            "ratio": ratio,
            "consecutive_cycles": consecutive_cycles,
        },
    )


def evaluate_meter_export_limit_alert(
    hub_id: str,
    export_kw: float | None,
    limit_kw: float,
    consecutive_cycles: int,
    *,
    thresholds: HealthThresholds,
) -> AlertFinding | None:
    """ALR-METER-EXPORT-LIMIT (warning): a hub's meter-reported export power sustained near its limit.

    NOT YET WIRED into `evaluate_alerts()`: FLEET-SIM hasn't landed meter-export telemetry (no
    `og.hub_state`/`og.feed_obs` column for it yet). Prepared now so wiring it in once that telemetry
    exists is a `queries.fetch_*` call plus one line in `evaluate_alerts()`, not a new design."""
    return evaluate_limit_proximity_alert(
        rule="ALR-METER-EXPORT-LIMIT",
        scope_kind="HUB",
        scope_ref=hub_id,
        value=export_kw,
        limit=limit_kw,
        unit="kW",
        consecutive_cycles=consecutive_cycles,
        warn_ratio=thresholds.meter_export_warn_ratio,
        required_consecutive_cycles=thresholds.limit_proximity_sustained_cycles,
    )


def evaluate_temperature_limit_alert(
    hub_id: str,
    temperature_c: float | None,
    limit_c: float,
    consecutive_cycles: int,
    *,
    thresholds: HealthThresholds,
) -> AlertFinding | None:
    """ALR-TEMPERATURE-LIMIT (warning): a hub's reported inverter/battery temperature sustained near its
    limit. NOT YET WIRED into `evaluate_alerts()` -- see `evaluate_meter_export_limit_alert`'s docstring;
    same situation, same telemetry gap (FLEET-SIM hasn't landed temperature readings yet)."""
    return evaluate_limit_proximity_alert(
        rule="ALR-TEMPERATURE-LIMIT",
        scope_kind="HUB",
        scope_ref=hub_id,
        value=temperature_c,
        limit=limit_c,
        unit="C",
        consecutive_cycles=consecutive_cycles,
        warn_ratio=thresholds.temperature_warn_ratio,
        required_consecutive_cycles=thresholds.limit_proximity_sustained_cycles,
    )
