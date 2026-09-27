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
from collections.abc import Mapping
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ZoneBlockConfig:
    """One optional extra load-zone block, appended after the base `hub_count`/`bank_count`/`zones`
    fleet (build phase, 2026-09-26: Austin Energy `LZ_AEN`/CPS Energy `LZ_CPS`, both municipal,
    vertically-integrated regulated utilities outside ERCOT retail choice -- see
    `docs/orchestrator/07-delivery/08-market-model-two-markets.md`).

    Disabled by default (`enabled=False`): a disabled block reserves no hub/bank ids at all, so the
    base fleet's ids (`hub-00000..`, `bank-000..`) never move when a block is defined-but-off, and new
    ids only ever start at `hub_count`/`bank_count` (or after the previous enabled block) once a block
    is turned on. Every bank in a block shares the block's single `zone` (feeder segments can't span
    zones, same rule as the base fleet); `homes_per_bank` hubs are assigned round-robin across the
    block's `banks`, and the same per-bank-offset dual-unit rule as the base fleet (`_dual_unit_mask`)
    selects `floor(homes_per_bank * dual_unit_share) dual-unit hubs per bank. Must be parsed
    identically (field names/defaults) in `opengrid.fleet.seed`'s mirror of this dataclass -- that
    module can't import this one (BUILD.md S1 "share no code")."""

    zone: str
    banks: int
    homes_per_bank: int
    enabled: bool = False


#: Substation battery-set default rating (09-optimizer-dispatcher-update.md D11: "A new asset class
#: SUBSTATION_BESS has its own SoC, PCS rating, POI and transformer limits, ramp and wear. The default
#: is 20 MW / 2 h (40 MWh), RTE 0.88 and a 20% floor, with 4 h as a sensitivity").
SUBSTATION_RATED_MW_DEFAULT: float = 20.0
SUBSTATION_DURATION_H_DEFAULT: float = 2.0
SUBSTATION_RESERVE_FRAC_DEFAULT: float = 0.20
#: One-way efficiency from the round-trip figure (D11's RTE 0.88; `core/physics.py` uses one-way eta_c/
#: eta_d, same split as homes' 0.9487 -- see 09 S1.2's table: "substation sqrt(0.88)=0.938").
SUBSTATION_ETA_DEFAULT: float = 0.88**0.5

#: Truck-mounted mobile battery default rating (owner request 2026-09-26: realistic truck-mounted BESS,
#: ~1 MWh / 500 kW, 20% floor). ASSUMPTION, planning value, to be confirmed with Base.
MOBILE_P_KW_DEFAULT: float = 500.0
MOBILE_E_KWH_DEFAULT: float = 1000.0
MOBILE_RESERVE_FRAC_DEFAULT: float = 0.20


@dataclass(frozen=True)
class SubstationAssetConfig:
    """One optional substation-sited battery-set asset (build phase, 2026-09-26: FLEET-SIM
    reassignment; 09-optimizer-dispatcher-update.md D11 `SUBSTATION_BESS`), simulated by
    `ogsim.fleet` alongside home hubs: it publishes telemetry, accepts signed command batches and
    leases exactly like a hub (`ogsim.fleet.state.build_fleet_state` appends it as a one-hub "bank" of
    its own, so `ogsim.fleet.commands`/`lease`/`stop`/`runtime` all handle it for free -- see
    `build_fleet_state`'s docstring) -- only its rated power/energy and lack of home load are
    different. Configurable and OFF by default (`enabled=False`); coordinate `asset_id` with the
    MARKET-MODEL agent's `og.asset` model (see the FLEET-SIM build report) before wiring the
    orchestrator side to it.

    Known simplification: the substation "hub" still runs through the same per-hub household load
    model as a home (a few kW of simulated diurnal load), which is negligible (< 0.02%) against a
    20 MW rating and not worth a special-cased zero for a first cut."""

    asset_id: str  # e.g. "sub-LZ_AEN-00"; a distinct namespace from "hub-NNNNN"/"bank-NNN"
    zone: str
    rated_mw: float = SUBSTATION_RATED_MW_DEFAULT
    duration_h: float = SUBSTATION_DURATION_H_DEFAULT
    enabled: bool = False


