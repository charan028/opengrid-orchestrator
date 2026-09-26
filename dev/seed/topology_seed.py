#!/usr/bin/env python3
"""dev/seed/topology_seed.py -- grid topology seed for the simulated fleet (WP-L, Gitea #39).

Migration 0029's registry tables (`og.service_transformer`, `og.feeder_limit`, `og.substation_limit`, plus
`og.hub.transformer_id`) are almost empty, so G-27/G-28/G-29 fall back to their permissive static defaults
(`[guardian.flow]`: 25 kVA per unmapped home, a 10 MW feeder). 09 S2.6 wants these checks to fail closed on
real rows. This script generates those rows from the simulator's own config and prints them as one
idempotent SQL transaction (or writes it to `--out`, or applies it with `--dsn`).

    python dev/seed/topology_seed.py > /tmp/topology_seed.sql
    psql "$DSN" -v ON_ERROR_STOP=1 -f /tmp/topology_seed.sql
    python dev/seed/topology_seed.py --dsn "$DSN"          # same, in one step

NOT run by any migration, deploy or compose service: like every other file in dev/seed/, the lead applies
it by hand. Run it with the orchestrator's venv: it imports `opengrid.fleet.seed.build_topology` (the very
function `python -m opengrid.fleet.seed` seeds og.hub/og.bank with), so every hub, bank and feeder id here is
the id already in the database -- never a second copy of the id scheme.

Inputs
------
* `integration-sims/config/fleet.yaml` -- hub/bank/zone counts, dual-unit rule, zone blocks.
* `integration-sims/config/scada.yaml` -- cross-checked against fleet.yaml (same banks, zones, bank rating);
  its `zone_blocks` list (the blocks SCADA covers: LZ_AEN, LZ_CPS) is the default `--blocks` set. The ids
  are then exactly dev/seed/add_austin_fleet.sql's (bank-040..049 LZ_AEN, bank-050..059 LZ_CPS), whether
  or not the blocks are enabled in the yaml yet.
* `orchestrator/config/tdsp_tariffs.toml` `[zone_territory]` -- the utility of a regulated zone's home-bank
  asset row.

Rows (every one is an upsert; the output is deterministic, so re-running is a no-op)
-----------------------------------------------------------------------------------
* `og.service_transformer` + `og.hub.transformer_id`: every hub mapped. Assumed (Texas residential
  distribution practice, not utility GIS data): pole/pad-mount transformers of 25, 50 or 75 kVA, each
  serving 4-10 homes -- 25 kVA for 4-5 homes, 50 kVA for 6-8, 75 kVA for 9-10 (5-8.3 kVA per home). A
  bank's homes (in hub-id order) are grouped by the cycle `TRANSFORMER_GROUP_PATTERN` (10, 8, 4, 7, 10, 5,
  6 homes = 50, one 50-home bank), the tail adjusted so no group falls below 4 homes. A 50-home bank thus
  gets 7 transformers, 350 kVA in total, against 640 kW of battery inverters: G-27 and the allocator's F3
  group cap now bind a simultaneous full-fleet export at zero home load. Every single home stays feasible:
  the one dual-unit (20 kW) home behind each 25 kVA transformer can discharge at full power.
* `og.feeder_limit`: one row per feeder (`feeder-<zone>-NN`, 5 banks each): the 8 competitive feeders
  plus every included block's. Assumed: `thermal_kw` 10,000 (a 12.47 kV, 600 A-class Texas distribution
  feeder's normal rating, the `[guardian.flow]` default made explicit); `reverse_kw` the member banks'
  summed kVA rating (3,000 kW for 5 x 600 kVA segments): feeder-head reverse flow no larger than the
  segments the guardian can measure, which equals today's 3 MW default.
* `og.substation_limit` + `og.asset` HOME_BANK rows: G-29 membership is `og.asset.substation_id`
  (migration 0025), so each bank gets its HOME_BANK asset row (asset_id = bank_id, the bank's summed
  kW/kWh). Assumed: two of our feeders per distribution substation (`sub-<zone>-NN`), each substation
  rated 50,000 kVA (two 25 MVA 138/12.47 kV transformers), `reverse_kw` its feeders' summed reverse limit.
  The regulated-territory rule (K15: no reverse flow at an Austin/CPS substation) is applied by the
  guardian itself (`flow_repo.substation_flow`), not written into the equipment rating here.

Kept, not clobbered: the 20 MW Austin substation set (dev/seed/market_model_seed.sql; the sim's
`sub-LZ_AEN-00`). Its ids in `PROTECTED_IDS` are inserted with `ON CONFLICT DO NOTHING`, so a row already
there wins; `feeder-sub-LZ_AEN-00` and the `sub-aen-01` asset are never generated at all.

Rows for a bank that is not in `og.bank` yet (a zone block not seeded) are skipped, so the script is safe on
any database at migration 0029+. A regulated bank's asset row is also skipped while its `og.utility` row is
missing: apply dev/seed/market_model_seed.sql first to get G-29 membership for LZ_AEN/LZ_CPS.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import yaml

from opengrid.core.models.platform import Hub
from opengrid.fleet.seed import (
    BANKS_PER_FEEDER_DEFAULT,
    SimFleetTopologyConfig,
    Topology,
    ZoneBlockConfig,
    build_topology,
)
from opengrid.market.config import load_zone_territory

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FLEET_CONFIG = REPO_ROOT / "integration-sims" / "config" / "fleet.yaml"
DEFAULT_SCADA_CONFIG = REPO_ROOT / "integration-sims" / "config" / "scada.yaml"
DEFAULT_TDSP_TARIFFS = REPO_ROOT / "orchestrator" / "config" / "tdsp_tariffs.toml"

# Assumed service-transformer sizing (module docstring): homes per transformer, cycled over a bank's homes.
TRANSFORMER_GROUP_PATTERN: tuple[int, ...] = (10, 8, 4, 7, 10, 5, 6)
MIN_HOMES_PER_TRANSFORMER = 4
MAX_HOMES_PER_TRANSFORMER = 10

# Assumed feeder and substation equipment ratings (module docstring).
FEEDER_THERMAL_KW = 10_000.0
FEEDERS_PER_SUBSTATION = 2
SUBSTATION_RATING_KVA = 50_000.0

#: The 20 MW Austin substation set's rows (market_model_seed.sql / fleet.yaml `substation_assets`): never
#: overwritten. A generated row with one of these ids is inserted with ON CONFLICT DO NOTHING.
PROTECTED_IDS = frozenset({"feeder-sub-LZ_AEN-00", "sub-LZ_AEN-00", "sub-aen-01"})


@dataclass(frozen=True, slots=True)
class TransformerRow:
    transformer_id: str
    bank_id: str
    rating_kva: float
    hub_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeederRow:
    feeder_id: str
    thermal_kw: float
    reverse_kw: float
    bank_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubstationRow:
    substation_id: str
    rating_kva: float
    reverse_kw: float
    feeder_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HomeBankAssetRow:
    bank_id: str
    zone: str
    feeder_id: str
    substation_id: str
    utility_id: str | None
    p_kw: float
    e_kwh: float
    eta_rt: float
    floor_frac: float


@dataclass(frozen=True, slots=True)
class TopologySeed:
    topology: Topology
    transformers: tuple[TransformerRow, ...]
    feeders: tuple[FeederRow, ...]
    substations: tuple[SubstationRow, ...]
    assets: tuple[HomeBankAssetRow, ...]


# --- config ------------------------------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def _blocks(raw: Any) -> tuple[ZoneBlockConfig, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        ZoneBlockConfig(
            zone=str(b["zone"]),
            banks=int(b["banks"]),
            homes_per_bank=int(b["homes_per_bank"]),
            enabled=bool(b.get("enabled", False)),
        )
        for b in raw
        if isinstance(b, dict)
    )


def load_fleet_config(
    fleet_path: Path = DEFAULT_FLEET_CONFIG,
    scada_path: Path = DEFAULT_SCADA_CONFIG,
    blocks: Sequence[str] | None = None,
) -> SimFleetTopologyConfig:
    """fleet.yaml as `SimFleetTopologyConfig`, cross-checked against scada.yaml, with the zone blocks in
    `blocks` (default: scada.yaml's `zone_blocks` zones) treated as enabled. Raises `ValueError` when the
    two files disagree on what they both describe."""
    fleet_raw, scada_raw = _read_yaml(fleet_path), _read_yaml(scada_path)
    names = {f.name for f in fields(SimFleetTopologyConfig)} - {"zones", "zone_blocks"}
    cfg = SimFleetTopologyConfig(**{k: v for k, v in fleet_raw.items() if k in names})
    if "zones" in fleet_raw:
        cfg = replace(cfg, zones=tuple(str(z) for z in fleet_raw["zones"]))
    fleet_blocks, scada_blocks = _blocks(fleet_raw.get("zone_blocks")), _blocks(scada_raw.get("zone_blocks"))

    for key, fleet_value in (
        ("bank_count", cfg.bank_count),
        ("zones", list(cfg.zones)),
        ("bank_kva_rating_default", cfg.bank_kva_rating_default),
    ):
        if key in scada_raw and scada_raw[key] != fleet_value:
            raise ValueError(f"{key}: fleet.yaml has {fleet_value!r}, scada.yaml {scada_raw[key]!r}")
    by_zone = {b.zone: b for b in fleet_blocks}
    for sb in scada_blocks:
        fb = by_zone.get(sb.zone)
        if fb is None or (fb.banks, fb.homes_per_bank) != (sb.banks, sb.homes_per_bank):
            raise ValueError(f"zone block {sb.zone}: scada.yaml does not match fleet.yaml")

    wanted = {b.zone for b in scada_blocks} if blocks is None else set(blocks)
    unknown = wanted - set(by_zone)
    if unknown:
        raise ValueError(f"zone blocks not in fleet.yaml: {sorted(unknown)}")
    return replace(
        cfg, zone_blocks=tuple(replace(b, enabled=b.enabled or b.zone in wanted) for b in fleet_blocks)
    )


# --- rows --------------------------------------------------------------------------------------------------


def transformer_group_sizes(homes: int) -> tuple[int, ...]:
    """Homes per transformer for a bank of `homes` homes: `TRANSFORMER_GROUP_PATTERN` cycled, the tail
    adjusted so every group has 4-10 homes (a bank of fewer than 4 homes is one group)."""
    sizes: list[int] = []
    remaining = homes
    while remaining > 0:
        if remaining <= MAX_HOMES_PER_TRANSFORMER:
            size = remaining
        else:
            size = TRANSFORMER_GROUP_PATTERN[len(sizes) % len(TRANSFORMER_GROUP_PATTERN)]
            if remaining - size < MIN_HOMES_PER_TRANSFORMER:
                size = remaining - MIN_HOMES_PER_TRANSFORMER
        sizes.append(size)
        remaining -= size
    return tuple(sizes)


def transformer_kva_for(homes: int) -> float:
    """Assumed rating for a transformer serving `homes` homes (module docstring)."""
    if homes <= 5:
        return 25.0
    if homes <= 8:
        return 50.0
    return 75.0


def build_seed(
    config: SimFleetTopologyConfig,
    zone_territory: Mapping[str, str],
    *,
    banks_per_feeder: int = BANKS_PER_FEEDER_DEFAULT,
) -> TopologySeed:
    """Pure: the registry rows for `build_topology(config)`'s hubs and banks."""
    topology = build_topology(config, banks_per_feeder=banks_per_feeder)
    hubs_of_bank: dict[str, list[Hub]] = {}
    for hub in topology.hubs:
        hubs_of_bank.setdefault(hub.bank_id, []).append(hub)

    transformers: list[TransformerRow] = []
    for bank in topology.banks:
        hub_ids = sorted(h.hub_id for h in hubs_of_bank.get(bank.bank_id, []))
        start = 0
        for g, size in enumerate(transformer_group_sizes(len(hub_ids))):
            members = tuple(hub_ids[start : start + size])
            start += size
            transformers.append(
                TransformerRow(
                    f"xfmr-{bank.bank_id}-{g:02d}", bank.bank_id, transformer_kva_for(size), members
                )
            )

    feeder_banks: dict[str, list[str]] = {}
    feeder_zone: dict[str, str] = {}
    kva_by_bank = {b.bank_id: b.kva_rating for b in topology.banks}
    for bank in topology.banks:
        if bank.feeder_id is None:
            continue
        feeder_banks.setdefault(bank.feeder_id, []).append(bank.bank_id)
        feeder_zone[bank.feeder_id] = bank.zone
    feeders = tuple(
        FeederRow(f, FEEDER_THERMAL_KW, sum(kva_by_bank[b] for b in banks), tuple(sorted(banks)))
        for f, banks in sorted(feeder_banks.items())
    )

    substation_of_feeder: dict[str, str] = {}
    for zone in dict.fromkeys(feeder_zone[f.feeder_id] for f in feeders):
        zone_feeders = [f.feeder_id for f in feeders if feeder_zone[f.feeder_id] == zone]
        for rank, feeder_id in enumerate(zone_feeders):
            substation_of_feeder[feeder_id] = f"sub-{zone}-{rank // FEEDERS_PER_SUBSTATION:02d}"
    reverse_by_feeder = {f.feeder_id: f.reverse_kw for f in feeders}
    substation_feeders: dict[str, list[str]] = {}
    for feeder_id, substation_id in substation_of_feeder.items():
        substation_feeders.setdefault(substation_id, []).append(feeder_id)
    substations = tuple(
        SubstationRow(s, SUBSTATION_RATING_KVA, sum(reverse_by_feeder[f] for f in fs), tuple(sorted(fs)))
        for s, fs in sorted(substation_feeders.items())
    )

    assets = tuple(
        HomeBankAssetRow(
            bank_id=bank.bank_id,
            zone=bank.zone,
            feeder_id=bank.feeder_id,
            substation_id=substation_of_feeder[bank.feeder_id],
            utility_id=zone_territory.get(bank.zone),
            p_kw=sum(h.p_kw for h in hubs_of_bank.get(bank.bank_id, [])),
            e_kwh=sum(h.e_kwh for h in hubs_of_bank.get(bank.bank_id, [])),
            eta_rt=config.eta_c * config.eta_d,
            floor_frac=config.reserve_frac_default,
        )
        for bank in topology.banks
        if bank.feeder_id is not None and hubs_of_bank.get(bank.bank_id)
    )
    return TopologySeed(topology, tuple(transformers), feeders, substations, assets)


# --- SQL ---------------------------------------------------------------------------------------------------


def _lit(value: str | float | None) -> str:
    if value is None:
        return "NULL::text"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return f"{round(float(value), 4):.4f}"


def _values(rows: Iterable[Sequence[str | float | None]]) -> str:
    return ",\n".join("    (" + ", ".join(_lit(v) for v in row) + ")" for row in rows)


def _upsert_limits(
    table: str, key: str, cols: tuple[str, str], rows: Sequence[tuple[str, float, float]]
) -> str:
    """Two statements: ordinary rows upserted, `PROTECTED_IDS` rows inserted only when absent."""
    head = f"INSERT INTO og.{table} ({key}, {cols[0]}, {cols[1]}) VALUES\n"
    update = f"{cols[0]} = EXCLUDED.{cols[0]}, {cols[1]} = EXCLUDED.{cols[1]}, updated_at = now()"
    out: list[str] = []
    normal = [r for r in rows if r[0] not in PROTECTED_IDS]
    kept = [r for r in rows if r[0] in PROTECTED_IDS]
    if normal:
        out.append(f"{head}{_values(normal)}\nON CONFLICT ({key}) DO UPDATE SET {update};")
    if kept:
        out.append(
            f"-- Protected (market_model_seed.sql's row wins):\n{head}{_values(kept)}\nON CONFLICT DO NOTHING;"
        )
    return "\n\n".join(out)


def render_sql(seed: TopologySeed) -> str:
    """The whole seed as one transaction. Deterministic: the same config always renders the same text."""
    t = seed.topology
    bank_of_hub = {h.hub_id: h.bank_id for h in t.hubs}
    mapping = [
        (hub_id, bank_of_hub[hub_id], x.transformer_id) for x in seed.transformers for hub_id in x.hub_ids
    ]
    zones = sorted({b.zone for b in t.banks})
    parts = [
        "-- dev/seed/topology_seed.py output: og.service_transformer, og.hub.transformer_id, og.feeder_limit,",
        "-- og.substation_limit and HOME_BANK og.asset rows (migration 0029 registry, 09 S2.6). Generated; do not",
        "-- edit by hand -- change the config or the script and regenerate. Idempotent: safe to re-run.",
        f"-- {len(t.hubs)} hubs, {len(t.banks)} banks, {len(seed.transformers)} transformers, "
        f"{len(seed.feeders)} feeders, {len(seed.substations)} substations; zones {', '.join(zones)}.",
        "",
        "BEGIN;",
        "",
        "SET search_path TO og;",
        "",
        "-- Service transformers, for banks already in og.bank (bank_id is a foreign key).",
        "INSERT INTO og.service_transformer (transformer_id, bank_id, rating_kva)",
        "SELECT v.transformer_id, v.bank_id, v.rating_kva",
        "FROM (VALUES",
        _values((x.transformer_id, x.bank_id, x.rating_kva) for x in seed.transformers),
        ") AS v(transformer_id, bank_id, rating_kva)",
        "WHERE EXISTS (SELECT 1 FROM og.bank b WHERE b.bank_id = v.bank_id)",
        "ON CONFLICT (transformer_id) DO UPDATE SET bank_id = EXCLUDED.bank_id, rating_kva = EXCLUDED.rating_kva;",
        "",
        "-- Every hub -> its transformer; only where og.hub agrees on the hub's bank.",
        "UPDATE og.hub h SET transformer_id = v.transformer_id",
        "FROM (VALUES",
        _values(mapping),
        ") AS v(hub_id, bank_id, transformer_id)",
        "WHERE h.hub_id = v.hub_id AND h.bank_id = v.bank_id",
        "  AND h.transformer_id IS DISTINCT FROM v.transformer_id",
        "  AND EXISTS (SELECT 1 FROM og.service_transformer st WHERE st.transformer_id = v.transformer_id);",
        "",
        "-- Feeder thermal and reverse limits (G-28).",
        _upsert_limits(
            "feeder_limit",
            "feeder_id",
            ("thermal_kw", "reverse_kw"),
            [(f.feeder_id, f.thermal_kw, f.reverse_kw) for f in seed.feeders],
        ),
        "",
        "-- Substation transformer limits (G-29).",
        _upsert_limits(
            "substation_limit",
            "substation_id",
            ("rating_kva", "reverse_kw"),
            [(s.substation_id, s.rating_kva, s.reverse_kw) for s in seed.substations],
        ),
        "",
        "-- HOME_BANK assets: G-29 substation membership (og.asset.substation_id, migration 0025). Skipped for a",
        "-- bank not in og.bank, or a regulated bank whose og.utility row is missing (market_model_seed.sql).",
        "INSERT INTO og.asset (",
        "    asset_id, asset_class, bank_id, feeder_id, substation_id, zone, utility_id, p_kw, e_kwh, eta_rt,",
        "    floor_frac, status",
        ")",
        "SELECT v.bank_id, 'HOME_BANK', v.bank_id, v.feeder_id, v.substation_id, v.zone, v.utility_id, v.p_kw,",
        "    v.e_kwh, v.eta_rt, v.floor_frac, 'ACTIVE'",
        "FROM (VALUES",
        _values(
            (
                a.bank_id,
                a.feeder_id,
                a.substation_id,
                a.zone,
                a.utility_id,
                a.p_kw,
                a.e_kwh,
                a.eta_rt,
                a.floor_frac,
            )
            for a in seed.assets
        ),
        ") AS v(bank_id, feeder_id, substation_id, zone, utility_id, p_kw, e_kwh, eta_rt, floor_frac)",
        "WHERE EXISTS (SELECT 1 FROM og.bank b WHERE b.bank_id = v.bank_id)",
        "  AND (v.utility_id IS NULL OR EXISTS (SELECT 1 FROM og.utility u WHERE u.utility_id = v.utility_id))",
        "ON CONFLICT (asset_id) DO UPDATE SET",
        "    feeder_id = EXCLUDED.feeder_id, substation_id = EXCLUDED.substation_id, zone = EXCLUDED.zone,",
        "    utility_id = EXCLUDED.utility_id, p_kw = EXCLUDED.p_kw, e_kwh = EXCLUDED.e_kwh,",
        "    eta_rt = EXCLUDED.eta_rt, floor_frac = EXCLUDED.floor_frac, updated_at = now()",
        "WHERE og.asset.asset_class = 'HOME_BANK';",
        "",
        "COMMIT;",
        "",
        "-- Verification (read-only):",
        "--   SELECT count(*) FROM og.hub WHERE transformer_id IS NULL;              -- expect 0",
        "--   SELECT rating_kva, count(*) FROM og.service_transformer GROUP BY 1 ORDER BY 1;",
        "--   SELECT feeder_id, thermal_kw, reverse_kw FROM og.feeder_limit ORDER BY 1;",
        "--   SELECT substation_id, rating_kva, reverse_kw FROM og.substation_limit ORDER BY 1;",
        "--   SELECT b.bank_id FROM og.bank b LEFT JOIN og.asset a ON a.bank_id = b.bank_id",
        "--       AND a.asset_class = 'HOME_BANK' WHERE a.asset_id IS NULL;        -- expect 0 rows",
        "",
    ]
    return "\n".join(parts)


# --- CLI ---------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fleet-config", type=Path, default=DEFAULT_FLEET_CONFIG)
    parser.add_argument("--scada-config", type=Path, default=DEFAULT_SCADA_CONFIG)
    parser.add_argument("--tdsp-tariffs", type=Path, default=DEFAULT_TDSP_TARIFFS)
    parser.add_argument(
        "--blocks",
        default=None,
        help="comma-separated zone blocks to include (default: scada.yaml's zone_blocks); '' for none",
    )
    parser.add_argument("--banks-per-feeder", type=int, default=BANKS_PER_FEEDER_DEFAULT)
    parser.add_argument("--out", type=Path, default=None, help="write the SQL here instead of stdout")
    parser.add_argument("--dsn", default=None, help="apply the SQL to this database instead of printing it")
    args = parser.parse_args(argv)

    blocks = None if args.blocks is None else [z for z in args.blocks.split(",") if z]
    config = load_fleet_config(args.fleet_config, args.scada_config, blocks)
    seed = build_seed(config, load_zone_territory(args.tdsp_tariffs), banks_per_feeder=args.banks_per_feeder)
    sql = render_sql(seed)
    if args.dsn:
        import psycopg

        with psycopg.connect(args.dsn, autocommit=True) as conn:
            conn.execute(sql.encode("utf-8"))
        print(
            f"applied: {len(seed.transformers)} transformers, {len(seed.feeders)} feeders, "
            f"{len(seed.substations)} substations, {len(seed.assets)} home-bank assets"
        )
    elif args.out is not None:
        args.out.write_text(sql, encoding="utf-8")
    else:
        sys.stdout.write(sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
