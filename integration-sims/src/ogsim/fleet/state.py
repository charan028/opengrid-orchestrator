"""ogsim.fleet.state -- vectorized per-hub state for the whole fleet.

One `FleetState` holds every hub's physics/command/anomaly state as numpy
arrays (not per-hub Python objects), so a 2,000-10,000 hub tick is a
handful of array ops instead of a Python loop (02b §4.1/§5.1).

Bank assignment rule (must match opengrid.fleet.seed exactly): hub index i belongs to bank `i %
bank_count` (round-robin) -- banks are feeder segments, so every hub on one bank must share that
bank's single zone; since `zones[i % len(zones)]`'s period (4) divides `bank_count` (40) at the
confirmed defaults, round-robin keeps every bank single-zone.

Dual-unit rule (must match ogsim.fleet.state / opengrid.fleet.seed exactly): hub index i is
dual-unit iff, letting k = i // bank_count (the hub's rank *within its own bank*), floor((k + 1) *
dual_unit_share) > floor(k * dual_unit_share) -- the same deterministic, evenly-spread selection as
a plain hub-index-based rule, but offset per bank instead of fleet-wide: every bank's k ranges over
the same 0..49, so every bank gets the same floor(50 * dual_unit_share) = 10 dual-unit hubs. Indexing
directly by i instead of by k would select fleet-wide index residues (e.g. i % 5 == 4 at the default
0.2 share), which -- combined with `i % bank_count` bank assignment and bank_count=40 being a
multiple of 5 -- always landed on the same 8 of 40 banks (1,000 kW of dual-unit inverter capacity on
one 600 kVA bank) and never on the other 32.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ogsim.common.config import (
    SUBSTATION_ETA_DEFAULT,
    SUBSTATION_RESERVE_FRAC_DEFAULT,
    FleetConfig,
    SubstationAssetConfig,
)

HEALTH_ONLINE = "online"
HEALTH_STALE = "stale"
HEALTH_FAULT = "fault"

# Deterministic per-hub lat/lon (owner UI request, 2026-09-26, #19). Must match
# `opengrid.fleet.seed`'s independent copy of these exact constants and formula (BUILD.md S1 "share no
# code") -- see that module's `hub_lat_lon` docstring for the full explanation and the city/region
# each zone centers on.
ZONE_GEO_CENTERS: dict[str, tuple[float, float]] = {
    "LZ_NORTH": (32.7767, -96.7970),
    "LZ_HOUSTON": (29.7604, -95.3698),
    "LZ_SOUTH": (29.4241, -98.4936),
    "LZ_WEST": (31.9973, -102.0779),
    "LZ_AEN": (30.2672, -97.7431),
    "LZ_CPS": (29.4241, -98.4936),
    "LZ_LCRA": (30.5000, -98.3000),
    "LZ_RAYBN": (32.8700, -95.7500),
}
_DEFAULT_ZONE_GEO_CENTER: tuple[float, float] = (31.0000, -100.0000)
_GEO_JITTER_SPREAD_DEG: float = 0.35


def hub_lat_lon(index: int, zone: str) -> tuple[float, float]:
    """Identical formula to `opengrid.fleet.seed.hub_lat_lon` -- see its docstring."""
    center_lat, center_lon = ZONE_GEO_CENTERS.get(zone, _DEFAULT_ZONE_GEO_CENTER)
    frac_lat = ((index * 9301 + 49297) % 233280) / 233280.0
    frac_lon = ((index * 134775813 + 40503) % 1000003) / 1000003.0
    lat = center_lat + (frac_lat - 0.5) * _GEO_JITTER_SPREAD_DEG
    lon = center_lon + (frac_lon - 0.5) * _GEO_JITTER_SPREAD_DEG
    return lat, lon


def _lat_lon_arrays(hub_offset: int, n: int, zones: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized-by-loop `hub_lat_lon` for a contiguous segment of `n` hubs starting at global index
    `hub_offset` (build time only, not per-tick -- a plain Python loop is fine at fleet-build scale)."""
    lat = np.empty(n)
    lon = np.empty(n)
    for i in range(n):
        lat[i], lon[i] = hub_lat_lon(hub_offset + i, zones[i])
    return lat, lon