@dataclass(frozen=True)
class MobileUnitConfig:
    """One simulated mobile battery/trailer unit (D-31, 2026-09-26: docs/orchestrator/07-delivery/
    11-decision-log.md -- a mobile unit is NEVER charged from the fleet, only from its home station's
    own grid connection). Mirrors SERVICES' `orchestrator/config/service_profiles/
    mobile_storage_home_stations.toml` `[[assignment]]` 1:1 -- same `trailer_id`/`bank_id` and
    `home_station_id` id space (`trailer_id` IS the `og.bank.bank_id`/`og.hub.hub_id` once the D-31
    migration lands; today it's a plain string, no DB row).

    With `simulate=False` (the default, e.g. svc-mobile-storage.yaml's `trailer-mb-01`) an entry is a
    target-existence registry row only, for the #33 scenario target-check. With `simulate=True` (the
    owner's truck fleet, 2026-09-26: `truck-aus-*`, `truck-sat-*`, `truck-dfw-*`) `ogsim.fleet` also
    simulates it as its own one-hub bank (`ogsim.fleet.state._mobile_segment`): hub id `trailer_id`,
    bank id `bank_id` (default `bank-<trailer_id>`), at `lat`/`lon` (its current position, the home
    station's coordinates while parked there), rated `p_kw`/`e_kwh` with a `reserve_frac` floor, no
    household load or PV. D-31 in the sim: a unit charges only while `at_home`; away from home any
    charging request is held at 0 kW and `p_ch_max_kw` reports 0. A scenario moves a unit
    (`FleetEngine.move_mobile_unit`): `mobile_deployment_start` (params `site_lat`/`site_lon`) and
    `mobile_deployment_relocate` drive it to a site, `mobile_home_station_charge` back to `home_position`;
    each move republishes its device_info (the position the orchestrator's G-35/selector read), as does
    a heartbeat every `FleetConfig.mobile_position_interval_s`."""

    trailer_id: str
    home_station_id: str
    simulate: bool = False
    bank_id: str = ""
    zone: str = ""
    lat: float = 0.0
    lon: float = 0.0
    p_kw: float = MOBILE_P_KW_DEFAULT
    e_kwh: float = MOBILE_E_KWH_DEFAULT
    reserve_frac: float = MOBILE_RESERVE_FRAC_DEFAULT
    at_home: bool = True
    # The home station's position; None = `lat`/`lon` (a unit configured parked at home, as shipped).
    home_lat: float | None = None
    home_lon: float | None = None

    @property
    def home_position(self) -> tuple[float, float]:
        """The depot a scenario's `mobile_home_station_charge` step drives the unit back to."""
        return (
            self.lat if self.home_lat is None else self.home_lat,
            self.lon if self.home_lon is None else self.home_lon,
        )

    @property
    def sim_bank_id(self) -> str:
        """The unit's single-hub bank id: `bank_id` if configured, else `bank-<trailer_id>` (the same
        `bank-<id>` scheme `_substation_segment` uses)."""
        return self.bank_id or f"bank-{self.trailer_id}"


def bank_topology(
    bank_count: int, zones: tuple[str, ...], zone_blocks: tuple[ZoneBlockConfig, ...]
) -> tuple[list[str], list[str]]:
    """`(bank_ids, zone_per_bank)` for the base fleet's `bank_count` banks (round-robin across
    `zones`, `bank-000..`) plus every ENABLED entry in `zone_blocks`, in the SAME
    id-numbering/offset scheme `ogsim.fleet.state.build_fleet_state` uses for hubs (a block's
    banks start right after the base fleet's `bank_count`, then after each already-enabled
    block in order, so enabling/disabling one block never renumbers another).

    Lives in `ogsim.common` (not duplicated separately in `ogsim.fleet.state` and
    `ogsim.scada.runtime`) because both need the identical bank roster for a shared config --
    unlike the opengrid/ogsim boundary (BUILD.md S1: "share no code"), `ogsim.fleet`/
    `ogsim.scada`/`ogsim.common` are one sims package with one owner, and `ogsim.common` is
    already the shared config module both import."""
    bank_ids = [f"bank-{i:03d}" for i in range(bank_count)]
    zone_per_bank = [zones[i % len(zones)] for i in range(bank_count)]
    offset = bank_count
    for block in zone_blocks:
        if not block.enabled:
            continue
        bank_ids += [f"bank-{offset + i:03d}" for i in range(block.banks)]
        zone_per_bank += [block.zone] * block.banks
        offset += block.banks
    return bank_ids, zone_per_bank


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


