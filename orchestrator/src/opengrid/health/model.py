"""Pure data shapes for the health evaluator (02b S6.4). No I/O -- see `health.queries` for that."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from opengrid.platform.config import Config

ProcessStatus = Literal["ok", "down"]
HubHealthState = Literal["online", "stale", "offline", "fault"]
AlertSeverity = Literal["warning", "critical"]

#: 02b S6.4 names 7 processes including "sim", but `ogsim` (the integration simulators) is an external
#: system that shares no code with `opengrid` (BUILD.md S1) and never writes an `og.heartbeat` row --
#: expecting one made `ALR-PROCESS-DOWN:sim` permanently open (defect fix). `ogsim`'s liveness is instead
#: inferred from its own MQTT-driven writes (`ALR-SIM-OFFLINE`, see `evaluate_sim_offline_alert`), so only
#: the 6 `opengrid` processes that actually call `opengrid.platform.heartbeat.write_heartbeat` are listed.
ALL_PROCESSES: tuple[str, ...] = ("feeds", "engine", "guardian", "safestop", "settle", "api")

DegradedMode = Literal[
    "NO_NEW_COMMITMENTS",  # 02b S6.5 row 1: a feed crosses STALE
    "HOLD_LOCAL_AUTONOMY",  # 02b S6.5 row 2: engine down
    "HOLD",  # 02b S6.5 row 3: guardian down / verdict timeout
    "DIST_DEFERRAL_OPEN_LOOP",  # 02b S6.5 row 5: sim SCADA silent for a bank
]

#: `og.alert` has no owner/source column (`migrations/0001_init.sql`), so this code-level allow-list is
#: the fix for a live defect: `evaluate_alerts()`'s clear pass used to close ANY open alert whose
#: rule+scope didn't match one of ITS OWN this-cycle findings -- which also closed alerts other modules
#: raise and own the lifecycle of (`ALR-SETTLE-STALLED` from settle, `ALR-SELECTOR-GATE-FAILED` from
#: selector, `ALR-ENERGY-SHORTFALL-RISK` raised directly by the allocator/engine hook, see
#: `evaluate_energy_shortfall_risk_alert`'s docstring) within one ~5s cycle of them being raised. Only a
#: rule in this set -- exactly the `ALR-*` rules `opengrid.health.evaluate_alerts` itself evaluates every
#: cycle -- may be auto-cleared here; every other module clears its own alerts. In particular,
#: `ALR-SCOPE-CONSERVATIVE`, `ALR-SAFE-STOP-REQUESTED` and `ALR-CLOCK-QUALITY` are guardian's (it raises
#: and clears them itself, R2 coordination note) and must stay OUT of this set.
HEALTH_OWNED_ALERT_RULES: frozenset[str] = frozenset(
    {
        "ALR-FEED-STALE",
        "ALR-FEED-LGV-EXHAUSTED",
        "ALR-PROCESS-DOWN",
        "ALR-HUB-OFFLINE-RATIO",
        "ALR-CYCLE-P99",
        "ALR-CYCLE-P99-APPROACHING",
        "ALR-GUARDIAN-TIMEOUT-RATE",
        "ALR-RESERVE-BREACH",
        "ALR-SCADA-OVERLOAD",
        "ALR-SIM-OFFLINE",
        # Prepared, not yet raised anywhere (R2 item 3: FLEET-SIM hasn't landed the telemetry yet) --
        # listed now so wiring them into evaluate_alerts() later doesn't also require touching this set.
        "ALR-METER-EXPORT-LIMIT",
        "ALR-TEMPERATURE-LIMIT",
        "ALR-SCADA-SILENT",
        "ALR-SCADA-SILENT-BANK",
    }
)


def normalize_feed_key(source: str, product: str) -> str:
    """Canonical `"SOURCE:product"` form for `HealthThresholds.firm_blocking_feeds` membership --
    case-normalised (uppercased) so a config typo like `"ercot:NP6-905-CD"` still matches the runtime
    `FeedStatus.source`/`.product` pair (R3 review fix)."""
    return f"{source.strip()}:{product.strip()}".upper()


#: R3 review fix (HIGH): the AS DAM clearing price (np4-188-cd) also feeds firm pricing -- AS intake
#: values offers at its MCPC, so a stale clearing price must block new commitments exactly like the
#: real-time energy price does. Its own staleness *window* is unaffected (still `AS_PRICE_FRESH_S`,
#: `opengrid.feeds.staleness.threshold_s_for_product`); only its membership in the blocking set changed.
_DEFAULT_FIRM_BLOCKING_FEEDS: frozenset[str] = frozenset(
    {normalize_feed_key("ERCOT", "np6-905-cd"), normalize_feed_key("ERCOT", "np4-188-cd")}
)


def _resolve_firm_blocking_feeds(cfg: Config, defaults: frozenset[str]) -> frozenset[str]:
    """Validates and resolves `[health].firm_blocking_feeds` (R3 review fix):

    - must be a list of `"SOURCE:product"` strings -- a bare string (a config author forgetting the
      list brackets, e.g. `firm_blocking_feeds = "ERCOT:np6-905-cd"`) raises rather than being silently
      iterated character-by-character;
    - every entry is case-normalised (`normalize_feed_key`);
    - configured entries ADD to `defaults` unless `health.firm_blocking_feeds_replace = true`;
    - the resolved set must not be empty unless `health.firm_blocking_feeds_allow_empty = true` -- an
      empty set means NO feed ever blocks `NO_NEW_COMMITMENTS`, which must be an explicit, deliberate
      choice, not an accident (e.g. a typo'd `firm_blocking_feeds_replace = true` with an empty list).
    """
    raw = cfg.get("health.firm_blocking_feeds", None)
    if raw is None:
        return defaults
    if isinstance(raw, str):
        raise ValueError(
            'health.firm_blocking_feeds must be a list of "SOURCE:product" strings, not a bare string '
            f'({raw!r}) -- wrap it in a list, e.g. ["{raw}"]'
        )
    configured_keys: set[str] = set()
    for entry in raw:
        parts = str(entry).split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f'health.firm_blocking_feeds entries must be "SOURCE:product" strings, got {entry!r}'
            )
        configured_keys.add(normalize_feed_key(*parts))
    configured = frozenset(configured_keys)
    replace = bool(cfg.get("health.firm_blocking_feeds_replace", False))
    resolved = configured if replace else (defaults | configured)
    if not resolved and not bool(cfg.get("health.firm_blocking_feeds_allow_empty", False)):
        raise ValueError(
            "health.firm_blocking_feeds resolved to an empty set -- no feed would ever block "
            "NO_NEW_COMMITMENTS. Set health.firm_blocking_feeds_allow_empty = true if this is intended."
        )
    return resolved


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
    # ALR-CYCLE-P99-APPROACHING (pre-limit warning, R2): fires the instant p99 crosses this fraction of
    # `cycle_p99_budget_s`, with no consecutive-cycle requirement -- an early signal before the hard
    # breach (`ALR-CYCLE-P99`, which still needs `cycle_p99_breach_cycles` consecutive breaches).
    cycle_p99_warn_ratio: float = 0.80
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
    # ALR-SIM-OFFLINE (defect fix): `ogsim` writes no heartbeat (see `ALL_PROCESSES`'s docstring), so its
    # liveness is inferred from the freshest of its own MQTT-driven writes -- `og.hub_state.last_seen_at`
    # (`ogsim.fleet`) and `og.feed_obs` SCADA readings (`ogsim.scada`). 60s is 30x `fleet.telemetry_interval_s`
    # (2s) and comfortably past `scada.publish_interval_s` (2s), so a couple of missed publishes never
    # false-positives, but an actually-dead sim process is caught quickly.
    sim_offline_s: float = 60.0
    # ALR-METER-EXPORT-LIMIT / ALR-TEMPERATURE-LIMIT (R2 item 3, prepared ahead of FLEET-SIM landing meter
    # export / temperature telemetry -- not wired into evaluate_alerts() yet, see
    # `rules.evaluate_meter_export_limit_alert`/`evaluate_temperature_limit_alert`'s docstrings). Warning
    # once a reading has stayed at or above this fraction of its limit for
    # `limit_proximity_sustained_cycles` consecutive cycles -- a momentary spike doesn't count.
    meter_export_warn_ratio: float = 0.90
    temperature_warn_ratio: float = 0.90
    limit_proximity_sustained_cycles: int = 3
    # ALR-SCADA-SILENT / DIST_DEFERRAL_OPEN_LOOP (R3, DM-09 / ES07-S02): no SCADA bank reading
    # (`og.feed_obs` source='scada') for longer than this is "SCADA silent" -- SCADA-dependent dispatch
    # loops must HOLD/SCHEDULE until it recovers (07 S6.8). Distinct from (and independently configurable
    # from) `sim_offline_s`, which also folds in fleet telemetry to infer whether `ogsim` itself is alive.
    scada_silent_s: float = 60.0
    # R3 hotfix: `NO_NEW_COMMITMENTS` (02b S6.5 row 1) used to fire on ANY stale `feed_status` row,
    # including ERCOT system-load ACTUALS (np6-345-cd, a daily product), NWS and the EIA fallback --
    # none of those feed firm pricing, so their normal staleness (or, for EIA, being idle while its
    # ERCOT primary is healthy, see `is_fallback_feed_needed`) blocked production commitments for no
    # reason. Only a feed in this set blocks new commitments when stale; every feed still raises its own
    # `ALR-FEED-STALE`/`ALR-FEED-LGV-EXHAUSTED` regardless of membership here -- this only narrows the
    # gate, not the alerting. Keyed as `normalize_feed_key(source, product)` (case-normalised
    # `"SOURCE:PRODUCT"`). Default: the ERCOT real-time price series (np6-905-cd) and the AS DAM clearing
    # price (np4-188-cd, R3 review fix -- AS intake values offers at its MCPC) -- the firm-pricing inputs
    # `forecast.scenarios`/`selector.gate`/AS intake need fresh. Config-driven (see
    # `_resolve_firm_blocking_feeds`) so the architect can extend it as more feeds are confirmed to be
    # genuine firm-pricing inputs.
    firm_blocking_feeds: frozenset[str] = field(default_factory=lambda: _DEFAULT_FIRM_BLOCKING_FEEDS)

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
            sim_offline_s=cfg.get("health.sim_offline_s", defaults.sim_offline_s),
            cycle_p99_warn_ratio=cfg.get("health.cycle_p99_warn_ratio", defaults.cycle_p99_warn_ratio),
            meter_export_warn_ratio=cfg.get(
                "health.meter_export_warn_ratio", defaults.meter_export_warn_ratio
            ),
            temperature_warn_ratio=cfg.get("health.temperature_warn_ratio", defaults.temperature_warn_ratio),
            limit_proximity_sustained_cycles=cfg.get(
                "health.limit_proximity_sustained_cycles", defaults.limit_proximity_sustained_cycles
            ),
            scada_silent_s=cfg.get("health.scada_silent_s", defaults.scada_silent_s),
            firm_blocking_feeds=_resolve_firm_blocking_feeds(cfg, defaults.firm_blocking_feeds),
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
