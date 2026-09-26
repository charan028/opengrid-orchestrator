"""opengrid.fleet.seed -- idempotent `og.hub`/`og.bank` topology loader (dispatch-live pass).

**Why this exists.** `og.hub`/`og.bank` were never seeded on a fresh database, so `opengrid.fleet`'s
twin (`load_topology()`) started up knowing zero hubs/banks. Every telemetry message from the
integration simulator's ~2,000 simulated hubs was then dropped as "unknown hub_id"
(`opengrid.fleet.ingest_telemetry`), so `GET /og/api/fleet/hubs` always reported 0 hubs online, and
nothing downstream (selector commitments, allocator grants, guardian verdicts, settlement) ever had real
capacity to work with (A2/A4/A5/A6/A8, `qa/merge-notes.md` section 9).

**Single source of truth.** The id scheme (`hub-00000`.."hub-01999"`, `bank-000`.."bank-039"`, hubs
distributed round-robin across banks and zones) must match exactly what `integration-sims/src/ogsim/
fleet/state.py::build_fleet_state` generates, or telemetry still won't line up. `opengrid` must never
import `ogsim` (BUILD.md S1's "share no code" rule, enforced by `tools/dupcheck.py`), so this module
reads `integration-sims/config/fleet.yaml` itself, read-only, as plain YAML data -- never the sim's
Python config loader -- and rebuilds the identical id/param scheme from the same numbers. If that file
is ever edited, re-running this loader (idempotent) picks the change up; if it's missing, this falls
back to the exact defaults `ogsim.common.config.FleetConfig` ships with, which is what a fresh checkout
actually runs.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import Bank, Hub
from opengrid.platform.config import Config

_DEFAULT_ZONES: tuple[str, ...] = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")

# Dual-unit homes (confirmed by Base, 2026-09-25): see `build_topology`'s docstring for the
# deterministic hub-selection rule shared word-for-word with `ogsim.fleet.state`.
_DUAL_UNIT_SHARE_DEFAULT: float = 0.2
_E_KWH_DUAL_UNIT_DEFAULT: float = 78.4
_P_KW_DUAL_UNIT_DEFAULT: float = 20.0

# Bank rating models a feeder segment (~50 homes), not a single distribution transformer
# (confirmed by Base, 2026-09-25).
_BANK_KVA_RATING_DEFAULT: float = 600.0


@dataclass(frozen=True, slots=True)
class SimFleetTopologyConfig:
    """The subset of `ogsim.common.config.FleetConfig` this loader needs to reproduce the sim's id
    scheme and per-hub/bank physical params. Field names/defaults are kept identical to that dataclass
    on purpose -- this is a read of the same numbers, not an independent guess."""

    hub_count: int = 2000
    bank_count: int = 40
    zones: tuple[str, ...] = _DEFAULT_ZONES
    # Base Power home battery, usable kWh (confirmed by Base, 2026-09-25).
    e_kwh_default: float = 39.2
    reserve_frac_default: float = 0.20
    # Base Power inverter, kW per battery unit (confirmed by Base, 2026-09-25).
    p_kw_default: float = 11.0
    # Share of homes with two battery units instead of one (confirmed by Base, 2026-09-25); see
    # `build_topology`'s docstring for the deterministic hub-selection rule.
    dual_unit_share: float = _DUAL_UNIT_SHARE_DEFAULT
    # Dual-unit home usable kWh (confirmed by Base, 2026-09-25).
    e_kwh_dual_unit: float = _E_KWH_DUAL_UNIT_DEFAULT
    # Dual-unit home inverter kW (confirmed by Base, 2026-09-25).
    p_kw_dual_unit: float = _P_KW_DUAL_UNIT_DEFAULT
    eta_c: float = 0.9487
    eta_d: float = 0.9487
    # Feeder segment (~50 homes), not a single distribution transformer.
    bank_kva_rating_default: float = _BANK_KVA_RATING_DEFAULT


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def resolve_sim_fleet_config_path(cfg: Config | None = None) -> Path:
    """`[fleet].sim_config_path` / `OG_FLEET_SIM_CONFIG` override first; else the sibling
    `integration-sims/config/fleet.yaml` next to this checkout's `orchestrator/` directory (derived from
    `OG_CONFIG`, so it resolves correctly regardless of the process's working directory -- the same class
    of bug this loader exists to avoid; `ogsim.fleet`'s own default path resolution has this bug today,
    see `qa/merge-notes.md`)."""
    override = os.environ.get("OG_FLEET_SIM_CONFIG") or (
        cfg.get("fleet.sim_config_path") if cfg is not None else None
    )
    if override:
        return Path(str(override))

    og_config = os.environ.get("OG_CONFIG")
    if og_config:
        # OG_CONFIG = .../orchestrator/config/orchestrator.toml -> repo root is two parents up. Not
        # `.resolve()`d: OG_CONFIG is already an absolute path in every real deployment (systemd units
        # set it explicitly), and resolving would needlessly follow symlinks / anchor to a drive.
        repo_root = Path(og_config).parents[2]
        return repo_root / "integration-sims" / "config" / "fleet.yaml"

    return Path("integration-sims/config/fleet.yaml")


def load_sim_fleet_topology_config(cfg: Config | None = None) -> SimFleetTopologyConfig:
    """Reads `integration-sims/config/fleet.yaml` (read-only) and falls back field-by-field to
    `SimFleetTopologyConfig`'s defaults (which match `ogsim.common.config.FleetConfig`'s own defaults) --
    a missing file or missing key never blocks seeding, it just uses the same numbers the sim would."""
    defaults = SimFleetTopologyConfig()
    raw = _load_yaml(resolve_sim_fleet_config_path(cfg))
    return SimFleetTopologyConfig(
        hub_count=int(raw.get("hub_count", defaults.hub_count)),
        bank_count=int(raw.get("bank_count", defaults.bank_count)),
        zones=tuple(raw.get("zones", defaults.zones)) or defaults.zones,
        e_kwh_default=float(raw.get("e_kwh_default", defaults.e_kwh_default)),
        reserve_frac_default=float(raw.get("reserve_frac_default", defaults.reserve_frac_default)),
        p_kw_default=float(raw.get("p_kw_default", defaults.p_kw_default)),
        dual_unit_share=float(raw.get("dual_unit_share", defaults.dual_unit_share)),
        e_kwh_dual_unit=float(raw.get("e_kwh_dual_unit", defaults.e_kwh_dual_unit)),
        p_kw_dual_unit=float(raw.get("p_kw_dual_unit", defaults.p_kw_dual_unit)),
        eta_c=float(raw.get("eta_c", defaults.eta_c)),
        eta_d=float(raw.get("eta_d", defaults.eta_d)),
        bank_kva_rating_default=float(raw.get("bank_kva_rating_default", defaults.bank_kva_rating_default)),
    )


@dataclass(frozen=True, slots=True)
class Topology:
    hubs: tuple[Hub, ...]
    banks: tuple[Bank, ...]


def _is_dual_unit(index: int, dual_unit_share: float) -> bool:
    """Dual-unit rule (must match ogsim.fleet.state exactly): hub index i (hub-{i:05d}) is dual-unit
    iff floor((i + 1) * dual_unit_share) > floor(i * dual_unit_share), which selects exactly
    floor(hub_count * dual_unit_share) hubs, deterministically and evenly spread across i in
    range(hub_count)."""
    return math.floor((index + 1) * dual_unit_share) > math.floor(index * dual_unit_share)


def build_topology(config: SimFleetTopologyConfig) -> Topology:
    """Pure (no I/O): reproduces `ogsim.fleet.state.build_fleet_state`'s id/grouping scheme exactly --
    `hub-{i:05d}` for `i` in `range(hub_count)`, `bank-{i % bank_count:03d}`, zone `zones[i % len(zones)]`
    -- so every id this generates is one the simulator will actually publish telemetry for. Dual-unit
    hub selection uses `_is_dual_unit`, identical to `ogsim.fleet.state`'s vectorized rule.
    """
    n_zones = len(config.zones)

    hub_zone_by_bank: dict[str, dict[str, int]] = {}
    hubs = []
    for i in range(config.hub_count):
        hub_id = f"hub-{i:05d}"
        bank_id = f"bank-{i % config.bank_count:03d}"
        zone = config.zones[i % n_zones]
        dual_unit = _is_dual_unit(i, config.dual_unit_share)
        e_kwh = config.e_kwh_dual_unit if dual_unit else config.e_kwh_default
        p_kw = config.p_kw_dual_unit if dual_unit else config.p_kw_default
        r_kwh = e_kwh * config.reserve_frac_default
        hubs.append(
            Hub(
                hub_id=hub_id,
                bank_id=bank_id,
                zone=zone,
                e_kwh=e_kwh,
                r_kwh=r_kwh,
                p_kw=p_kw,
                eta_c=config.eta_c,
                eta_d=config.eta_d,
            )
        )
        hub_zone_by_bank.setdefault(bank_id, {}).setdefault(zone, 0)
        hub_zone_by_bank[bank_id][zone] += 1

    banks = []
    for b in range(config.bank_count):
        bank_id = f"bank-{b:03d}"
        # A bank's own `zone` column has no independent source in the sim (hubs cycle zones by hub
        # index, independently of bank grouping) -- the bank's majority hub zone is the closest honest
        # single answer, deterministic via (count desc, zone asc) so re-seeding never flips it.
        zone_counts = hub_zone_by_bank.get(bank_id, {})
        zone = min(zone_counts, key=lambda z: (-zone_counts[z], z)) if zone_counts else config.zones[0]
        banks.append(
            Bank(
                bank_id=bank_id,
                zone=zone,
                kva_rating=config.bank_kva_rating_default,
                reserve_kva=0.0,
            )
        )
    return Topology(hubs=tuple(hubs), banks=tuple(banks))


_UPSERT_BANK_SQL = """
INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva, feeder_id)
VALUES (%(bank_id)s, %(zone)s, %(kva_rating)s, %(reserve_kva)s, %(feeder_id)s)
ON CONFLICT (bank_id) DO UPDATE SET
    zone = EXCLUDED.zone, kva_rating = EXCLUDED.kva_rating, reserve_kva = EXCLUDED.reserve_kva,
    feeder_id = EXCLUDED.feeder_id