def resolve_mqtt_credentials(role_user: str, role_password_env: str) -> tuple[str, str]:
    """Broker credentials, mirroring the orchestrator's `platform.mqtt.resolve_mqtt_credentials`:
    `OG_MQTT_WS_USER`/`OG_MQTT_WS_PASSWORD` (a workspace, `ogw_<ws>`, reaching only `ogtest/<ws>/#`)
    always win; otherwise the production role user with its password env var. A workspace (`OG_WS`)
    without workspace credentials, or a workspace user without a password, is refused."""
    ws_user = os.environ.get("OG_MQTT_WS_USER", "").strip()
    if ws_user:
        ws_password = os.environ.get("OG_MQTT_WS_PASSWORD", "")
        if not ws_password:
            raise WorkspaceConfigError("OG_MQTT_WS_USER is set but OG_MQTT_WS_PASSWORD is empty")
        return ws_user, ws_password
    if workspace_name():
        raise WorkspaceConfigError(
            f"OG_WS={workspace_name()!r} is set but OG_MQTT_WS_USER is not: a workspace never uses the "
            "production MQTT users"
        )
    return role_user, os.environ.get(role_password_env, "")


def mqtt_settings_from_env(raw: dict[str, Any]) -> MqttSettings:
    username, password = resolve_mqtt_credentials("og_sim", "OG_MQTT_SIM_PASSWORD")
    return MqttSettings(
        host=str(os.environ.get("OG_MQTT_HOST") or raw.get("host", "127.0.0.1")),
        port=int(os.environ.get("OG_MQTT_PORT") or raw.get("port", 1883)),
        username=username,
        password=password,
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
    # OWNER DECISION, 2026-09-26 (V-32): 10 s is the spec's normal telemetry cadence (disk-load
    # reduction); this built-in default now matches shipped fleet.yaml/scada.yaml so
    # `test_shipped_yaml_matches_the_built_in_defaults` holds without an explicit override.
    telemetry_interval_s: float = 10.0
    # The fleet's PHYSICS step (SoC, stop ramp, lease/autonomy) always runs on this cadence,
    # independent of `telemetry_interval_s` -- shipped fleet.yaml publishes telemetry every 10 s
    # while still physics-ticking every 2 s, so a stop ramp (or anything else keyed to `dt_s`) is
    # unaffected by how often telemetry is published.
    physics_tick_interval_s: float = 2.0
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
    # Optional extra load-zone blocks (Austin Energy/CPS Energy, build phase 2026-09-26), appended
    # after hub-{hub_count-1}/bank-{bank_count-1} in config order; empty/all-disabled by default, so
    # the base fleet is unchanged (see `ZoneBlockConfig`'s docstring).
    zone_blocks: tuple[ZoneBlockConfig, ...] = ()
    # S1.9 F5 (09-optimizer-dispatcher-update.md): the per-hub above-continuous peak budget B_i, in
    # seconds of P_cont-equivalent (kW*s = p_kw_limit * this). "Peak = continuous" until Base confirms
    # (OQ-10), so this is 0.0 by default -- no reported peak headroom, matching F5's "inactive" status.
    peak_power_budget_s_default: float = 0.0
    # Optional substation-sited battery-set assets (D11 SUBSTATION_BESS, `SubstationAssetConfig`'s
    # docstring); empty by default, so no substation asset is simulated unless explicitly configured.
    substation_assets: tuple[SubstationAssetConfig, ...] = ()
    # Mobile-unit (trailer) registry (#33 target-check, D-31, 2026-09-26); see `MobileUnitConfig`'s
    # docstring. Empty by default -- a workspace/deployment without any mobile units configures none.
    mobile_units: tuple[MobileUnitConfig, ...] = ()
    # D-31: a simulated mobile unit re-publishes its device_info (its position) at least this often, well
    # inside the orchestrator's 300 s position age limit (`opengrid.core.geo.MOBILE_POSITION_MAX_AGE_S`).
    mobile_position_interval_s: float = 60.0
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
    # V-32, 2026-09-26: 30 s matches shipped fleet.yaml (10 -> 30 s, alongside the telemetry
    # cadence reduction) so the built-in default and shipped YAML agree.
    wave_summary_interval_s: float = 30.0
    wave_summary_delta_pct: float = 1.0
    # OWNER DECISION, 2026-09-26 (R3.1): firmware updates ship in the final release. This is a plain
    # dict, not individual FleetConfig fields, handed straight to `ogsim.fleet.firmware.
    # FirmwareSimConfig(**config.firmware)` by `FleetEngine.__init__` -- that dataclass (built by the
    # FIRMWARE lane) owns its own field shape/defaults, so this loader only merges YAML values onto
    # them rather than re-declaring each one here. The literal defaults below mirror
    # `FirmwareSimConfig`'s own (duration 30-90 s, failure_rate 0.0, no image catalogue) and match
    # shipped fleet.yaml's `firmware:` block exactly, so `test_shipped_yaml_matches_the_built_in_
    # defaults` holds without this being a documented exception like zone_blocks/substation_assets.
    firmware: dict[str, Any] = field(
        default_factory=lambda: {
            "duration_min_s": 30.0,
            "duration_max_s": 90.0,
            "failure_rate": 0.0,
            "images": {},
        }
    )

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
        physics_tick_interval_s=float(raw.get("physics_tick_interval_s", defaults.physics_tick_interval_s)),
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
        zone_blocks=_zone_blocks_from_raw(raw.get("zone_blocks", [])),
        peak_power_budget_s_default=float(
            raw.get("peak_power_budget_s_default", defaults.peak_power_budget_s_default)
        ),
        substation_assets=_substation_assets_from_raw(raw.get("substation_assets", [])),
        mobile_units=_mobile_units_from_raw(raw.get("mobile_units", [])),
        mobile_position_interval_s=float(
            raw.get("mobile_position_interval_s", defaults.mobile_position_interval_s)
        ),
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
        firmware=_firmware_from_raw(raw.get("firmware", {}), defaults.firmware),
    )


