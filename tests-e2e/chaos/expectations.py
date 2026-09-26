"""Expected behaviour when each process is killed, as data, plus the pure evaluator (WORKBOARD Q3, TS-C).

Sources for every number and mode (cited per row in `source`):
- `orchestrator/src/opengrid/health/rules.py` `classify_process_status` / `derive_degraded_modes`
- `orchestrator/src/opengrid/health/model.py` `ALL_PROCESSES`, `DegradedMode`
- `orchestrator/config/orchestrator.toml` `[health] heartbeat_interval_s=5, heartbeat_miss_threshold=3`,
  `[fleet] lease_ttl_s=30`, `[feeds.staleness] ercot_price_fresh_s=600`
- `orchestrator/src/opengrid/safestop/README.md` K8, `deploy/systemd/og-safestop.service` (no After=/Requires=
  on engine/guardian), `deploy/README.md` (Restart=always, RestartSec=2)
- `integration-sims/config/fleet.yaml` `lease_hold_after_expiry_s=5`

No I/O here: `evaluate()` takes an observation timeline the runner recorded and returns findings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ALL_PROCESSES: tuple[str, ...] = ("feeds", "engine", "guardian", "safestop", "sim", "settle", "api")
INFRA: tuple[str, ...] = ("postgres", "mosquitto")

HEARTBEAT_INTERVAL_S = 5.0  # [health] heartbeat_interval_s
HEARTBEAT_MISS_THRESHOLD = 3  # [health] heartbeat_miss_threshold
HEARTBEAT_DOWN_AFTER_S = HEARTBEAT_INTERVAL_S * HEARTBEAT_MISS_THRESHOLD  # rules.classify_process_status
LEASE_TTL_S = 30.0  # [fleet] lease_ttl_s: hubs hold the last setpoint this long, then go local
LEASE_HOLD_AFTER_EXPIRY_S = 5.0  # fleet.yaml lease_hold_after_expiry_s
EVALUATOR_SLACK_S = 15.0  # one evaluator cycle + one heartbeat interval + one health poll
DOWN_WITHIN_S = HEARTBEAT_DOWN_AFTER_S + EVALUATOR_SLACK_S  # 30 s
RECOVERY_WITHIN_S = 30.0  # RestartSec=2 + start-up + first heartbeat + evaluator cycle
INFRA_RECOVERY_WITHIN_S = 60.0  # every process reconnects on its own (K7: degrade, don't trip)
UNREACHABLE_WITHIN_S = 5.0  # a killed og-api refuses connections at once
HUB_OFFLINE_S = 30.0  # [health] hub_offline_s: no MQTT telemetry -> every hub offline
FEED_STALE_AFTER_S = 600.0  # [feeds.staleness] ercot_price_fresh_s -> NO_NEW_COMMITMENTS (beyond the run)
ZERO_COUNTERS: tuple[str, ...] = ("reserve_breaches", "double_sold_kwh")  # A10, K1/K2

Phase = Literal["before", "outage", "recovery"]


@dataclass(frozen=True, slots=True)
class Expectation:
    process: str
    unit: str  # systemd unit name; also the compose service name
    source: str
    notes: str
    infra: bool = False
    health_key: str | None = None  # `processes[<key>]` that must read `down`; None = not asserted
    down_within_s: float | None = None
    degraded_modes: frozenset[str] = frozenset()  # must appear while down
    eventual_modes: frozenset[str] = frozenset()  # expected, but after the run's window; documented only
    must_stay_ok: frozenset[str] = frozenset()  # status ok AND heartbeat ts advancing during the outage
    alert_rules: frozenset[str] = frozenset()  # open alerts that must appear during the outage
    health_unreachable: bool = False  # GET /og/api/health must fail during the outage
    health_frozen: bool = False  # alerts / hub counts must stop changing during the outage
    outage_observe_s: float = (
        0.0  # keep the process dead at least this long (slow effects, e.g. hubs offline)
    )
    recovery_within_s: float = RECOVERY_WITHIN_S
    zero_counters: tuple[str, ...] = ZERO_COUNTERS


def _others(*excluded: str) -> frozenset[str]:
    return frozenset(p for p in ALL_PROCESSES if p not in excluded)


EXPECTATIONS: tuple[Expectation, ...] = (
    Expectation(
        process="feeds",
        unit="og-feeds",
        health_key="feeds",
        down_within_s=DOWN_WITHIN_S,
        eventual_modes=frozenset({"NO_NEW_COMMITMENTS"}),
        must_stay_ok=_others("feeds", "sim"),
        alert_rules=frozenset({"ALR-PROCESS-DOWN"}),
        source="health/rules.py derive_degraded_modes(feed_stale); [feeds.staleness] ercot_price_fresh_s=600",
        notes=f"Engine keeps allocating on last-good values; NO_NEW_COMMITMENTS only once a feed crosses STALE "
        f"({FEED_STALE_AFTER_S:.0f} s), which is outside this run's window.",
    ),
    Expectation(
        process="engine",
        unit="og-engine",
        health_key="engine",
        down_within_s=DOWN_WITHIN_S,
        degraded_modes=frozenset({"HOLD_LOCAL_AUTONOMY"}),
        must_stay_ok=_others("engine", "sim"),
        alert_rules=frozenset({"ALR-PROCESS-DOWN"}),
        source="health/rules.py derive_degraded_modes(engine down); [fleet] lease_ttl_s=30; K8 safestop/README.md",
        notes=f"Hubs hold the last setpoint until lease expiry ({LEASE_TTL_S:.0f} s + "
        f"{LEASE_HOLD_AFTER_EXPIRY_S:.0f} s), then local autonomy. safestop must stay ok (K8).",
    ),
    Expectation(
        process="guardian",
        unit="og-guardian",
        health_key="guardian",
        down_within_s=DOWN_WITHIN_S,
        degraded_modes=frozenset({"HOLD"}),
        must_stay_ok=_others("guardian", "sim"),
        alert_rules=frozenset({"ALR-PROCESS-DOWN"}),
        source="health/rules.py derive_degraded_modes(guardian down); engine/README.md step 5 (cycle holds)",
        notes="Engine keeps ticking but proposes no new command batches; no unsigned command may reach a hub (K3).",
    ),
    Expectation(
        process="safestop",
        unit="og-safestop",
        health_key="safestop",
        down_within_s=DOWN_WITHIN_S,
        must_stay_ok=_others("safestop", "sim"),
        alert_rules=frozenset({"ALR-PROCESS-DOWN"}),
        source="health/rules.py (no degraded mode for safestop); deploy/systemd/og-safestop.service Restart=always",
        notes="Normal operation continues; the stop authority is simply unavailable until systemd restarts it.",
    ),
    Expectation(
        process="sim",
        unit="og-sim-fleet",
        health_key="sim",
        down_within_s=DOWN_WITHIN_S,
        must_stay_ok=_others("sim"),
        alert_rules=frozenset({"ALR-PROCESS-DOWN", "ALR-HUB-OFFLINE-RATIO"}),
        outage_observe_s=HUB_OFFLINE_S + EVALUATOR_SLACK_S,
        recovery_within_s=RECOVERY_WITHIN_S + HUB_OFFLINE_S,
        source="health/rules.py classify_hub_health (stale>6 s, offline>30 s), evaluate_hub_offline_ratio_alert",
        notes="No telemetry: every hub goes stale then offline, ALR-HUB-OFFLINE-RATIO critical; orchestrator "
        "processes all stay ok. Recovery includes hubs coming back online.",
    ),
    Expectation(
        process="settle",
        unit="og-settle",
        health_key="settle",
        health_frozen=True,
        must_stay_ok=_others("settle", "sim"),
        source="health/README.md (evaluator runs inside og-settle); settle/main.py writes the heartbeat",
        notes="The health evaluator lives in og-settle, so nothing re-evaluates: alerts and hub health counts "
        "freeze and no process can be marked down until settle is back. Recovery = settle's heartbeat "
        "ts advancing again.",
    ),
    Expectation(
        process="api",
        unit="og-api",
        health_key="api",
        down_within_s=UNREACHABLE_WITHIN_S,
        health_unreachable=True,
        must_stay_ok=_others("api", "sim"),
        source="api/README.md (health is served by og-api); engine has no dependency on api",
        notes="UI and /og/api/health unreachable; the engine keeps ticking (its heartbeat ts must have advanced "
        "when the api is back).",
    ),
    Expectation(
        process="postgres",
        unit="postgresql",
        infra=True,
        health_unreachable=True,
        down_within_s=UNREACHABLE_WITHIN_S,
        recovery_within_s=INFRA_RECOVERY_WITHIN_S,
        source="platform/heartbeat.py (never raises), platform/process.py run_forever (K7: degrade, don't trip)",
        notes="Every process loses its pool; health cannot be served. Nothing may crash-loop: all 7 processes must "
        "read ok again without manual intervention. Hubs keep their last lease (K8/K7).",
    ),
    Expectation(
        process="mosquitto",
        unit="mosquitto",
        infra=True,
        must_stay_ok=_others("sim"),
        alert_rules=frozenset({"ALR-HUB-OFFLINE-RATIO"}),
        outage_observe_s=HUB_OFFLINE_S + EVALUATOR_SLACK_S,
        recovery_within_s=INFRA_RECOVERY_WITHIN_S,
        source="health/rules.py classify_hub_health; platform/mqtt.py reconnect; [fleet] lease_ttl_s=30",
        notes="Telemetry and commands stop; hubs go offline in health after 30 s and hold/then local autonomy "
        "after lease expiry. Heartbeats go through Postgres so every process stays ok.",
    ),
)

BY_PROCESS: dict[str, Expectation] = {e.process: e for e in EXPECTATIONS}


@dataclass(frozen=True, slots=True)
class Observation:
    """One `GET /og/api/health` result. `t_s` is seconds since the phase started (the kill, or the restart)."""

    phase: Phase
    t_s: float
    health: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    passed: bool
    detail: str


# --- payload accessors ------------------------------------------------------------------------------------


def _status(h: dict[str, Any], process: str) -> str | None:
    entry = h.get("processes", {}).get(process)
    return entry.get("status") if isinstance(entry, dict) else None


def _ts(h: dict[str, Any], process: str) -> datetime | None:
    entry = h.get("processes", {}).get(process)
    raw = entry.get("ts") if isinstance(entry, dict) else None
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _modes(h: dict[str, Any]) -> set[str] | None:
    raw = h.get("degraded_modes")
    return set(raw) if isinstance(raw, list) else None


def _alert_rules(h: dict[str, Any]) -> set[str]:
    return {str(a["rule"]) for a in h.get("alerts", []) if isinstance(a, dict) and a.get("rule")}


def _reachable(obs: Sequence[Observation], phase: Phase) -> list[Observation]:
    return [o for o in obs if o.phase == phase and o.health is not None]


# --- checks -----------------------------------------------------------------------------------------------


def _check_down(exp: Expectation, outage: Sequence[Observation]) -> Finding | None:
    if exp.down_within_s is None:
        return None
    if exp.health_unreachable:
        hit = next((o for o in outage if o.error is not None), None)
        what = "health unreachable"
    else:
        hit = next(
            (o for o in outage if o.health and _status(o.health, exp.health_key or "") == "down"), None
        )
        what = f"processes.{exp.health_key}.status == down"
    if hit is None:
        return Finding(what, False, f"never observed during {len(outage)} outage polls")
    return Finding(
        what, hit.t_s <= exp.down_within_s, f"at t={hit.t_s:.0f}s (limit {exp.down_within_s:.0f}s)"
    )


def _check_modes(exp: Expectation, reachable: Sequence[Observation]) -> Finding | None:
    if not exp.degraded_modes:
        return None
    name = f"degraded_modes contains {sorted(exp.degraded_modes)}"
    if not reachable:
        return Finding(name, False, "no reachable outage observation")
    modes = _modes(reachable[-1].health or {})
    if modes is None:
        return Finding(name, False, "`degraded_modes` missing from the health payload")
    return Finding(name, exp.degraded_modes <= modes, f"observed {sorted(modes)} at end of outage")


def _check_stay_ok(
    exp: Expectation,
    before: Observation | None,
    reachable: Sequence[Observation],
    recovery: Sequence[Observation],
) -> list[Finding]:
    """`must_stay_ok` processes read ok in every reachable outage observation and their heartbeat `ts`
    advanced since `before`. When health itself is unreachable during the outage (api, postgres), the
    first reachable recovery observation is the evidence: the engine must have kept ticking meanwhile."""
    if not exp.must_stay_ok:
        return []
    window = reachable if not exp.health_unreachable else [o for o in recovery if o.health is not None][:1]
    where = "outage" if not exp.health_unreachable else "first reachable recovery observation"
    not_ok = sorted({p for o in window for p in exp.must_stay_ok if _status(o.health or {}, p) != "ok"})
    findings = [
        Finding(
            f"{sorted(exp.must_stay_ok)} stay ok ({where})",
            not not_ok and bool(window),
            f"not ok: {not_ok}" if not_ok else ("no reachable observation" if not window else "all ok"),
        )
    ]
    if before is None or before.health is None or not window:
        return findings
    last = window[-1].health or {}
    stuck = sorted(
        p
        for p in exp.must_stay_ok
        if (a := _ts(before.health, p)) is not None and (b := _ts(last, p)) is not None and b <= a
    )
    findings.append(
        Finding(
            "independent processes kept heartbeating",
            not stuck,
            f"ts not advancing: {stuck}" if stuck else "ok",
        )
    )
    return findings


def _check_counters(exp: Expectation, reachable: Sequence[Observation]) -> Finding:
    bad = sorted(
        {
            f"{c}={o.health.get(c)!r}"
            for o in reachable
            if o.health
            for c in exp.zero_counters
            if o.health.get(c) != 0
        }
    )
    return Finding(f"{list(exp.zero_counters)} stay 0", not bad, ", ".join(bad) if bad else "all 0")


def _check_alerts(exp: Expectation, reachable: Sequence[Observation]) -> Finding | None:
    if not exp.alert_rules:
        return None
    seen = {r for o in reachable for r in _alert_rules(o.health or {})}
    missing = sorted(exp.alert_rules - seen)
    return Finding(
        f"alerts {sorted(exp.alert_rules)} raised", not missing, f"missing {missing}" if missing else "seen"
    )


def _frozen_view(h: dict[str, Any]) -> tuple[Any, Any]:
    alerts = h.get("alerts", [])
    keyed = sorted(str(a.get("rule", "")) + str(a.get("summary", "")) for a in alerts if isinstance(a, dict))
    return keyed, sorted((h.get("hub_health_counts") or {}).items())


def _check_frozen(exp: Expectation, reachable: Sequence[Observation]) -> Finding | None:
    if not exp.health_frozen:
        return None
    if len(reachable) < 2:
        return Finding("health evaluation frozen", False, "need >= 2 reachable outage observations")
    first = _frozen_view(reachable[0].health or {})
    changed = [o.t_s for o in reachable[1:] if _frozen_view(o.health or {}) != first]
    return Finding(
        "health evaluation frozen (alerts, hub counts unchanged)",
        not changed,
        f"changed at t={changed}" if changed else "unchanged across outage",
    )


def _recovered(exp: Expectation, o: Observation, frozen_ts: datetime | None) -> bool:
    if o.health is None:
        return False
    if exp.infra:
        return all(_status(o.health, p) == "ok" for p in ALL_PROCESSES)
    if exp.health_key and _status(o.health, exp.health_key) != "ok":
        return False
    if exp.health_frozen and exp.health_key:
        ts = _ts(o.health, exp.health_key)
        return ts is not None and (frozen_ts is None or ts > frozen_ts)
    modes = _modes(o.health)
    return not (exp.degraded_modes and modes is not None and exp.degraded_modes & modes)


def _check_recovery(
    exp: Expectation, outage: Sequence[Observation], recovery: Sequence[Observation]
) -> Finding:
    last_outage = next((o for o in reversed(outage) if o.health), None)
    frozen_ts = (
        _ts(last_outage.health, exp.health_key)
        if last_outage and last_outage.health and exp.health_key
        else None
    )
    hit = next((o for o in recovery if _recovered(exp, o, frozen_ts)), None)
    if hit is None:
        return Finding("recovery", False, f"not recovered within {len(recovery)} polls")
    return Finding(
        "recovery",
        hit.t_s <= exp.recovery_within_s,
        f"at t={hit.t_s:.0f}s (limit {exp.recovery_within_s:.0f}s)",
    )


def evaluate(timeline: Sequence[Observation], exp: Expectation) -> list[Finding]:
    """Compare a recorded kill/restart timeline against `exp`. Every finding names what was checked so the
    report is self-explanatory; one failed finding fails the row."""
    before = next((o for o in timeline if o.phase == "before"), None)
    outage = [o for o in timeline if o.phase == "outage"]
    reachable = _reachable(timeline, "outage")
    recovery = [o for o in timeline if o.phase == "recovery"]
    candidates: list[Finding | None] = [
        _check_down(exp, outage),
        _check_modes(exp, reachable),
        *_check_stay_ok(exp, before, reachable, recovery),
        _check_alerts(exp, reachable),
        _check_frozen(exp, reachable),
        _check_counters(
            exp, [*_reachable(timeline, "before"), *reachable, *_reachable(timeline, "recovery")]
        ),
        _check_recovery(exp, outage, recovery),
    ]
    if exp.health_unreachable and not exp.infra:
        leaked = [o.t_s for o in reachable if o.t_s > (exp.down_within_s or 0.0)]
        candidates.append(
            Finding("health stays unreachable during outage", not leaked, f"reachable at {leaked}")
        )
    return [f for f in candidates if f is not None]


def markdown_table(expectations: Sequence[Expectation] = EXPECTATIONS) -> str:
    """The expectations as one Markdown table (used by the README and the report header)."""
    rows = [
        "| process | unit | down within | degraded modes | eventual | must stay ok | alerts | recovery within | notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in expectations:
        down = (
            "health unreachable"
            if e.health_unreachable
            else (
                "frozen (no evaluator)"
                if e.health_frozen
                else (f"{e.down_within_s:.0f} s" if e.down_within_s else "-")
            )
        )
        rows.append(
            f"| {e.process} | {e.unit} | {down} | {', '.join(sorted(e.degraded_modes)) or '-'} | "
            f"{', '.join(sorted(e.eventual_modes)) or '-'} | {', '.join(sorted(e.must_stay_ok)) or '-'} | "
            f"{', '.join(sorted(e.alert_rules)) or '-'} | {e.recovery_within_s:.0f} s | {e.notes} |"
        )
    return "\n".join(rows)


__all__ = [
    "ALL_PROCESSES",
    "BY_PROCESS",
    "EXPECTATIONS",
    "INFRA",
    "Expectation",
    "Finding",
    "Observation",
    "evaluate",
    "markdown_table",
]