"""

_UPSERT_HUB_SQL = """
INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d, lat, lon)
VALUES (%(hub_id)s, %(bank_id)s, %(zone)s, %(e_kwh)s, %(r_kwh)s, %(p_kw)s, %(eta_c)s, %(eta_d)s,
        %(lat)s, %(lon)s)
ON CONFLICT (hub_id) DO UPDATE SET
    bank_id = EXCLUDED.bank_id, zone = EXCLUDED.zone, e_kwh = EXCLUDED.e_kwh, r_kwh = EXCLUDED.r_kwh,
    p_kw = EXCLUDED.p_kw, eta_c = EXCLUDED.eta_c, eta_d = EXCLUDED.eta_d, lat = EXCLUDED.lat,
    lon = EXCLUDED.lon
"""


@dataclass(frozen=True, slots=True)
class SeedResult:
    banks_upserted: int
    hubs_upserted: int


async def seed_topology(pool: AsyncConnectionPool, topology: Topology) -> SeedResult:
    """Idempotent upsert of `topology` into `og.bank`/`og.hub`. Banks are written first: `og.hub` has no
    FK to `og.bank` in `migrations/0001_init.sql`, but writing banks first still means a concurrent
    reader never observes a hub whose bank row doesn't exist yet."""
    async with pool.connection() as conn, conn.cursor() as cur:
        for bank in topology.banks:
            await cur.execute(
                _UPSERT_BANK_SQL,
                {
                    "bank_id": bank.bank_id,
                    "zone": bank.zone,
                    "kva_rating": bank.kva_rating,
                    "reserve_kva": bank.reserve_kva,
                    "feeder_id": bank.feeder_id,
                },
            )
        for hub in topology.hubs:
            await cur.execute(
                _UPSERT_HUB_SQL,
                {
                    "hub_id": hub.hub_id,
                    "bank_id": hub.bank_id,
                    "zone": hub.zone,
                    "e_kwh": hub.e_kwh,
                    "r_kwh": hub.r_kwh,
                    "p_kw": hub.p_kw,
                    "eta_c": hub.eta_c,
                    "eta_d": hub.eta_d,
                    "lat": hub.lat,
                    "lon": hub.lon,
                },
            )
        await conn.commit()
    return SeedResult(banks_upserted=len(topology.banks), hubs_upserted=len(topology.hubs))


async def run_seed(cfg: Config, pool: AsyncConnectionPool) -> SeedResult:
    """Full pipeline: resolve the sim's config, build the topology, upsert it. The single entry point
    both the CLI and the admin API call."""
    sim_cfg = load_sim_fleet_topology_config(cfg)
    topology = build_topology(sim_cfg)
    return await seed_topology(pool, topology)


def _cli() -> int:
    import asyncio

    from opengrid.platform.config import load_config
    from opengrid.platform.db import make_pool
    from opengrid.platform.log import configure_logging

    async def _main() -> int:
        configure_logging("fleet-seed")
        cfg = load_config(os.environ.get("OG_CONFIG"))
        pool = await make_pool(cfg)
        try:
            result = await run_seed(cfg, pool)
        finally:
            await pool.close()
        print(f"seeded {result.banks_upserted} banks, {result.hubs_upserted} hubs")
        return 0

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(_cli())
