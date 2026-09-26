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

from ogsim.common.config import FleetConfig

HEALTH_ONLINE = "online"
HEALTH_STALE = "stale"
HEALTH_FAULT = "fault"


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
    scheme, so this is proven by a shared JSON fixture in both test suites, not by import)."""
    n = config.hub_count
    hub_ids, bank_ids, zones = _segment_ids_and_zones(n, 0, 0, config.bank_count, list(config.zones))
    dual_unit = _dual_unit_mask(n, config.bank_count, config.dual_unit_share)
    physics = _segment_physics(n, dual_unit, config, rng)

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

        hub_ids += block_hub_ids
        bank_ids += block_bank_ids
        zones += block_zones
        for key, value in block_physics.items():
            physics[key] = np.concatenate([physics[key], value])

        hub_offset += block_n
        bank_offset += block.banks

    total = len(hub_ids)
    state = FleetState(
        hub_ids=hub_ids,
        bank_ids=bank_ids,
        zones=zones,
        health=[HEALTH_ONLINE] * total,
        fault_code=[None] * total,
        hub_index={hub_id: i for i, hub_id in enumerate(hub_ids)},
        **physics,
    )
    return state
