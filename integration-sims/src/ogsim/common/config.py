"""ogsim.common.config -- YAML+env configuration for ogsim.fleet/ogsim.scada.

Mirrors `ogsim.market.config`'s env-driven dataclass style, but also reads
an optional YAML file (path from env, e.g. `OGSIM_FLEET_CONFIG`) so the
sim harness scale (§4.1/§5) can be tuned without code changes. A missing
YAML file falls back to defaults, never crashes (02b §4/§5 defaults).

Precedence and production safety (mirrors the orchestrator's platform/config.py):
- environment variables always win over the YAML (`OG_MQTT_ROOT`/`OG_MQTT_HOST`/`OG_MQTT_PORT`, and
  `OGSIM_GUARDIAN_PUBLIC_KEY_PATH`/`OGSIM_SAFESTOP_PUBLIC_KEY_PATH` for the key paths);
- the default YAML path is the one shipped next to this package (`<install>/config/*.yaml`), never a
  path relative to the working directory;
- the production topic root `og/v1` and the production guardian/safestop public keys are used ONLY
  when the process is explicitly marked production (`OGSIM_ENV=prod`, set in the production units)
  and is not an agent workspace (`OG_WS` unset). Anything else refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: `integration-sims/` of this install (src/ogsim/common/config.py -> parents[3]).
INSTALL_DIR = Path(__file__).resolve().parents[3]
DEFAULT_FLEET_CONFIG_PATH = INSTALL_DIR / "config" / "fleet.yaml"
DEFAULT_SCADA_CONFIG_PATH = INSTALL_DIR / "config" / "scada.yaml"

DEFAULT_ZONES: tuple[str, ...] = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")

# Dual-unit homes (confirmed by Base, 2026-09-25): see FleetConfig's docstring for the
# deterministic hub-selection rule shared word-for-word with opengrid.fleet.seed.
DUAL_UNIT_SHARE_DEFAULT: float = 0.2
E_KWH_DUAL_UNIT_DEFAULT: float = 78.4
P_KW_DUAL_UNIT_DEFAULT: float = 20.0

# Bank rating models a feeder segment (~50 homes), not a single distribution transformer
# (confirmed by Base, 2026-09-25).
BANK_KVA_RATING_DEFAULT: float = 600.0


def load_yaml_file(path: str) -> dict[str, Any]:
    """Loads a YAML mapping from `path`; returns {} if absent or empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


#: The production MQTT topic root. Only an explicitly marked production process may use it.
PRODUCTION_TOPIC_ROOT = "og/v1"
#: The production marker: the production og-sim-* units set `OGSIM_ENV=prod`; nothing else does.
PRODUCTION_ENV = "prod"


class WorkspaceConfigError(RuntimeError):
    """A non-production run (an agent workspace, `OG_WS` set by tools/remote.ps1, or any process without
    `OGSIM_ENV=prod`) that would otherwise fall back to a production default -- the production topic
    root or the production guardian/safestop public keys."""


def workspace_name() -> str:
    """The agent workspace this process runs in (`OG_WS`, set by tools/remote.ps1), or ""."""
    return os.environ.get("OG_WS", "").strip()


def is_production() -> bool:
    """True only for an explicitly marked production process (`OGSIM_ENV=prod`) outside any workspace.
    A workspace that also claims production is refused outright."""
    marked = os.environ.get("OGSIM_ENV", "").strip() == PRODUCTION_ENV
    if marked and workspace_name():
        raise WorkspaceConfigError(
            f"OG_WS={workspace_name()!r} with OGSIM_ENV={PRODUCTION_ENV!r}: a workspace is never production"
        )
    return marked


def resolve_topic_root(raw: dict[str, Any]) -> str:
    """`OG_MQTT_ROOT` always wins over the YAML `topic_root` (as in the orchestrator's config), else the
    YAML value, else the production root -- which is refused unless the process is marked production
    (`is_production`). A workspace or dev sim must never publish telemetry or acks into production."""
    root = os.environ.get("OG_MQTT_ROOT", "").strip() or str(raw.get("topic_root", PRODUCTION_TOPIC_ROOT))
    if root.rstrip("/") == PRODUCTION_TOPIC_ROOT and not is_production():
        raise WorkspaceConfigError(
            f"topic root {root!r} is production, but this process is not marked production "
            f"(OGSIM_ENV={os.environ.get('OGSIM_ENV', '')!r}, OG_WS={workspace_name()!r}): set OG_MQTT_ROOT"
        )
    return root


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    topic_root: str