@dataclass
class FleetState:
    """Struct-of-arrays state for `hub_count` hubs, indexed 0..hub_count-1."""

    hub_ids: list[str]
    bank_ids: list[str]
    zones: list[str]

    soc_kwh: np.ndarray
    e_kwh: np.ndarray
    r_kwh: np.ndarray
    p_kw_limit: np.ndarray
    eta_c: np.ndarray
    eta_d: np.ndarray
    self_discharge_kwh_per_h: np.ndarray
    pv_capacity_kw: np.ndarray
    phase_offset_s: np.ndarray

    p_kw_commanded: np.ndarray  # last accepted market setpoint request (pre-clip)
    p_kw_applied: np.ndarray  # last applied (clipped) setpoint, for ack/telemetry

    last_epoch: np.ndarray  # int64, -1 = never accepted
    last_seq: np.ndarray  # int64

    lease_expires_at: np.ndarray  # unix epoch float; 0 = no lease
    local_autonomy: np.ndarray  # bool
    holding_after_expiry: np.ndarray  # bool

    health: list[str] = field(default_factory=list)
    fault_code: list[str | None] = field(default_factory=list)
    offline: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))

    # Discharge-flow-limit telemetry fields (09-optimizer-dispatcher-update.md S1.9/G11), recomputed
    # every tick by `ogsim.fleet.runtime.FleetEngine.tick` and published by `telemetry_messages`.
    home_load_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))  # >= 0, before PV
    pv_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))  # >= 0
    meter_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))  # site meter, +import (F2)
    cell_temp_c: np.ndarray = field(default_factory=lambda: np.zeros(0))
    p_dis_max_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))  # F1, SoC/temp-derated
    p_ch_max_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))  # F1, SoC/temp-derated
    # F5 peak budget B_i (kW*s above P_cont); constant per hub (set at build time from config, not
    # recomputed per tick) -- "peak = continuous" (OQ-10) means this is 0.0 until Base confirms F5.
    peak_power_budget_kws: np.ndarray = field(default_factory=lambda: np.zeros(0))

    # Deterministic per-hub geography (owner UI request, 2026-09-26, #19): fixed at build time, never
    # recomputed per tick, published in telemetry so the UI-API agent can map the fleet.
    lat_deg: np.ndarray = field(default_factory=lambda: np.zeros(0))
    lon_deg: np.ndarray = field(default_factory=lambda: np.zeros(0))

    # Charging-source split (owner decision D-28, 2026-09-26): recomputed every tick, PV surplus (after
    # home load) charges first, the rest from the grid. Both >= 0; both 0 when not charging.
    charge_pv_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))
    charge_grid_kw: np.ndarray = field(default_factory=lambda: np.zeros(0))

    hub_index: dict[str, int] = field(default_factory=dict)

    def index_of(self, hub_id: str) -> int | None:
        return self.hub_index.get(hub_id)


def _dual_unit_mask(hub_count: int, bank_count: int, dual_unit_share: float) -> np.ndarray:
    """Vectorized form of the dual-unit rule documented in this module's docstring: hub index i is
    dual-unit iff, with k = i // bank_count, floor((k + 1) * dual_unit_share) > floor(k *
    dual_unit_share)."""
    i = np.arange(hub_count)
    k = i // bank_count
    return np.floor((k + 1) * dual_unit_share) > np.floor(k * dual_unit_share)


