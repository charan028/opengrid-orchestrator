"""ogsim.common.config -- YAML+env configuration for ogsim.fleet/ogsim.scada.

Mirrors `ogsim.market.config`'s env-driven dataclass style, but also reads
an optional YAML file (path from env, e.g. `OGSIM_FLEET_CONFIG`) so the
sim harness scale (§4.1/§5) can be tuned without code changes. A missing
YAML file falls back to defaults, never crashes (02b §4/§5 defaults).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml

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


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    topic_root: str


def mqtt_settings_from_env(raw: dict[str, Any]) -> MqttSettings:
    return MqttSettings(
        host=str(raw.get("host", os.environ.get("OG_MQTT_HOST", "127.0.0.1"))),
        port=int(raw.get("port", os.environ.get("OG_MQTT_PORT", "1883"))),
        username="og_sim",
        password=os.environ.get("OG_MQTT_SIM_PASSWORD", ""),
        topic_root=str(raw.get("topic_root", os.environ.get("OG_MQTT_ROOT", "og/v1"))),
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

    def public_key_path(self) -> str:
        """Guardian public key path; dev override wins when set, so local
        runs don't need /etc access."""
        return self.guardian_public_key_path_dev or self.guardian_public_key_path

    def safestop_key_path(self) -> str:
        """Safestop public key path (crypto.md §2.3: the only key allowed to
        sign a StopEvent `action="ENGAGE"`); dev override wins when set."""
        return self.safestop_public_key_path_dev or self.safestop_public_key_path


def load_fleet_config(path: str | None = None) -> FleetConfig:
    path = path or os.environ.get("OGSIM_FLEET_CONFIG", "integration-sims/config/fleet.yaml")
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
        guardian_public_key_path_dev=str(raw.get("guardian_public_key_path_dev", "")),
        safestop_public_key_path=str(raw.get("safestop_public_key_path", defaults.safestop_public_key_path)),
        safestop_public_key_path_dev=str(raw.get("safestop_public_key_path_dev", "")),
        **_inverter_pq_fields(raw, defaults),
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
    path = path or os.environ.get("OGSIM_SCADA_CONFIG", "integration-sims/config/scada.yaml")
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
    "FleetConfig",
    "MqttSettings",
    "ScadaConfig",
    "load_fleet_config",
    "load_scada_config",
    "load_yaml_file",
]
