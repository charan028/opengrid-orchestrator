"""ogsim.fleet.state -- vectorized per-hub state for the whole fleet.

One `FleetState` holds every hub's physics/command/anomaly state as numpy
arrays (not per-hub Python objects), so a 2,000-10,000 hub tick is a
handful of array ops instead of a Python loop (02b §4.1/§5.1).

Dual-unit rule (must match ogsim.fleet.state / opengrid.fleet.seed exactly): hub index i
(hub-{i:05d}) is dual-unit iff floor((i + 1) * dual_unit_share) > floor(i * dual_unit_share),
which selects exactly floor(hub_count * dual_unit_share) hubs, deterministically and evenly
spread across i in range(hub_count).
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


def _dual_unit_mask(hub_count: int, dual_unit_share: float) -> np.ndarray:
    """Vectorized form of the dual-unit rule documented in this module's docstring: hub index i is
    dual-unit iff floor((i + 1) * dual_unit_share) > floor(i * dual_unit_share)."""
    i = np.arange(hub_count)
    return np.floor((i + 1) * dual_unit_share) > np.floor(i * dual_unit_share)


def build_fleet_state(config: FleetConfig, rng: np.random.Generator) -> FleetState:
    """Allocates a `FleetState` for `config.hub_count` hubs distributed
    evenly across `config.bank_count` banks and `config.zones` (02b §4.1)."""
    n = config.hub_count
    hub_ids = [f"hub-{i:05d}" for i in range(n)]
    bank_ids = [f"bank-{(i % config.bank_count):03d}" for i in range(n)]
    zones = [config.zones[i % len(config.zones)] for i in range(n)]

    soc_frac = rng.uniform(0.4, 0.9, size=n)
    dual_unit = _dual_unit_mask(n, config.dual_unit_share)
    e_kwh = np.where(dual_unit, config.e_kwh_dual_unit, config.e_kwh_default)
    r_kwh = e_kwh * config.reserve_frac_default
    p_kw_limit = np.where(dual_unit, config.p_kw_dual_unit, config.p_kw_default)
    soc_kwh = np.clip(soc_frac * e_kwh, r_kwh, e_kwh)

    state = FleetState(
        hub_ids=hub_ids,
        bank_ids=bank_ids,
        zones=zones,
        soc_kwh=soc_kwh,
        e_kwh=e_kwh,
        r_kwh=r_kwh,
        p_kw_limit=p_kw_limit,
        eta_c=np.full(n, config.eta_c),
        eta_d=np.full(n, config.eta_d),
        self_discharge_kwh_per_h=np.full(n, config.self_discharge_kwh_per_h),
        pv_capacity_kw=rng.uniform(0.0, 5.0, size=n),
        phase_offset_s=rng.uniform(0.0, 3600.0, size=n),
        p_kw_commanded=np.zeros(n),
        p_kw_applied=np.zeros(n),
        last_epoch=np.full(n, -1, dtype=np.int64),
        last_seq=np.full(n, -1, dtype=np.int64),
        lease_expires_at=np.zeros(n),
        local_autonomy=np.ones(n, dtype=bool),
        holding_after_expiry=np.zeros(n, dtype=bool),
        health=[HEALTH_ONLINE] * n,
        fault_code=[None] * n,
        offline=np.zeros(n, dtype=bool),
        hub_index={hub_id: i for i, hub_id in enumerate(hub_ids)},
    )
    return state