def _zone_blocks_from_raw(raw_blocks: Any) -> tuple[ZoneBlockConfig, ...]:
    """Reads the optional `zone_blocks:` YAML list (`ZoneBlockConfig`'s docstring); a missing key, a
    non-list value, or a non-mapping entry is treated as "no extra blocks" rather than raising, so a
    fleet.yaml without this key (every deployment before 2026-09-26) still loads (BUILD.md S5a "no
    silent fallbacks" is about masking real data, not about a key that was never required)."""
    if not isinstance(raw_blocks, list):
        return ()
    blocks = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        blocks.append(
            ZoneBlockConfig(
                zone=str(block["zone"]),
                banks=int(block["banks"]),
                homes_per_bank=int(block["homes_per_bank"]),
                enabled=bool(block.get("enabled", False)),
            )
        )
    return tuple(blocks)


def _substation_assets_from_raw(raw_assets: Any) -> tuple[SubstationAssetConfig, ...]:
    """Reads the optional `substation_assets:` YAML list (`SubstationAssetConfig`'s docstring); a
    missing key, non-list value, or non-mapping entry parses to "no substation assets" (same
    permissive-default policy as `_zone_blocks_from_raw`)."""
    if not isinstance(raw_assets, list):
        return ()
    assets = []
    for asset in raw_assets:
        if not isinstance(asset, dict):
            continue
        assets.append(
            SubstationAssetConfig(
                asset_id=str(asset["asset_id"]),
                zone=str(asset["zone"]),
                rated_mw=float(asset.get("rated_mw", SUBSTATION_RATED_MW_DEFAULT)),
                duration_h=float(asset.get("duration_h", SUBSTATION_DURATION_H_DEFAULT)),
                enabled=bool(asset.get("enabled", False)),
            )
        )
    return tuple(assets)


#: Known `ogsim.fleet.firmware.FirmwareSimConfig` scalar/dict field names this loader will pass
#: through from YAML -- `failure_kinds` (a tuple field) is handled separately below since it needs a
#: list->tuple cast, and any UNKNOWN key in the YAML block is dropped rather than passed through
#: (`FirmwareSimConfig(**config.firmware)` would otherwise raise `TypeError` on a typo'd key).
_FIRMWARE_FIELD_CASTERS: dict[str, Any] = {
    "duration_min_s": float,
    "duration_max_s": float,
    "failure_rate": float,
    "images": dict,
    "default_version": str,
    "default_hardware_revision": str,
}


def _firmware_from_raw(raw_firmware: Any, default: dict[str, Any]) -> dict[str, Any]:
    """Reads the optional `firmware:` YAML block into a plain dict merged onto `default` (this
    `FleetConfig`'s own built-in `firmware` default) -- a missing key, a non-dict value, or an unknown
    field name is treated permissively (same policy as `_zone_blocks_from_raw`/`_wave_fields`: a
    fleet.yaml without this key, or with only some of its fields set, still loads)."""
    merged = dict(default)
    if not isinstance(raw_firmware, dict):
        return merged
    for key, caster in _FIRMWARE_FIELD_CASTERS.items():
        if key in raw_firmware:
            merged[key] = caster(raw_firmware[key])
    if isinstance(raw_firmware.get("failure_kinds"), list):
        merged["failure_kinds"] = tuple(str(k) for k in raw_firmware["failure_kinds"])
    return merged