def mqtt_settings_from_env(raw: dict[str, Any]) -> MqttSettings:
    return MqttSettings(
        host=str(os.environ.get("OG_MQTT_HOST") or raw.get("host", "127.0.0.1")),
        port=int(os.environ.get("OG_MQTT_PORT") or raw.get("port", 1883)),
        username="og_sim",
        password=os.environ.get("OG_MQTT_SIM_PASSWORD", ""),
        topic_root=resolve_topic_root(raw),
    )


@dataclass(frozen=True)
class FleetConfig:
    """Fleet sim config (02b §4 hub/bank physics, §5 sim harness).

    Dual-unit rule (must match ogsim.fleet.state / opengrid.fleet.seed exactly): hub index i
    (hub-{i:05d}) is dual-unit iff floor((i + 1) * dual_unit_share) > floor(i * dual_unit_share),
    which selects exactly floor(hub_count * dual_unit_share) hubs, deterministically and evenly
    spread across i in range(hub_count).
    """

    mqtt: MqttSettings
    hub_count: int = 2000
    bank_count: int = 40
    zones: tuple[str, ...] = DEFAULT_ZONES
    telemetry_interval_s: float = 2.0
    lease_ttl_s: float = 30.0
    lease_hold_after_expiry_s: float = 5.0
    stop_ramp_s: float = 4.0
    # Base Power home battery, usable kWh (confirmed by Base, 2026-09-25).
    e_kwh_default: float = 39.2
    reserve_frac_default: float = 0.20
    # Base Power inverter, kW per battery unit (confirmed by Base, 2026-09-25).
    p_kw_default: float = 11.0
    # Share of homes with two battery units instead of one (confirmed by Base, 2026-09-25); see
    # class docstring for the deterministic hub-selection rule.
    dual_unit_share: float = DUAL_UNIT_SHARE_DEFAULT
    # Dual-unit home usable kWh (confirmed by Base, 2026-09-25).
    e_kwh_dual_unit: float = E_KWH_DUAL_UNIT_DEFAULT
    # Dual-unit home inverter kW (confirmed by Base, 2026-09-25).
    p_kw_dual_unit: float = P_KW_DUAL_UNIT_DEFAULT
    eta_c: float = 0.9487
    eta_d: float = 0.9487
    self_discharge_kwh_per_h: float = 0.0005
    # Feeder segment (~50 homes), not a single distribution transformer.
    bank_kva_rating_default: float = BANK_KVA_RATING_DEFAULT
    guardian_public_key_path: str = "/etc/opengrid/guardian_ed25519.pub"
    guardian_public_key_path_dev: str = ""
    safestop_public_key_path: str = "/etc/opengrid/safestop_ed25519.pub"
    safestop_public_key_path_dev: str = ""
    # Per-inverter power-quality imperfection model (06-service-profiles-and-power-quality.md
    # §3.1/§7.1/§7.3), seeded once per unit at build time plus a slow ambient drift each tick.
    pq_freq_offset_std_hz: float = 0.01
    pq_voltage_offset_std_pct: float = 0.5
    pq_thd_current_median_pct: float = 2.0
    pq_thd_current_p95_pct: float = 4.5
    # false = diverse per-unit harmonic phase angles (cancellation regime, §3.2b); true = all
    # units on one firmware batch share identical angles (stacking/worst-case regime).
    pq_harmonic_phase_lock: bool = False
    pq_ride_through_class_default: str = "CATEGORY_III"
    # Remote-calibration rate limit, §5.5.4: at most one attempt per unit per rolling window.
    pq_calibration_rate_limit_s: float = 86400.0
    # Waveform summary/raw generation and publication (06-service-profiles-and-power-quality.md
    # §6.4/§7.4, WP-H), seeded from `fleet.yaml`'s `wave:` block.
    wave_harmonic_detail_interval_s: float = 30.0
    wave_harmonic_detail_delta_pct: float = 1.0
    wave_raw_audit_sample_pct_per_min: float = 1.0
    wave_sync_source: str = "ptp"
    wave_sync_quality_ns: float = 50.0
    # S9 wave-2 fix: gates the summary message itself (not just its harmonic-detail
    # sub-block) to this cadence/deadband -- see fleet.yaml's `wave:` block comment.
    wave_summary_interval_s: float = 10.0
    wave_summary_delta_pct: float = 1.0

    def public_key_path(self) -> str:
        """Guardian public key path; dev override wins when set, so local
        runs don't need /etc access. A workspace must set the override: it
        never falls back to the production guardian key."""
        return _key_path(self.guardian_public_key_path_dev, self.guardian_public_key_path, "guardian")

    def safestop_key_path(self) -> str:
        """Safestop public key path (crypto.md §2.3: the only key allowed to
        sign a StopEvent `action="ENGAGE"`); dev override wins when set, and
        is mandatory in a workspace."""
        return _key_path(self.safestop_public_key_path_dev, self.safestop_public_key_path, "safestop")


