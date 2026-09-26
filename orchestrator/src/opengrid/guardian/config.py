"""Guardian configuration: the `[guardian]` TOML section plus the thresholds the G-checks need
(02a S6.1's threshold table). No hard-coded numbers in `service.py`/`checks.py` -- BUILD.md S5a.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from opengrid.platform.config import Config

ClockSource = Literal["kernel", "chrony"]
CalibrationSyncSource = Literal["ptp", "gps", "ntp_disciplined"]

#: G-02: PER-UNIT inverter limit (one Base Power unit). It binds times a home's unit count only when that
#: count is known (HubParams.units); the per-home cap is always the home's own rating (HubParams.p_kw).
DEFAULT_INVERTER_CAP_KW = 11.0
DEFAULT_BANK_LOADING_PCT = 0.95
DEFAULT_RESERVE_MARGIN_PCT = 0.01
DEFAULT_DISCRETIONARY_RAMP_CAP_KW_PER_MIN = 50_000.0
DEFAULT_NON_FIRM_RAMP_CAP_KW_PER_MIN = 10_000.0
DEFAULT_FEEDER_RAMP_CEILING_KW_PER_MIN = 3_000.0
DEFAULT_CLOCK_OFFSET_MAX_MS = 200.0
DEFAULT_VERDICT_TIMEOUT_MS = 300.0
DEFAULT_AS_RELEASE_ENABLED = False
#: K12: where G-20 reads clock quality. "kernel" (adjtimex) works under any NTP daemon (the live host
#: runs ntpd, not chrony); "chrony" parses `chronyc tracking`.
DEFAULT_CLOCK_SOURCE: ClockSource = "kernel"
#: K12: a clock reading is reused for this long, so a 50-batch tick costs one read, not fifty.
DEFAULT_CLOCK_CACHE_S = 1.0
#: K4/G-03: a SCADA bank-load reading older than this is unknown loading (VETO), never 0 kVA.
DEFAULT_BANK_LOAD_MAX_AGE_S = 30.0
#: K1/G-01: a hub whose telemetry the guardian has not received for this long is stale (zero discharge).
DEFAULT_TELEMETRY_MAX_AGE_S = 60.0
#: S6.7/G-25: calibration command lease the guardian issues, the longest it will sign, the future-dated
#: skew it tolerates, and how old a PENDING `og.calibration_attempt` row may be and still be signed.
DEFAULT_CALIBRATION_LEASE_S = 60.0
DEFAULT_CALIBRATION_MAX_LEASE_S = 300.0
DEFAULT_CALIBRATION_MAX_ISSUE_SKEW_S = 5.0
DEFAULT_CALIBRATION_MAX_REQUEST_AGE_S = 600.0
DEFAULT_CALIBRATION_SYNC_SOURCE: CalibrationSyncSource = "ntp_disciplined"
#: G-25 fleet caps on autonomous recalibration: at most this % of the fleet signed per window, at most
#: this many awaiting their ack, and a hold (plus ALR-CALIBRATION-BUDGET) when more than this % of the fleet
#: is flagged for calibration in the window (a fleet event, not per-inverter drift).
DEFAULT_CALIBRATION_BUDGET_PCT = 2.0
DEFAULT_CALIBRATION_BUDGET_WINDOW_S = 3600.0
DEFAULT_CALIBRATION_MAX_CONCURRENT = 10
DEFAULT_CALIBRATION_SYSTEMIC_DRIFT_PCT = 5.0
#: K8: a Tier-2 release approval older than this is never acted on; a signed RELEASE not yet published
#: by og-safestop is re-handed to it for the same window.
DEFAULT_STOP_RELEASE_MAX_AGE_S = 300.0
#: K8: how far in the future an approval timestamp may be (og-api and og-guardian share the host clock).
DEFAULT_STOP_RELEASE_MAX_CLOCK_SKEW_S = 5.0


@dataclass(frozen=True, slots=True)
class GuardianConfig:
    key_path: str
    clock_offset_max_ms: float = DEFAULT_CLOCK_OFFSET_MAX_MS
    verdict_timeout_ms: float = DEFAULT_VERDICT_TIMEOUT_MS
    inverter_cap_kw: float = DEFAULT_INVERTER_CAP_KW
    bank_loading_pct: float = DEFAULT_BANK_LOADING_PCT
    reserve_margin_pct: float = DEFAULT_RESERVE_MARGIN_PCT
    discretionary_ramp_cap_kw_per_min: float = DEFAULT_DISCRETIONARY_RAMP_CAP_KW_PER_MIN
    non_firm_ramp_cap_kw_per_min: float = DEFAULT_NON_FIRM_RAMP_CAP_KW_PER_MIN
    default_feeder_ramp_ceiling_kw_per_min: float = DEFAULT_FEEDER_RAMP_CEILING_KW_PER_MIN
    as_release_enabled: bool = DEFAULT_AS_RELEASE_ENABLED
    cycle_interval_s: float = 2.0
    key_id: str = "guardian-2026a"
    signing_seed_env: str = "GUARDIAN_SIGNING_SEED"
    feeder_ramp_ceiling_kw_per_min: dict[str, float] = field(default_factory=dict)
    clock_source: ClockSource = DEFAULT_CLOCK_SOURCE
    clock_cache_s: float = DEFAULT_CLOCK_CACHE_S
    bank_load_max_age_s: float = DEFAULT_BANK_LOAD_MAX_AGE_S
    telemetry_max_age_s: float = DEFAULT_TELEMETRY_MAX_AGE_S
    calibration_lease_s: float = DEFAULT_CALIBRATION_LEASE_S
    calibration_max_lease_s: float = DEFAULT_CALIBRATION_MAX_LEASE_S
    calibration_max_issue_skew_s: float = DEFAULT_CALIBRATION_MAX_ISSUE_SKEW_S
    calibration_max_request_age_s: float = DEFAULT_CALIBRATION_MAX_REQUEST_AGE_S
    calibration_sync_source: CalibrationSyncSource = DEFAULT_CALIBRATION_SYNC_SOURCE
    calibration_budget_pct: float = DEFAULT_CALIBRATION_BUDGET_PCT
    calibration_budget_window_s: float = DEFAULT_CALIBRATION_BUDGET_WINDOW_S
    calibration_max_concurrent: int = DEFAULT_CALIBRATION_MAX_CONCURRENT
    calibration_systemic_drift_pct: float = DEFAULT_CALIBRATION_SYSTEMIC_DRIFT_PCT
    #: ES06-S04: veto ratio above which a scope goes CONSERVATIVE, consecutive CONSERVATIVE ticks before a
    #: safe stop is requested of a person, and idle ticks after which a CONSERVATIVE scope clears.
    escalation_conservative_ratio: float = 0.05
    escalation_stop_request_after: int = 3
    escalation_idle_clear_ticks: int = 30
    #: K7 hysteresis: consecutive good ticks (veto ratio <= ratio x clear factor) before a CONSERVATIVE
    #: scope clears (`opengrid.guardian.escalation`).
    escalation_clear_after_good_ticks: int = 3
    escalation_clear_ratio_factor: float = 0.5
    #: K8: operators allowed to request/approve a stop RELEASE. Empty = no release is ever signed.
    stop_release_authorised_operators: frozenset[str] = frozenset()
    stop_release_max_age_s: float = DEFAULT_STOP_RELEASE_MAX_AGE_S
    stop_release_max_clock_skew_s: float = DEFAULT_STOP_RELEASE_MAX_CLOCK_SKEW_S
    #: 09 S2.6 flow limits. `flow_telemetry_required` False: a hub that has NEVER reported a flow field
    #: (meter, cell temperature, BMS limits, peak budget) is checked against the static limits below; a
    #: reported value that goes stale always fails closed. True (go-live): never-reported is stale too.
    flow_telemetry_required: bool = False
    flow_max_age_s: float = DEFAULT_BANK_LOAD_MAX_AGE_S
    unknown_temp_factor: float = 0.5  # G-02 f_T for an unknown/stale cell temperature (0 = strict)
    load_drop_kw: float = 0.5  # G-26 Delta L over the lease
    #: G-26 static premise defaults while og.hub carries none (migration 0029 columns NULL). None = unknown.
    default_export_limit_kw: float | None = 20.0
    default_service_kw: float = 48.0  # 200 A at 240 V
    default_pv_rated_kw: float = 0.0
    xfmr_forward_pct: float = 1.0  # G-27 rho_xf
    xfmr_reverse_pct: float = 1.0  # G-27 rho_rev
    xfmr_max_stale_fraction: float = 0.2  # G-27: above this, any increase of |F| is vetoed
    unmapped_xfmr_kva_per_home: float = 5.0  # G-27 S_def for a hub with no transformer mapping
    feeder_thermal_pct: float = 0.95  # G-28 rho_th
    #: G-28 static feeder limits while no feeder row exists (None = unknown: any increase vetoed).
    default_feeder_thermal_kw: float | None = 10_000.0
    default_feeder_reverse_kw: float | None = 3_000.0
    substation_pct: float = 0.95  # G-29 rho


def _clock_source(value: object) -> ClockSource:
    if value == "kernel" or value == "chrony":
        return value
    msg = f"guardian.clock_source must be 'kernel' or 'chrony', got {value!r}"
    raise ValueError(msg)


def _sync_source(value: object) -> CalibrationSyncSource:
    if value == "ptp" or value == "gps" or value == "ntp_disciplined":
        return value
    msg = f"guardian.calibration_sync_source must be 'ptp', 'gps' or 'ntp_disciplined', got {value!r}"
    raise ValueError(msg)


def load_guardian_config(cfg: Config) -> GuardianConfig:
    """Build `GuardianConfig` from the loaded TOML `Config` (02b S1.4 `[guardian]`, `[allocator]`)."""
    return GuardianConfig(
        key_path=str(cfg.get("guardian.key_path", "/etc/opengrid/guardian_ed25519.key")),
        clock_offset_max_ms=float(cfg.get("guardian.clock_offset_max_ms", DEFAULT_CLOCK_OFFSET_MAX_MS)),
        verdict_timeout_ms=float(cfg.get("guardian.verdict_timeout_ms", DEFAULT_VERDICT_TIMEOUT_MS)),
        inverter_cap_kw=float(cfg.get("guardian.inverter_cap_kw", DEFAULT_INVERTER_CAP_KW)),
        bank_loading_pct=float(cfg.get("guardian.bank_loading_pct", DEFAULT_BANK_LOADING_PCT)),
        reserve_margin_pct=float(cfg.get("guardian.reserve_margin_pct", DEFAULT_RESERVE_MARGIN_PCT)),
        discretionary_ramp_cap_kw_per_min=float(
            cfg.get("guardian.discretionary_ramp_cap_kw_per_min", DEFAULT_DISCRETIONARY_RAMP_CAP_KW_PER_MIN)
        ),
        non_firm_ramp_cap_kw_per_min=float(
            cfg.get("guardian.non_firm_ramp_cap_kw_per_min", DEFAULT_NON_FIRM_RAMP_CAP_KW_PER_MIN)
        ),
        default_feeder_ramp_ceiling_kw_per_min=float(
            cfg.get("guardian.feeder_ramp_ceiling_kw_per_min", DEFAULT_FEEDER_RAMP_CEILING_KW_PER_MIN)
        ),
        as_release_enabled=bool(cfg.get("guardian.as_release_enabled", DEFAULT_AS_RELEASE_ENABLED)),
        cycle_interval_s=float(cfg.get("allocator.cycle_interval_s", 2.0)),
        key_id=str(cfg.get("guardian.key_id", "guardian-2026a")),
        signing_seed_env=str(cfg.get("guardian.signing_seed_env", "GUARDIAN_SIGNING_SEED")),
        feeder_ramp_ceiling_kw_per_min=dict(cfg.get("guardian.feeder_ramp_ceiling_kw_per_min_by_feeder", {})),
        clock_source=_clock_source(cfg.get("guardian.clock_source", DEFAULT_CLOCK_SOURCE)),
        clock_cache_s=float(cfg.get("guardian.clock_cache_s", DEFAULT_CLOCK_CACHE_S)),
        bank_load_max_age_s=float(cfg.get("guardian.bank_load_max_age_s", DEFAULT_BANK_LOAD_MAX_AGE_S)),
        telemetry_max_age_s=float(cfg.get("guardian.telemetry_max_age_s", DEFAULT_TELEMETRY_MAX_AGE_S)),
        calibration_lease_s=float(cfg.get("guardian.calibration_lease_s", DEFAULT_CALIBRATION_LEASE_S)),
        calibration_max_lease_s=float(
            cfg.get("guardian.calibration_max_lease_s", DEFAULT_CALIBRATION_MAX_LEASE_S)
        ),
        calibration_max_issue_skew_s=float(
            cfg.get("guardian.calibration_max_issue_skew_s", DEFAULT_CALIBRATION_MAX_ISSUE_SKEW_S)
        ),
        calibration_max_request_age_s=float(
            cfg.get("guardian.calibration_max_request_age_s", DEFAULT_CALIBRATION_MAX_REQUEST_AGE_S)
        ),
        calibration_sync_source=_sync_source(
            cfg.get("guardian.calibration_sync_source", DEFAULT_CALIBRATION_SYNC_SOURCE)
        ),
        calibration_budget_pct=float(
            cfg.get("guardian.calibration_budget_pct", DEFAULT_CALIBRATION_BUDGET_PCT)
        ),
        calibration_budget_window_s=float(
            cfg.get("guardian.calibration_budget_window_s", DEFAULT_CALIBRATION_BUDGET_WINDOW_S)
        ),
        calibration_max_concurrent=int(
            cfg.get("guardian.calibration_max_concurrent", DEFAULT_CALIBRATION_MAX_CONCURRENT)
        ),
        calibration_systemic_drift_pct=float(
            cfg.get("guardian.calibration_systemic_drift_pct", DEFAULT_CALIBRATION_SYSTEMIC_DRIFT_PCT)
        ),
        escalation_conservative_ratio=float(cfg.get("guardian.escalation_conservative_ratio", 0.05)),
        escalation_stop_request_after=int(cfg.get("guardian.escalation_stop_request_after", 3)),
        escalation_idle_clear_ticks=int(cfg.get("guardian.escalation_idle_clear_ticks", 30)),
        escalation_clear_after_good_ticks=int(cfg.get("guardian.escalation_clear_after_good_ticks", 3)),
        escalation_clear_ratio_factor=float(cfg.get("guardian.escalation_clear_ratio_factor", 0.5)),
        stop_release_authorised_operators=frozenset(
            str(op) for op in cfg.get("guardian.stop_release_authorised_operators", []) or []
        ),
        stop_release_max_age_s=float(
            cfg.get("guardian.stop_release_max_age_s", DEFAULT_STOP_RELEASE_MAX_AGE_S)
        ),
        stop_release_max_clock_skew_s=float(
            cfg.get("guardian.stop_release_max_clock_skew_s", DEFAULT_STOP_RELEASE_MAX_CLOCK_SKEW_S)
        ),
        flow_telemetry_required=bool(cfg.get("guardian.flow.telemetry_required", False)),
        flow_max_age_s=float(cfg.get("guardian.flow.max_age_s", DEFAULT_BANK_LOAD_MAX_AGE_S)),
        unknown_temp_factor=float(cfg.get("guardian.flow.unknown_temp_factor", 0.5)),
        load_drop_kw=float(cfg.get("guardian.flow.load_drop_kw", 0.5)),
        default_export_limit_kw=_optional_float(cfg.get("guardian.flow.default_export_limit_kw", 20.0)),
        default_service_kw=float(cfg.get("guardian.flow.default_service_kw", 48.0)),
        default_pv_rated_kw=float(cfg.get("guardian.flow.default_pv_rated_kw", 0.0)),
        xfmr_forward_pct=float(cfg.get("guardian.flow.xfmr_forward_pct", 1.0)),
        xfmr_reverse_pct=float(cfg.get("guardian.flow.xfmr_reverse_pct", 1.0)),
        xfmr_max_stale_fraction=float(cfg.get("guardian.flow.xfmr_max_stale_fraction", 0.2)),
        unmapped_xfmr_kva_per_home=float(cfg.get("guardian.flow.unmapped_xfmr_kva_per_home", 5.0)),
        feeder_thermal_pct=float(cfg.get("guardian.flow.feeder_thermal_pct", 0.95)),
        default_feeder_thermal_kw=_optional_float(
            cfg.get("guardian.flow.default_feeder_thermal_kw", 10_000.0)
        ),
        default_feeder_reverse_kw=_optional_float(
            cfg.get("guardian.flow.default_feeder_reverse_kw", 3_000.0)
        ),
        substation_pct=float(cfg.get("guardian.flow.substation_pct", 0.95)),
    )


def _optional_float(value: object) -> float | None:
    """A TOML value where the string "unknown" (TOML has no null) means unknown."""
    if value is None or value == "unknown":
        return None
    return float(str(value))