def _segment_ids_and_zones(
    n: int, hub_offset: int, bank_offset: int, bank_count: int, zones: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """`(hub_ids, bank_ids, zone_per_hub)` for one contiguous segment of `n` hubs starting at
    `hub_offset`, round-robin across `bank_count` banks starting at `bank_offset` (`zones[i %
    len(zones)]` for hub-index `i` *within the segment*). The base fleet and each extra zone block
    (`ZoneBlockConfig`, a single-zone block passes `zones=[block.zone]`) both call this."""
    hub_ids = [f"hub-{hub_offset + i:05d}" for i in range(n)]
    bank_ids = [f"bank-{bank_offset + (i % bank_count):03d}" for i in range(n)]
    hub_zones = [zones[i % len(zones)] for i in range(n)]
    return hub_ids, bank_ids, hub_zones


def _segment_physics(
    n: int,
    dual_unit: np.ndarray,
    config: FleetConfig,
    rng: np.random.Generator,
) -> dict[str, np.ndarray]:
    """The physics/command/anomaly-state arrays for one segment of `n` hubs, given its dual-unit mask
    -- shared by the base fleet and every extra zone block so their per-hub distributions (SoC draw,
    PV capacity, phase offset) are drawn the same way."""
    soc_frac = rng.uniform(0.4, 0.9, size=n)
    e_kwh = np.where(dual_unit, config.e_kwh_dual_unit, config.e_kwh_default)
    r_kwh = e_kwh * config.reserve_frac_default
    p_kw_limit = np.where(dual_unit, config.p_kw_dual_unit, config.p_kw_default)
    soc_kwh = np.clip(soc_frac * e_kwh, r_kwh, e_kwh)
    return {
        "soc_kwh": soc_kwh,
        "e_kwh": e_kwh,
        "r_kwh": r_kwh,
        "p_kw_limit": p_kw_limit,
        "eta_c": np.full(n, config.eta_c),
        "eta_d": np.full(n, config.eta_d),
        "self_discharge_kwh_per_h": np.full(n, config.self_discharge_kwh_per_h),
        "pv_capacity_kw": rng.uniform(0.0, 5.0, size=n),
        "phase_offset_s": rng.uniform(0.0, 3600.0, size=n),
        "p_kw_commanded": np.zeros(n),
        "p_kw_applied": np.zeros(n),
        "last_epoch": np.full(n, -1, dtype=np.int64),
        "last_seq": np.full(n, -1, dtype=np.int64),
        "lease_expires_at": np.zeros(n),
        "local_autonomy": np.ones(n, dtype=bool),
        "holding_after_expiry": np.zeros(n, dtype=bool),
        "offline": np.zeros(n, dtype=bool),
    }


def _substation_segment(
    assets: tuple[SubstationAssetConfig, ...], rng: np.random.Generator
) -> tuple[list[str], list[str], list[str], dict[str, np.ndarray]]:
    """Enabled substation battery-set assets (D11 `SUBSTATION_BESS`, `SubstationAssetConfig`'s
    docstring), each as its OWN one-hub "bank" -- `asset_id` becomes the hub id directly (not
    `hub-NNNNN`-numbered, since it isn't part of the round-robin home fleet) and `bank-<asset_id>` its
    dedicated bank, so it never collides with a home hub/bank id and never shares a bank (hence a K4
    kVA rating) with any home. No dual-unit concept applies; physics/command/lease/stop machinery is
    identical to a home hub (this is exactly why this function returns the same physics-dict shape
    `_segment_physics` does, only built directly from each asset's own rated_mw/duration_h instead of
    from a fleet-wide config)."""
    enabled = [a for a in assets if a.enabled]
    n = len(enabled)
    hub_ids = [a.asset_id for a in enabled]
    bank_ids = [f"bank-{a.asset_id}" for a in enabled]
    zones = [a.zone for a in enabled]
    p_kw_limit = np.array([a.rated_mw * 1000.0 for a in enabled])
    e_kwh = np.array([a.rated_mw * 1000.0 * a.duration_h for a in enabled])
    r_kwh = e_kwh * SUBSTATION_RESERVE_FRAC_DEFAULT
    soc_frac = rng.uniform(0.4, 0.9, size=n)
    soc_kwh = np.clip(soc_frac * e_kwh, r_kwh, e_kwh)
    physics = {
        "soc_kwh": soc_kwh,
        "e_kwh": e_kwh,
        "r_kwh": r_kwh,
        "p_kw_limit": p_kw_limit,
        "eta_c": np.full(n, SUBSTATION_ETA_DEFAULT),
        "eta_d": np.full(n, SUBSTATION_ETA_DEFAULT),
        "self_discharge_kwh_per_h": np.zeros(n),
        "pv_capacity_kw": np.zeros(n),  # no PV on a substation asset
        "phase_offset_s": np.zeros(n),
        "p_kw_commanded": np.zeros(n),
        "p_kw_applied": np.zeros(n),
        "last_epoch": np.full(n, -1, dtype=np.int64),
        "last_seq": np.full(n, -1, dtype=np.int64),
        "lease_expires_at": np.zeros(n),
        "local_autonomy": np.ones(n, dtype=bool),
        "holding_after_expiry": np.zeros(n, dtype=bool),
        "offline": np.zeros(n, dtype=bool),
    }
    return hub_ids, bank_ids, zones, physics


def build_fleet_state(config: FleetConfig, rng: np.random.Generator) -> FleetState:
    """Allocates a `FleetState` for `config.hub_count` hubs distributed round-robin across
    `config.bank_count` banks and `config.zones` (02b §4.1) -- banks are feeder segments and stay
    single-zone (see this module's docstring) -- plus, in config order, every enabled entry in
    `config.zone_blocks` (build phase 2026-09-26: Austin Energy/CPS Energy, `ZoneBlockConfig`'s
    docstring). Each enabled block is a further contiguous, single-zone segment appended after every
    id already assigned so far (base fleet first, then each enabled block in order); a disabled block
    is skipped entirely and reserves no ids, so enabling/disabling a block never renumbers another
    one. `opengrid.fleet.seed.build_topology` must produce the identical id/zone/dual-unit scheme for
    both the base fleet and every block (BUILD.md S1: the two packages share no code, only the id
    scheme, so this is proven by a shared JSON fixture in both test suites, not by import).

    Finally appends every enabled `config.substation_assets` entry (D11 `SUBSTATION_BESS`, build phase
    2026-09-26) as its own one-hub "bank" (`_substation_segment`) -- `opengrid.fleet.seed` does NOT
    mirror these: substation assets are a new `og.asset` model (the MARKET-MODEL agent's), not part of
    `og.hub`/`og.bank`'s home-fleet topology, so there is nothing to keep in sync here."""
    n = config.hub_count
    hub_ids, bank_ids, zones = _segment_ids_and_zones(n, 0, 0, config.bank_count, list(config.zones))
    dual_unit = _dual_unit_mask(n, config.bank_count, config.dual_unit_share)
    physics = _segment_physics(n, dual_unit, config, rng)
    lat_deg, lon_deg = _lat_lon_arrays(0, n, zones)

    hub_offset, bank_offset = n, config.bank_count
    for block in config.zone_blocks:
        if not block.enabled:
            continue
        block_n = block.banks * block.homes_per_bank
        block_hub_ids, block_bank_ids, block_zones = _segment_ids_and_zones(
            block_n, hub_offset, bank_offset, block.banks, [block.zone]
        )
        block_dual_unit = _dual_unit_mask(block_n, block.banks, config.dual_unit_share)
        block_physics = _segment_physics(block_n, block_dual_unit, config, rng)
        block_lat, block_lon = _lat_lon_arrays(hub_offset, block_n, block_zones)

        hub_ids += block_hub_ids
        bank_ids += block_bank_ids
        zones += block_zones
        for key, value in block_physics.items():
            physics[key] = np.concatenate([physics[key], value])
        lat_deg = np.concatenate([lat_deg, block_lat])
        lon_deg = np.concatenate([lon_deg, block_lon])

        hub_offset += block_n
        bank_offset += block.banks

    sub_hub_ids, sub_bank_ids, sub_zones, sub_physics = _substation_segment(config.substation_assets, rng)
    hub_ids += sub_hub_ids
    bank_ids += sub_bank_ids
    zones += sub_zones
    for key, value in sub_physics.items():
        physics[key] = np.concatenate([physics[key], value])
    # Substation assets aren't numeric-indexed (their id is a configured asset_id, not hub-NNNNN), so
    # each is simply placed at its own zone's center (index 0's jitter) -- see `_substation_segment`'s
    # docstring for why there's no home-fleet-style numbering to hash here.
    sub_lat_lon = [hub_lat_lon(0, zone) for zone in sub_zones]
    lat_deg = np.concatenate([lat_deg, np.array([ll[0] for ll in sub_lat_lon])])
    lon_deg = np.concatenate([lon_deg, np.array([ll[1] for ll in sub_lat_lon])])

    total = len(hub_ids)
    p_kw_limit = physics["p_kw_limit"]
    state = FleetState(
        hub_ids=hub_ids,
        bank_ids=bank_ids,
        zones=zones,
        health=[HEALTH_ONLINE] * total,
        fault_code=[None] * total,
        hub_index={hub_id: i for i, hub_id in enumerate(hub_ids)},
        # Placeholders only: `FleetEngine.tick` recomputes all of these every tick before the first
        # telemetry publish (`run_fleet` always ticks before it publishes), so what they hold here
        # never reaches the wire. `p_dis_max_kw`/`p_ch_max_kw` start at the full rating (a build-time
        # SoC/temp derate has not run yet); `peak_power_budget_kws` is set once, from config, and never
        # recomputed per tick (S1.9 F5 is a per-hub constant, not a live measurement).
        home_load_kw=np.zeros(total),
        pv_kw=np.zeros(total),
        meter_kw=np.zeros(total),
        cell_temp_c=np.zeros(total),
        p_dis_max_kw=p_kw_limit.copy(),
        p_ch_max_kw=p_kw_limit.copy(),
        peak_power_budget_kws=p_kw_limit * config.peak_power_budget_s_default,
        lat_deg=lat_deg,
        lon_deg=lon_deg,
        charge_pv_kw=np.zeros(total),
        charge_grid_kw=np.zeros(total),
        **physics,
    )
    return state