def _key_path(dev_override: str, production_path: str, which: str) -> str:
    if dev_override:
        return dev_override
    if not is_production():
        raise WorkspaceConfigError(
            f"no {which} public key override is set and this process is not marked production "
            f"(OGSIM_ENV=prod): set OGSIM_{which.upper()}_PUBLIC_KEY_PATH (or {which}_public_key_path_dev) "
            f"rather than trusting the production key {production_path!r}"
        )
    return production_path


def load_fleet_config(path: str | None = None) -> FleetConfig:
    path = path or os.environ.get("OGSIM_FLEET_CONFIG") or str(DEFAULT_FLEET_CONFIG_PATH)
    raw = load_yaml_file(path)
    defaults = FleetConfig(mqtt=mqtt_settings_from_env(raw.get("mqtt", {})))
    return FleetConfig(
        mqtt=defaults.mqtt,
        hub_count=int(raw.get("hub_count", defaults.hub_count)),
        bank_count=int(raw.get("bank_count", defaults.bank_count)),
        zones=tuple(raw.get("zones", defaults.zones)),
        telemetry_interval_s=float(raw.get("telemetry_interval_s", defaults.telemetry_interval_s)),
        lease_ttl_s=float(raw.get("lease_ttl_s", defaults.lease_ttl_s)),
        lease_hold_after_expiry_s=float(
            raw.get("lease_hold_after_expiry_s", defaults.lease_hold_after_expiry_s)
        ),
        stop_ramp_s=float(raw.get("stop_ramp_s", defaults.stop_ramp_s)),
        e_kwh_default=float(raw.get("e_kwh_default", defaults.e_kwh_default)),
        reserve_frac_default=float(raw.get("reserve_frac_default", defaults.reserve_frac_default)),
        p_kw_default=float(raw.get("p_kw_default", defaults.p_kw_default)),
        dual_unit_share=float(raw.get("dual_unit_share", defaults.dual_unit_share)),
        e_kwh_dual_unit=float(raw.get("e_kwh_dual_unit", defaults.e_kwh_dual_unit)),
        p_kw_dual_unit=float(raw.get("p_kw_dual_unit", defaults.p_kw_dual_unit)),
        eta_c=float(raw.get("eta_c", defaults.eta_c)),
        eta_d=float(raw.get("eta_d", defaults.eta_d)),
        self_discharge_kwh_per_h=float(
            raw.get("self_discharge_kwh_per_h", defaults.self_discharge_kwh_per_h)
        ),
        bank_kva_rating_default=float(raw.get("bank_kva_rating_default", defaults.bank_kva_rating_default)),
        guardian_public_key_path=str(raw.get("guardian_public_key_path", defaults.guardian_public_key_path)),
        guardian_public_key_path_dev=str(
            os.environ.get("OGSIM_GUARDIAN_PUBLIC_KEY_PATH") or raw.get("guardian_public_key_path_dev", "")
        ),
        safestop_public_key_path=str(raw.get("safestop_public_key_path", defaults.safestop_public_key_path)),
        safestop_public_key_path_dev=str(
            os.environ.get("OGSIM_SAFESTOP_PUBLIC_KEY_PATH") or raw.get("safestop_public_key_path_dev", "")
        ),
        **_inverter_pq_fields(raw, defaults),
        **_wave_fields(raw, defaults),
    )


