"""opengrid.fleet.seed -- idempotent `og.hub`/`og.bank` topology loader (dispatch-live pass).

**Why this exists.** `og.hub`/`og.bank` were never seeded on a fresh database, so `opengrid.fleet`'s
twin (`load_topology()`) started up knowing zero hubs/banks. Every telemetry message from the
integration simulator's ~2,000 simulated hubs was then dropped as "unknown hub_id"
(`opengrid.fleet.ingest_telemetry`), so `GET /og/api/fleet/hubs` always reported 0 hubs online, and
nothing downstream (selector commitments, allocator grants, guardian verdicts, settlement) ever had real
capacity to work with (A2/A4/A5/A6/A8, `qa/merge-notes.md` section 9).

**Single source of truth.** The id scheme (`hub-00000`.."hub-01999"`, `bank-000`.."bank-039"`, hubs
distributed round-robin across banks and zones -- banks are feeder segments and must stay single-zone,
so the dual-unit rule is offset per-bank instead, see `_is_dual_unit`) must match exactly what
`integration-sims/src/ogsim/
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

# K4/G-06: a feeder groups this many consecutive banks (feeder segments) of ONE load zone, so the
# per-feeder firm-event ramp ceiling has a feeder to bind to. 40 banks x 4 zones -> 8 feeders of 5 banks
# (3 MVA of segment rating each). Override with `[fleet].banks_per_feeder`.
BANKS_PER_FEEDER_DEFAULT: int = 5


@dataclass(frozen=True, slots=True)
class ZoneBlockConfig:
    """Mirrors `ogsim.common.config.ZoneBlockConfig` field-for-field (BUILD.md S1: the two packages
    share no code, so this is a second, independent definition of the same shape, not an import). One
    optional extra load-zone block (build phase 2026-09-26: Austin Energy `LZ_AEN`/CPS Energy
    `LZ_CPS`), appended after `hub-{hub_count-1}`/`bank-{bank_count-1}` (or after the previous enabled
    block) in `zone_blocks` list order; disabled (`enabled=False`) by default, and a disabled block
    reserves no ids at all, so the base fleet's ids never move regardless of how many blocks are
    defined-but-off."""

    zone: str
    banks: int
    homes_per_bank: int
    enabled: bool = False


def feeder_id_for(zone: str, rank_in_zone: int, banks_per_feeder: int) -> str:
    """`feeder-<zone>-<NN>`: the `rank_in_zone`-th bank of `zone` (0-based, by bank index) belongs to
    feeder `rank_in_zone // banks_per_feeder`. The live backfill SQL in the guardian safety report
    derives the same ids with `row_number() OVER (PARTITION BY zone ORDER BY bank_id)`."""
    if banks_per_feeder < 1:
        raise ValueError("banks_per_feeder must be >= 1")
    return f"feeder-{zone}-{rank_in_zone // banks_per_feeder:02d}"


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
    # Optional extra load-zone blocks (Austin Energy/CPS Energy, build phase 2026-09-26); empty/all-
    # disabled by default, so the base fleet is unchanged (see `ZoneBlockConfig`'s docstring).
    zone_blocks: tuple[ZoneBlockConfig, ...] = ()


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


def _zone_blocks_from_raw(raw_blocks: Any) -> tuple[ZoneBlockConfig, ...]:
    """Mirrors `ogsim.common.config._zone_blocks_from_raw` (BUILD.md S1: independently re-implemented,
    not imported). A missing key, non-list value, or non-mapping entry parses to "no extra blocks"."""
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
        zone_blocks=_zone_blocks_from_raw(raw.get("zone_blocks", [])),
    )


@dataclass(frozen=True, slots=True)
class Topology:
    hubs: tuple[Hub, ...]
    banks: tuple[Bank, ...]


def _is_dual_unit(index: int, bank_count: int, dual_unit_share: float) -> bool:
    """Dual-unit rule (must match `ogsim.fleet.state` exactly, and the bank-assignment rule below):
    hub index `i` belongs to bank `i % bank_count` (round-robin, single-zone banks -- see
    `build_topology`'s docstring), and within a bank the hubs are `i = b, b + bank_count, b +
    2*bank_count, ...` for `k = i // bank_count = 0, 1, 2, ...`. Hub `i` is dual-unit iff
    `floor((k + 1) * dual_unit_share) > floor(k * dual_unit_share)` -- the same deterministic,
    evenly-spread selection rule as before, applied to `k` (the hub's *rank within its own bank*)
    instead of to `i` (its rank in the whole fleet), so every bank's `k` ranges over the identical
    `0..49` and gets the identical `floor(50 * dual_unit_share)` = 10 dual-unit hubs.

    This replaces indexing directly by `i`: at the default `dual_unit_share=0.2` (period 5), `i %
    bank_count == i`'s bank, and `k % 5 == 4` selects one hub in every run of 5 *within* that bank
    (`k=4,9,14,...,49`) -- 10 per bank, every bank -- instead of selecting hubs whose *fleet-wide*
    index was congruent to 4 mod 5, which (since `bank_count=40` is a multiple of 5) always landed on
    the same 8 of 40 banks (1,000 kW of dual-unit inverter capacity on one 600 kVA bank) and never on
    the other 32."""
    k = index // bank_count
    return math.floor((k + 1) * dual_unit_share) > math.floor(k * dual_unit_share)


# Real ERCOT load-zone centroids (the same public geometry the simulators' map uses). A zone is huge;
# real homes cluster around its metro, so a hub is scattered inside `_HUB_SCATTER_KM` of the centroid.
_ZONE_CENTROIDS: dict[str, tuple[float, float]] = {
    "LZ_NORTH": (32.78, -96.80),  # North Central (Dallas-Fort Worth)
    "LZ_SOUTH": (29.90, -98.20),  # South Central (San Antonio - Austin)
    "LZ_HOUSTON": (29.76, -95.37),  # Coast (Houston)
    "LZ_WEST": (32.09, -100.44),  # West
    "LZ_AEN": (30.27, -97.74),  # Austin Energy
    "LZ_CPS": (29.42, -98.49),  # CPS Energy (San Antonio)
    "LZ_RAYBN": (33.20, -96.10),  # Rayburn Country
    "LZ_LCRA": (30.55, -98.39),  # LCRA
}
_HUB_SCATTER_KM = 45.0
_KM_PER_DEGREE_LAT = 111.0


def _hash_unit(text: str) -> float:
    """A stable hash of `text` in [0, 1). FNV-1a plus murmur3's finalizer -- the avalanche step matters:
    hub ids are sequential, and without it neighbouring ids produce neighbouring angles and the fleet
    collapses onto a line instead of filling the zone."""
    h = 2166136261
    for char in text:
        h = ((h ^ ord(char)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= h >> 16
    return h / 0x100000000


def hub_coordinates(hub_id: str, zone: str) -> tuple[float | None, float | None]:
    """Where this hub sits, as a (lat, lon) pair -- deterministic, so a hub never moves between seeds.

    The simulated fleet has no real street addresses, so a hub is placed at a stable pseudo-random point
    within ~45 km of its *real* load-zone centroid, the same way the simulators' reference map scatters
    homes inside a real ZIP. The zone is real; the point inside it is not, and nothing downstream should
    treat it as a surveyed location. Replace this with the real installation coordinate the moment the
    fleet carries one -- every consumer already reads `og.hub.lat`/`lon`.

    An unknown zone yields `(None, None)` rather than a guess.
    """
    centre = _ZONE_CENTROIDS.get(zone)
    if centre is None:
        return None, None
    angle = _hash_unit(hub_id) * 2 * math.pi
    radius_km = _HUB_SCATTER_KM * math.sqrt(_hash_unit(f"{hub_id}:r"))
    lat = centre[0] + (radius_km / _KM_PER_DEGREE_LAT) * math.cos(angle)
    lon = centre[1] + (radius_km / (_KM_PER_DEGREE_LAT * math.cos(math.radians(centre[0])))) * math.sin(angle)
    return round(lat, 6), round(lon, 6)


def build_topology(
    config: SimFleetTopologyConfig, *, banks_per_feeder: int = BANKS_PER_FEEDER_DEFAULT
) -> Topology:
    """Pure (no I/O): reproduces `ogsim.fleet.state.build_fleet_state`'s id/grouping scheme exactly --
    `hub-{i:05d}` for `i` in `range(hub_count)`, `bank-{i % bank_count:03d}` (round-robin -- banks are
    feeder segments, so every hub on a bank must share that bank's one zone; `zones[i % len(zones)]`
    cycles with a period that divides `bank_count` at the confirmed defaults, so this keeps every bank
    single-zone) -- so every id this generates is one the simulator will actually publish telemetry
    for. Dual-unit hub selection uses `_is_dual_unit`, identical to `ogsim.fleet.state`'s vectorized
    rule: it selects 10 of each bank's 50 hubs (at the confirmed defaults), spread across *all* 40
    banks instead of clustering in 1-in-5 of them. Each bank's `feeder_id` groups `banks_per_feeder`
    banks of the same zone (`feeder_id_for`), so G-06 evaluates.
    """
    n_zones = len(config.zones)

    hub_zone_by_bank: dict[str, dict[str, int]] = {}
    hubs = []
    for i in range(config.hub_count):
        hub_id = f"hub-{i:05d}"
        bank_id = f"bank-{i % config.bank_count:03d}"
        zone = config.zones[i % n_zones]
        dual_unit = _is_dual_unit(i, config.bank_count, config.dual_unit_share)
        e_kwh = config.e_kwh_dual_unit if dual_unit else config.e_kwh_default
        p_kw = config.p_kw_dual_unit if dual_unit else config.p_kw_default
        r_kwh = e_kwh * config.reserve_frac_default
        _lat, _lon = hub_coordinates(hub_id, zone)
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
                lat=_lat,
                lon=_lon,
            )
        )
        hub_zone_by_bank.setdefault(bank_id, {}).setdefault(zone, 0)
        hub_zone_by_bank[bank_id][zone] += 1

    banks = []
    banks_seen_by_zone: dict[str, int] = {}
    for b in range(config.bank_count):
        bank_id = f"bank-{b:03d}"
        # A bank's own `zone` column has no independent source in the sim (hubs cycle zones by hub
        # index, independently of bank grouping) -- the bank's majority hub zone is the closest honest
        # single answer, deterministic via (count desc, zone asc) so re-seeding never flips it.
        zone_counts = hub_zone_by_bank.get(bank_id, {})
        zone = min(zone_counts, key=lambda z: (-zone_counts[z], z)) if zone_counts else config.zones[0]
        rank_in_zone = banks_seen_by_zone.get(zone, 0)
        banks_seen_by_zone[zone] = rank_in_zone + 1
        banks.append(
            Bank(
                bank_id=bank_id,
                zone=zone,
                kva_rating=config.bank_kva_rating_default,
                reserve_kva=0.0,
                feeder_id=feeder_id_for(zone, rank_in_zone, banks_per_feeder),
            )
        )

    hub_offset, bank_offset = config.hub_count, config.bank_count
    for block in config.zone_blocks:
        if not block.enabled:
            continue
        hubs_added, banks_added = _build_zone_block(
            block, config, hub_offset=hub_offset, bank_offset=bank_offset, banks_per_feeder=banks_per_feeder
        )
        hubs.extend(hubs_added)
        banks.extend(banks_added)
        hub_offset += block.banks * block.homes_per_bank
        bank_offset += block.banks

    return Topology(hubs=tuple(hubs), banks=tuple(banks))


def _build_zone_block(
    block: ZoneBlockConfig,
    config: SimFleetTopologyConfig,
    *,
    hub_offset: int,
    bank_offset: int,
    banks_per_feeder: int,
) -> tuple[list[Hub], list[Bank]]:
    """One enabled `ZoneBlockConfig`'s hubs/banks: `block.banks` banks, all in `block.zone` (a block is
    single-zone by construction, so -- unlike the base fleet's majority-vote zone -- there's no
    ambiguity), each with `block.homes_per_bank` hubs round-robin-assigned and the same per-bank-offset
    dual-unit rule as the base fleet (`_is_dual_unit`). Ids continue from `hub_offset`/`bank_offset`,
    so an enabled block never renumbers the base fleet or an earlier block (`build_topology`'s
    docstring). Mirrors `ogsim.fleet.state.build_fleet_state`'s block-handling loop exactly."""
    n_block = block.banks * block.homes_per_bank
    hubs: list[Hub] = []
    for j in range(n_block):
        dual_unit = _is_dual_unit(j, block.banks, config.dual_unit_share)
        e_kwh = config.e_kwh_dual_unit if dual_unit else config.e_kwh_default
        p_kw = config.p_kw_dual_unit if dual_unit else config.p_kw_default
        block_hub_id = f"hub-{hub_offset + j:05d}"
        block_lat, block_lon = hub_coordinates(block_hub_id, block.zone)
        hubs.append(
            Hub(
                hub_id=block_hub_id,
                bank_id=f"bank-{bank_offset + (j % block.banks):03d}",
                zone=block.zone,
                e_kwh=e_kwh,
                r_kwh=e_kwh * config.reserve_frac_default,
                p_kw=p_kw,
                eta_c=config.eta_c,
                eta_d=config.eta_d,
                lat=block_lat,
                lon=block_lon,
            )
        )

    banks = [
        Bank(
            bank_id=f"bank-{bank_offset + b:03d}",
            zone=block.zone,
            kva_rating=config.bank_kva_rating_default,
            reserve_kva=0.0,
            feeder_id=feeder_id_for(block.zone, b, banks_per_feeder),
        )
        for b in range(block.banks)
    ]
    return hubs, banks


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
    topology = build_topology(
        sim_cfg, banks_per_feeder=int(cfg.get("fleet.banks_per_feeder", BANKS_PER_FEEDER_DEFAULT))
    )
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
