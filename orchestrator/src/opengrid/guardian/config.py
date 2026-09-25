"""Guardian configuration: the `[guardian]` TOML section plus the thresholds the G-checks need
(02a S6.1's threshold table). No hard-coded numbers in `service.py`/`checks.py` -- BUILD.md S5a.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opengrid.platform.config import Config

DEFAULT_INVERTER_CAP_KW = 11.0
DEFAULT_BANK_LOADING_PCT = 0.95
DEFAULT_RESERVE_MARGIN_PCT = 0.01
DEFAULT_DISCRETIONARY_RAMP_CAP_KW_PER_MIN = 50_000.0
DEFAULT_NON_FIRM_RAMP_CAP_KW_PER_MIN = 10_000.0
DEFAULT_FEEDER_RAMP_CEILING_KW_PER_MIN = 3_000.0
DEFAULT_CLOCK_OFFSET_MAX_MS = 200.0
DEFAULT_VERDICT_TIMEOUT_MS = 300.0
DEFAULT_AS_RELEASE_ENABLED = False


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
    )