def _mobile_units_from_raw(raw_units: Any) -> tuple[MobileUnitConfig, ...]:
    """Reads the optional `mobile_units:` YAML list (`MobileUnitConfig`'s docstring); a missing key,
    non-list value, or non-mapping entry parses to "no mobile units" (same permissive-default policy
    as `_zone_blocks_from_raw`/`_substation_assets_from_raw`)."""
    if not isinstance(raw_units, list):
        return ()
    units = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            continue
        units.append(
            MobileUnitConfig(
                trailer_id=str(unit["trailer_id"]),
                home_station_id=str(unit["home_station_id"]),
                simulate=bool(unit.get("simulate", False)),
                bank_id=str(unit.get("bank_id", "")),
                zone=str(unit.get("zone", "")),
                lat=float(unit.get("lat", 0.0)),
                lon=float(unit.get("lon", 0.0)),
                p_kw=float(unit.get("p_kw", MOBILE_P_KW_DEFAULT)),
                e_kwh=float(unit.get("e_kwh", MOBILE_E_KWH_DEFAULT)),
                reserve_frac=float(unit.get("reserve_frac", MOBILE_RESERVE_FRAC_DEFAULT)),
                at_home=bool(unit.get("at_home", True)),
                home_lat=None if unit.get("home_lat") is None else float(unit["home_lat"]),
                home_lon=None if unit.get("home_lon") is None else float(unit["home_lon"]),
            )
        )
    return tuple(units)


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
    # Bug fix, 2026-09-26 (R3): the auto-issued overload LIMIT never expired. It now lifts after
    # EITHER this many consecutive back-under-rating readings, or `overload_limit_max_duration_s`,
    # whichever comes first (`ogsim.scada.instructions.OverloadRule`).
    overload_clear_samples: int = 3
    overload_limit_max_duration_s: float = 900.0  # 15 min
    history_tsv_path: str = "/var/lib/opengrid/import/mariadb_history_signals.tsv"
    base_load_kw_default: float = 200.0
    # Optional extra load-zone blocks (see `ZoneBlockConfig`'s docstring) -- mirrors
    # `FleetConfig.zone_blocks` (kept in the same order/values in scada.yaml as in fleet.yaml,
    # the same manual-duplication pattern `bank_count`/`zones` already use across the two
    # config files) so `ScadaEngine`'s bank roster covers exactly the banks `ogsim.fleet`
    # actually seeds -- empty/all-disabled by default, so the base fleet is unchanged.
    zone_blocks: tuple[ZoneBlockConfig, ...] = ()
    # Optional substation-sited battery-set assets (D11 SUBSTATION_BESS; mirrors
    # `FleetConfig.substation_assets`, same manual-duplication-across-config-files pattern as
    # `zone_blocks` above) -- OWNER DECISION D-29(b), 2026-09-26: `ogsim.scada` must also measure a
    # substation asset once it's enabled, not just `ogsim.fleet`. Empty by default.
    substation_assets: tuple[SubstationAssetConfig, ...] = ()
    # Simulated mobile units (trucks, D-31): mirrors `FleetConfig.mobile_units` (same manual
    # duplication as `substation_assets`), so `ogsim.scada` measures each simulated truck's own
    # single-hub bank. Only `simulate: true` entries get a bank; empty by default.
    mobile_units: tuple[MobileUnitConfig, ...] = ()
    # Utility grid-control link (D-34): the raw `grid_link` table, parsed by `ogsim.scada.grid_link`.
    # Empty (disabled) by default: L2 instructions then travel on MQTT only, as before.
    grid_link: Mapping[str, Any] = field(default_factory=dict)


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
        overload_clear_samples=int(raw.get("overload_clear_samples", defaults.overload_clear_samples)),
        overload_limit_max_duration_s=float(
            raw.get("overload_limit_max_duration_s", defaults.overload_limit_max_duration_s)
        ),
        history_tsv_path=str(raw.get("history_tsv_path", defaults.history_tsv_path)),
        base_load_kw_default=float(raw.get("base_load_kw_default", defaults.base_load_kw_default)),
        zone_blocks=_zone_blocks_from_raw(raw.get("zone_blocks", [])),
        substation_assets=_substation_assets_from_raw(raw.get("substation_assets", [])),
        mobile_units=_mobile_units_from_raw(raw.get("mobile_units", [])),
        grid_link=dict(raw.get("grid_link") or {}),
    )


__all__ = [
    "DEFAULT_FLEET_CONFIG_PATH",
    "DEFAULT_SCADA_CONFIG_PATH",
    "PRODUCTION_ENV",
    "PRODUCTION_TOPIC_ROOT",
    "FleetConfig",
    "MqttSettings",
    "ScadaConfig",
    "SubstationAssetConfig",
    "WorkspaceConfigError",
    "ZoneBlockConfig",
    "bank_topology",
    "is_production",
    "load_fleet_config",
    "load_scada_config",
    "load_yaml_file",
    "resolve_topic_root",
    "workspace_name",
]
