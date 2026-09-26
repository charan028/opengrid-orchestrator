"""Pure data shapes for the health evaluator (02b S6.4). No I/O -- see `health.queries` for that."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from opengrid.platform.config import Config

ProcessStatus = Literal["ok", "down"]
HubHealthState = Literal["online", "stale", "offline", "fault"]
AlertSeverity = Literal["warning", "critical"]

#: 02b S6.4: "every process (all 7: feeds, engine, guardian, safestop, sim, settle, api)".
ALL_PROCESSES: tuple[str, ...] = ("feeds", "engine", "guardian", "safestop", "sim", "settle", "api")

DegradedMode = Literal[
    "NO_NEW_COMMITMENTS",  # 02b S6.5 row 1: a feed crosses STALE
    "HOLD_LOCAL_AUTONOMY",  # 02b S6.5 row 2: engine down
    "HOLD",  # 02b S6.5 row 3: guardian down / verdict timeout
    "DIST_DEFERRAL_OPEN_LOOP",  # 02b S6.5 row 5: sim SCADA silent for a bank
]


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    """02b S6.4 threshold constants, overridable via `[health]`/`[fleet]` config -- BUILD.md S5a: "no
    hard-coded ... thresholds"."""

    heartbeat_interval_s: float = 5.0
    heartbeat_miss_threshold: int = 3
    telemetry_interval_s: float = 2.0
    hub_stale_s: float = 6.0
    hub_offline_s: float = 30.0
    cycle_p99_budget_s: float = 0.5
    cycle_p99_breach_cycles: int = 3
    hub_offline_ratio_warning: float = 0.05
    hub_offline_ratio_critical: float = 0.20
    guardian_timeout_rate_critical: float = 0.01
    feed_lgv_exhausted_margin_s: float = 0.0  # 0 => LGV window fully elapsed
    # ALR-SCADA-OVERLOAD (dispatch-live pass): a bank drawing over its kva_rating -- the anomaly
    # catalogue's "bank_overload" injects exactly this (integration-sims/src/ogsim/control/catalogue.py
    # id="bank_overload", default kva_over_rating_pct=20.0). Warning at rating itself (100%), critical
    # once it matches the catalogue's own default injected severity (120%), so the default injection
    # reliably crosses into critical rather than sitting just under a warning-only threshold.
    bank_kva_overload_warning_pct: float = 1.00
    bank_kva_overload_critical_pct: float = 1.20

    @property
    def heartbeat_down_after_s(self) -> float:
        """A process is DOWN after `heartbeat_miss_threshold` missed `heartbeat_interval_s` beats."""
        return self.heartbeat_interval_s * self.heartbeat_miss_threshold

    @property
    def hub_online_s(self) -> float:
        """02b S6.4: online <= fleet.telemetry_interval_s * 2."""
        return self.telemetry_interval_s * 2

    @classmethod
    def from_config(cls, cfg: Config) -> HealthThresholds:
        defaults = cls()
        return cls(
            heartbeat_interval_s=cfg.get("health.heartbeat_interval_s", defaults.heartbeat_interval_s),
            heartbeat_miss_threshold=cfg.get(
                "health.heartbeat_miss_threshold", defaults.heartbeat_miss_threshold
            ),
            telemetry_interval_s=cfg.get("fleet.telemetry_interval_s", defaults.telemetry_interval_s),
            hub_stale_s=cfg.get("health.hub_stale_s", defaults.hub_stale_s),
            hub_offline_s=cfg.get("health.hub_offline_s", defaults.hub_offline_s),
            cycle_p99_budget_s=cfg.get("health.cycle_p99_budget_s", defaults.cycle_p99_budget_s),
            cycle_p99_breach_cycles=cfg.get(
                "health.cycle_p99_breach_cycles", defaults.cycle_p99_breach_cycles
            ),
            hub_offline_ratio_warning=cfg.get(
                "health.hub_offline_ratio_warning", defaults.hub_offline_ratio_warning
            ),
            hub_offline_ratio_critical=cfg.get(
                "health.hub_offline_ratio_critical", defaults.hub_offline_ratio_critical
            ),
            guardian_timeout_rate_critical=cfg.get(
                "health.guardian_timeout_rate_critical", defaults.guardian_timeout_rate_critical
            ),
            bank_kva_overload_warning_pct=cfg.get(
                "health.bank_kva_overload_warning_pct", defaults.bank_kva_overload_warning_pct
            ),
            bank_kva_overload_critical_pct=cfg.get(
                "health.bank_kva_overload_critical_pct", defaults.bank_kva_overload_critical_pct
            ),
        )


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    process: str
    status: ProcessStatus
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class HubHealthCounts:
    online: int = 0
    stale: int = 0
    offline: int = 0
    fault: int = 0

    @property
    def total(self) -> int:
        return self.online + self.stale + self.offline + self.fault

    @property
    def offline_ratio(self) -> float:
        """Fraction of hubs excluded from `fleet.capability()` (offline or fault) -- 02b S6.4 alert rule."""
        return 0.0 if self.total == 0 else (self.offline + self.fault) / self.total


@dataclass(frozen=True, slots=True)
class AlertFinding:
    """One evaluated alert-rule outcome (02b S6.4 `ALR-*` set). `condition_key` is stable across cycles
    so `evaluate_alerts` can match it against already-open `og.alert` rows to raise-once/clear-on-resolve
    (TS-07-06: no duplicate alert storm while the condition persists)."""

    rule: str
    severity: AlertSeverity
    summary: str
    condition_key: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleLatencySample:
    """p99 (seconds) of `og_control_tick_duration_seconds` over the evaluator's rolling window, and the
    number of consecutive cycles it has been over `cycle_p99_budget_s` (02b S6.4)."""

    p99_s: float | None
    consecutive_breaches: int


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """The read model the API's `GET /og/api/health` exposes (02b S7.1)."""

    evaluated_at: datetime
    processes: tuple[ProcessHealth, ...]
    hub_counts_by_zone: dict[str, HubHealthCounts]
    cycle_latency: CycleLatencySample
    degraded_modes: frozenset[DegradedMode]
    open_alert_count: int

    @property
    def fleet_totals(self) -> HubHealthCounts:
        online = sum(c.online for c in self.hub_counts_by_zone.values())
        stale = sum(c.stale for c in self.hub_counts_by_zone.values())
        offline = sum(c.offline for c in self.hub_counts_by_zone.values())
        fault = sum(c.fault for c in self.hub_counts_by_zone.values())
        return HubHealthCounts(online, stale, offline, fault)