def _inverter_pq_fields(raw: dict[str, Any], defaults: FleetConfig) -> dict[str, Any]:
    """Reads the optional `inverter_pq:` YAML block (§7.3), defaulting every field
    independently so a partial or absent block never crashes config loading."""
    block = raw.get("inverter_pq", {})
    block = block if isinstance(block, dict) else {}
    return {
        "pq_freq_offset_std_hz": float(block.get("freq_offset_std_hz", defaults.pq_freq_offset_std_hz)),
        "pq_voltage_offset_std_pct": float(
            block.get("voltage_offset_std_pct", defaults.pq_voltage_offset_std_pct)
        ),
        "pq_thd_current_median_pct": float(
            block.get("thd_current_median_pct", defaults.pq_thd_current_median_pct)
        ),
        "pq_thd_current_p95_pct": float(block.get("thd_current_p95_pct", defaults.pq_thd_current_p95_pct)),
        "pq_harmonic_phase_lock": bool(block.get("harmonic_phase_lock", defaults.pq_harmonic_phase_lock)),
        "pq_ride_through_class_default": str(
            block.get("ride_through_class_default", defaults.pq_ride_through_class_default)
        ),
        "pq_calibration_rate_limit_s": float(
            block.get("calibration_rate_limit_s", defaults.pq_calibration_rate_limit_s)
        ),
    }


def _wave_fields(raw: dict[str, Any], defaults: FleetConfig) -> dict[str, Any]:
    """Reads the optional `wave:` YAML block (§7.3/§7.4, WP-H), defaulting every field
    independently so a partial or absent block never crashes config loading."""
    block = raw.get("wave", {})
    block = block if isinstance(block, dict) else {}
    return {
        "wave_harmonic_detail_interval_s": float(
            block.get("harmonic_detail_interval_s", defaults.wave_harmonic_detail_interval_s)
        ),
        "wave_harmonic_detail_delta_pct": float(
            block.get("harmonic_detail_delta_pct", defaults.wave_harmonic_detail_delta_pct)
        ),
        "wave_raw_audit_sample_pct_per_min": float(
            block.get("raw_audit_sample_pct_per_min", defaults.wave_raw_audit_sample_pct_per_min)
        ),
        "wave_sync_source": str(block.get("sync_source", defaults.wave_sync_source)),
        "wave_sync_quality_ns": float(block.get("sync_quality_ns", defaults.wave_sync_quality_ns)),
        "wave_summary_interval_s": float(block.get("summary_interval_s", defaults.wave_summary_interval_s)),
        "wave_summary_delta_pct": float(block.get("summary_delta_pct", defaults.wave_summary_delta_pct)),
    }


@dataclass(frozen=True)
class ScadaConfig:
    """SCADA sim config (02b §4.2 bank kVA rating, §5.1 bank aggregators)."""

    mqtt: MqttSettings
    bank_count: int = 40
    zones: tuple[str, ...] = DEFAULT_ZONES
    publish_interval_s: float = 2.0
    # Feeder segment (~50 homes), not a single distribution transformer.
    bank_kva_rating_default: float = BANK_KVA_RATING_DEFAULT
    overload_consecutive_samples: int = 3
    history_tsv_path: str = "/var/lib/opengrid/import/mariadb_history_signals.tsv"
    base_load_kw_default: float = 200.0


def load_scada_config(path: str | None = None) -> ScadaConfig:
    path = path or os.environ.get("OGSIM_SCADA_CONFIG") or str(DEFAULT_SCADA_CONFIG_PATH)
    raw = load_yaml_file(path)
    defaults = ScadaConfig(mqtt=mqtt_settings_from_env(raw.get("mqtt", {})))
    return ScadaConfig(
        mqtt=defaults.mqtt,
        bank_count=int(raw.get("bank_count", defaults.bank_count)),
        zones=tuple(raw.get("zones", defaults.zones)),
        publish_interval_s=float(raw.get("publish_interval_s", defaults.publish_interval_s)),
        bank_kva_rating_default=float(raw.get("bank_kva_rating_default", defaults.bank_kva_rating_default)),
        overload_consecutive_samples=int(
            raw.get("overload_consecutive_samples", defaults.overload_consecutive_samples)
        ),
        history_tsv_path=str(raw.get("history_tsv_path", defaults.history_tsv_path)),
        base_load_kw_default=float(raw.get("base_load_kw_default", defaults.base_load_kw_default)),
    )


__all__ = [
    "DEFAULT_FLEET_CONFIG_PATH",
    "DEFAULT_SCADA_CONFIG_PATH",
    "PRODUCTION_ENV",
    "PRODUCTION_TOPIC_ROOT",
    "FleetConfig",
    "MqttSettings",
    "ScadaConfig",
    "WorkspaceConfigError",
    "is_production",
    "load_fleet_config",
    "load_scada_config",
    "load_yaml_file",
    "resolve_topic_root",
    "workspace_name",
]
