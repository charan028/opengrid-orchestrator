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
    python dev/seed/topology_seed.py --dsn "$DSN" --only-missing --dry-run   # backfill plan, rolled back

NOT run by any migration or deploy: `deploy/scripts/bootstrap_from_scratch.sh` phase e runs it as the LAST
seed (after the trucks), and `deploy/scripts/topology_backfill.sh` runs it with `--only-missing` on an existing
database. Run it with the orchestrator's venv: it imports `opengrid.fleet.seed.build_topology` (the very
function `python -m opengrid.fleet.seed` seeds og.hub/og.bank with), so every hub, bank and feeder id here is
the id already in the database -- never a second copy of the id scheme.

Inputs
------
* `integration-sims/config/fleet.yaml` (production: `/etc/opengrid/sim/fleet.yaml`) -- hub/bank/zone counts,
  dual-unit rule, zone blocks. The zone blocks it marks `enabled` are exactly the blocks the fleet seed wrote
  to og.hub/og.bank, so they are the default set here too: a block's ids depend on which blocks precede it
  (LZ_LCRA is bank-050..059 while LZ_CPS is off, bank-060..069 once it is on), so seeding a block the fleet
  seed did not would map hubs to the wrong zone's rows. `--blocks` adds blocks (tests, what-if SQL only).
* `integration-sims/config/scada.yaml` -- cross-checked against fleet.yaml (same banks, zones, bank rating,
  and the same zone blocks, enabled alike).
* `orchestrator/config/tdsp_tariffs.toml` `[zone_territory]` -- the utility of a regulated zone's home-bank
  asset row.

Rows (every one is an upsert; the output is deterministic, so re-running is a no-op)
-----------------------------------------------------------------------------------
* `og.service_transformer` + `og.hub.transformer_id`: every hub mapped. Sizing per D-5 (decision D-36): a
  home bank's service transformers SUM TO THE BANK'S kVA RATING (~600 kVA). A bank gets
  `ceil(rating / TRANSFORMER_UNIT_KVA)` transformers (never more than it has homes), each rated
  `og.bank.kva_rating / n` -- the rating in og.bank where the row exists, the config's otherwise -- so a
  600 kVA, 50-home bank gets 12 x 50 kVA. Homes are dealt round-robin in hub-id order (the hub's rank in
  its bank), so groups differ by at most one home (4-5 of 50) and the dual-unit homes (every 5th rank,
  `opengrid.fleet.seed._is_dual_unit`) land on distinct transformers. Every single home stays feasible
  (a 20 kW dual-unit home at full power behind a 50 kVA unit), and G-27/F3 bind only where a whole group's
  inverters exceed its unit (4 x 11 + 20 = 64 kW behind 50 kVA): the bank total equals the bank rating
  the guardian already enforces.
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
* Dedicated-connection banks (`opengrid.fleet.topology_audit.DEDICATED_BANKS_SQL`): the single-hub bank of a
  SUBSTATION asset (the 20 MW Austin set, market_model_seed.sql) or a MOBILE_STORAGE asset (each truck,
  mobile_trucks_seed.sql). They are not in fleet.yaml's id scheme, so their rows are derived in SQL from the
  rows those seeds wrote: one service transformer `xfmr-<bank_id>-00` rated at the bank's own
  `og.bank.kva_rating` (the connection rating each seed declares: 20,408 kVA for the substation set, 600 kVA
  for a truck's depot service), every hub of the bank mapped to it, and an `og.feeder_limit` row for the
  bank's feeder when it has none (thermal as above, reverse = the bank rating; an existing row, e.g. the
  substation set's, wins). No HOME_BANK asset: they are not members of a distribution substation.
  TRUCKS ARE MAPPED, NOT EXEMPT: G-27 stays fail-closed for them. A truck is only dispatched while it sits at
  its home station (G-35, D-31), and there its PCS hangs off the depot's own service transformer, so that is
  the transformer G-27 must bind; `feeder-<truck id>` is the depot feeder mobile_trucks_seed.sql assigns.
  An exemption would need a guardian code path that skips G-27 for a class of hub, which 09 S2.6 does not
  allow, and a truck rated at the 25 kVA unmapped-home default could never discharge.
  POI premise: their hubs' `service_kw` and `export_limit_kw` are filled with the nameplate `p_kw` where
  NULL (never overwritten), so G-26 does not apply a home's 20 kW export / 48 kW service default to them.

Every count in `opengrid.fleet.topology_audit.UNMAPPED_QUERIES` is 0 after this seed (bootstrap_check.py
asserts it): no ALR-XFMR-UNMAPPED and no ALR-BANK-UNMAPPED-TOPOLOGY on a fresh install.

Kept, not clobbered: the 20 MW Austin substation set (dev/seed/market_model_seed.sql; the sim's
`sub-LZ_AEN-00`). Its ids in `PROTECTED_IDS` are inserted with `ON CONFLICT DO NOTHING`, so a row already
there wins; `feeder-sub-LZ_AEN-00` and the `sub-aen-01` asset are never generated at all.

Rows for a bank that is not in `og.bank` yet (a zone block not seeded) are skipped, so the script is safe on
any database at migration 0029+. A regulated bank's asset row is also skipped while its `og.utility` row is
missing: apply dev/seed/market_model_seed.sql first to get G-29 membership for LZ_AEN/LZ_CPS.

`--only-missing` (production backfill) renders the same rows insert-only: every INSERT is ON CONFLICT DO
NOTHING and a hub is mapped only while its `transformer_id` is NULL, so no existing og.bank, og.hub, limit,
transformer or asset row is rewritten. With `--dsn` the statements run in ONE transaction that also verifies
exactly that (fingerprints of the existing rows before and after); any difference rolls it back. `--dry-run`
always rolls back and prints the plan: rows per statement and the unmapped counts before and after.
`--only HUB_ID` restricts that to one dedicated-connection hub's bank (its transformer, mapping, POI premise
and feeder limit): the toll hotfix for `sub-LZ_AEN-00`, shippable without the full backfill.
"""

from __future__ import annotations

import argparse
import math
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
from opengrid.fleet.topology_audit import DEDICATED_BANKS_SQL, count_unmapped
from opengrid.market.config import load_zone_territory

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FLEET_CONFIG = REPO_ROOT / "integration-sims" / "config" / "fleet.yaml"
DEFAULT_SCADA_CONFIG = REPO_ROOT / "integration-sims" / "config" / "scada.yaml"
DEFAULT_TDSP_TARIFFS = REPO_ROOT / "orchestrator" / "config" / "tdsp_tariffs.toml"

# Service-transformer sizing (D-5 / D-36, module docstring): a home bank's units sum to its kVA rating, each
# about this size (a standard residential pad-mount unit).
TRANSFORMER_UNIT_KVA = 50.0

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
    rating_kva: float  # config rating / units; the SQL rates it og.bank.kva_rating / units
    hub_ids: tuple[str, ...]
    units: int  # transformers on this bank


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
    """fleet.yaml as `SimFleetTopologyConfig`, cross-checked against scada.yaml. The zone blocks enabled are
    fleet.yaml's own (what the fleet seed wrote), plus any named in `blocks`. Raises `ValueError` when the two
    files disagree on what they both describe, or `blocks` names a block fleet.yaml does not define."""
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
        if fb is None or (fb.banks, fb.homes_per_bank, fb.enabled) != (
            sb.banks,
            sb.homes_per_bank,
            sb.enabled,
        ):
            raise ValueError(f"zone block {sb.zone}: scada.yaml does not match fleet.yaml")

    wanted = set(blocks or ())
    unknown = wanted - set(by_zone)
    if unknown:
        raise ValueError(f"zone blocks not in fleet.yaml: {sorted(unknown)}")
    return replace(
        cfg, zone_blocks=tuple(replace(b, enabled=b.enabled or b.zone in wanted) for b in fleet_blocks)
    )


# --- rows --------------------------------------------------------------------------------------------------


def transformer_count(bank_kva: float, homes: int) -> int:
    """Transformers on a home bank (D-36): `ceil(bank_kva / TRANSFORMER_UNIT_KVA)`, at least 1, at most one
    per home."""
    return max(1, min(homes, math.ceil(bank_kva / TRANSFORMER_UNIT_KVA - 1e-9)))


def transformer_groups(hub_ids: Sequence[str], units: int) -> tuple[tuple[str, ...], ...]:
    """`hub_ids` (sorted: the hub's rank in its bank) dealt round-robin onto `units` transformers: rank r goes
    to unit r % units, so group sizes differ by at most one and evenly spaced ranks (the dual-unit homes)
    land on distinct units."""
    ordered = sorted(hub_ids)
    return tuple(tuple(ordered[g::units]) for g in range(units))


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
        hub_ids = [h.hub_id for h in hubs_of_bank.get(bank.bank_id, [])]
        if not hub_ids:
            continue
        units = transformer_count(bank.kva_rating, len(hub_ids))
        for g, members in enumerate(transformer_groups(hub_ids, units)):
            transformers.append(
                TransformerRow(
                    f"xfmr-{bank.bank_id}-{g:02d}", bank.bank_id, bank.kva_rating / units, members, units
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


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement of the seed: `label` names it in the `--dry-run` plan, `comment` precedes it in the SQL."""

    label: str
    comment: str
    sql: str


def _limit_statements(
    table: str,
    key: str,
    cols: tuple[str, str],
    rows: Sequence[tuple[str, float, float]],
    *,
    comment: str,
    only_missing: bool,
) -> list[Statement]:
    """Ordinary rows upserted (inserted only when absent with `only_missing`); `PROTECTED_IDS` rows always
    inserted only when absent."""
    head = f"INSERT INTO og.{table} ({key}, {cols[0]}, {cols[1]}) VALUES\n"
    update = f"{cols[0]} = EXCLUDED.{cols[0]}, {cols[1]} = EXCLUDED.{cols[1]}, updated_at = now()"
    conflict = "ON CONFLICT DO NOTHING" if only_missing else f"ON CONFLICT ({key}) DO UPDATE SET {update}"
    out: list[Statement] = []
    normal = [r for r in rows if r[0] not in PROTECTED_IDS]
    kept = [r for r in rows if r[0] in PROTECTED_IDS]
    if normal:
        out.append(Statement(f"og.{table}", comment, f"{head}{_values(normal)}\n{conflict}"))
    if kept:
        out.append(
            Statement(
                f"og.{table} (protected ids)",
                "-- Protected (market_model_seed.sql's row wins):",
                f"{head}{_values(kept)}\nON CONFLICT DO NOTHING",
            )
        )
    return out


def _dedicated_statements(*, only_missing: bool, only_hub: str | None = None) -> list[Statement]:
    """The dedicated-connection banks' rows (module docstring), derived in SQL from og.bank/og.hub/og.asset:
    the transformer at the bank rating, the hubs mapped to it, the hubs' POI premise and the bank feeder's
    limit. `only_hub`: just that hub's bank (the separable toll hotfix, `--only`).

    POI premise: a utility-scale hub (the 20 MW substation set, a 500 kW truck at its depot) is not a home
    behind a 200 A service, so the guardian's home defaults (export 20 kW, service 48 kW) would make G-26 veto
    it. `service_kw` and `export_limit_kw` are set to the hub's nameplate `p_kw` -- ONLY where NULL, in every
    mode: a value already in og.hub (an interconnection agreement's) is never overwritten."""
    xfmr = "'xfmr-' || b.bank_id || '-00'"
    upsert_xfmr = (
        "ON CONFLICT (transformer_id) DO NOTHING"
        if only_missing
        else "ON CONFLICT (transformer_id) DO UPDATE SET bank_id = EXCLUDED.bank_id, rating_kva = EXCLUDED.rating_kva"
    )
    hub_still = "h.transformer_id IS NULL" if only_missing else f"h.transformer_id IS DISTINCT FROM {xfmr}"
    banks = f"b.bank_id IN ({DEDICATED_BANKS_SQL})"
    if only_hub is not None:
        banks += f" AND b.bank_id = (SELECT o.bank_id FROM og.hub o WHERE o.hub_id = {_lit(only_hub)})"  # noqa: S608
    return [
        Statement(
            "og.service_transformer (dedicated banks)",
            "-- Dedicated-connection banks (the substation set, each truck at its depot): one transformer at the\n"
            "-- bank's own kVA rating, from the rows market_model_seed.sql / mobile_trucks_seed.sql wrote.",
            "\n".join(
                [
                    "INSERT INTO og.service_transformer (transformer_id, bank_id, rating_kva)",
                    f"SELECT {xfmr}, b.bank_id, b.kva_rating",
                    "FROM og.bank b",
                    f"WHERE {banks}",
                    upsert_xfmr,
                ]
            ),
        ),
        Statement(
            "og.hub.transformer_id (dedicated banks)",
            "-- Every hub of a dedicated bank -> that bank's transformer.",
            "\n".join(
                [
                    f"UPDATE og.hub h SET transformer_id = {xfmr}",  # noqa: S608 -- fixed fragments
                    "FROM og.bank b",
                    f"WHERE b.bank_id = h.bank_id AND {banks}",
                    f"  AND {hub_still}",
                    "  AND EXISTS (SELECT 1 FROM og.service_transformer st",
                    f"    WHERE st.transformer_id = {xfmr})",
                ]
            ),
        ),
        Statement(
            "og.hub service_kw/export_limit_kw (dedicated banks, NULL only)",
            "-- POI premise of a utility-scale hub: its nameplate, only where og.hub has none (never overwritten).",
            "\n".join(
                [
                    "UPDATE og.hub h SET service_kw = coalesce(h.service_kw, h.p_kw),",
                    "    export_limit_kw = coalesce(h.export_limit_kw, h.p_kw)",
                    "FROM og.bank b",
                    f"WHERE b.bank_id = h.bank_id AND {banks}",
                    "  AND (h.service_kw IS NULL OR h.export_limit_kw IS NULL)",
                ]
            ),
        ),
        Statement(
            "og.feeder_limit (dedicated banks)",
            "-- A dedicated bank's feeder, when it has no row yet (market_model_seed.sql's substation feeder wins).",
            "\n".join(
                [
                    "INSERT INTO og.feeder_limit (feeder_id, thermal_kw, reverse_kw)",
                    f"SELECT b.feeder_id, {_lit(FEEDER_THERMAL_KW)}, sum(b.kva_rating)",
                    "FROM og.bank b",
                    f"WHERE b.feeder_id IS NOT NULL AND {banks}",
                    "GROUP BY b.feeder_id",
                    "ON CONFLICT DO NOTHING",
                ]
            ),
        ),
    ]


def seed_statements(seed: TopologySeed, *, only_missing: bool = False) -> tuple[Statement, ...]:
    """The seed's statements in apply order. `only_missing`: insert-only, a hub mapped only while unmapped."""
    t = seed.topology
    bank_of_hub = {h.hub_id: h.bank_id for h in t.hubs}
    mapping = [
        (hub_id, bank_of_hub[hub_id], x.transformer_id) for x in seed.transformers for hub_id in x.hub_ids
    ]
    upsert_xfmr = (
        "ON CONFLICT (transformer_id) DO NOTHING"
        if only_missing
        else "ON CONFLICT (transformer_id) DO UPDATE SET bank_id = EXCLUDED.bank_id, rating_kva = EXCLUDED.rating_kva"
    )
    hub_still = (
        "h.transformer_id IS NULL" if only_missing else "h.transformer_id IS DISTINCT FROM v.transformer_id"
    )
    statements = [
        Statement(
            "og.service_transformer (home banks)",
            "-- Service transformers, for banks already in og.bank (bank_id is a foreign key): each of a bank's\n"
            "-- units rated og.bank.kva_rating / units, so they sum to the bank's rating (D-5, D-36).",
            "\n".join(
                [
                    "INSERT INTO og.service_transformer (transformer_id, bank_id, rating_kva)",
                    "SELECT v.transformer_id, v.bank_id, b.kva_rating / v.units",
                    "FROM (VALUES",
                    _values((x.transformer_id, x.bank_id, float(x.units)) for x in seed.transformers),
                    ") AS v(transformer_id, bank_id, units)",
                    "JOIN og.bank b ON b.bank_id = v.bank_id",
                    upsert_xfmr,
                ]
            ),
        ),
        Statement(
            "og.hub.transformer_id (home hubs)",
            "-- Every hub -> its transformer; only where og.hub agrees on the hub's bank.",
            "\n".join(
                [
                    "UPDATE og.hub h SET transformer_id = v.transformer_id",
                    "FROM (VALUES",
                    _values(mapping),
                    ") AS v(hub_id, bank_id, transformer_id)",
                    "WHERE h.hub_id = v.hub_id AND h.bank_id = v.bank_id",
                    f"  AND {hub_still}",
                    "  AND EXISTS (SELECT 1 FROM og.service_transformer st WHERE st.transformer_id = v.transformer_id)",
                ]
            ),
        ),
    ]
    statements += _dedicated_statements(only_missing=only_missing)
    statements += _limit_statements(
        "feeder_limit",
        "feeder_id",
        ("thermal_kw", "reverse_kw"),
        [(f.feeder_id, f.thermal_kw, f.reverse_kw) for f in seed.feeders],
        comment="-- Feeder thermal and reverse limits (G-28).",
        only_missing=only_missing,
    )
    statements += _limit_statements(
        "substation_limit",
        "substation_id",
        ("rating_kva", "reverse_kw"),
        [(s.substation_id, s.rating_kva, s.reverse_kw) for s in seed.substations],
        comment="-- Substation transformer limits (G-29).",
        only_missing=only_missing,
    )
    upsert_asset = (
        "ON CONFLICT (asset_id) DO NOTHING"
        if only_missing
        else "\n".join(
            [
                "ON CONFLICT (asset_id) DO UPDATE SET",
                "    feeder_id = EXCLUDED.feeder_id, substation_id = EXCLUDED.substation_id, zone = EXCLUDED.zone,",
                "    utility_id = EXCLUDED.utility_id, p_kw = EXCLUDED.p_kw, e_kwh = EXCLUDED.e_kwh,",
                "    eta_rt = EXCLUDED.eta_rt, floor_frac = EXCLUDED.floor_frac, updated_at = now()",
                "WHERE og.asset.asset_class = 'HOME_BANK'",
            ]
        )
    )
    statements.append(
        Statement(
            "og.asset HOME_BANK",
            "-- HOME_BANK assets: G-29 substation membership (og.asset.substation_id, migration 0025). Skipped for a\n"
            "-- bank not in og.bank, or a regulated bank whose og.utility row is missing (market_model_seed.sql).",
            "\n".join(
                [
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
                    upsert_asset,
                ]
            ),
        )
    )
    return tuple(statements)


def render_sql(seed: TopologySeed, *, only_missing: bool = False) -> str:
    """The whole seed as one transaction. Deterministic: the same config always renders the same text."""
    t = seed.topology
    zones = sorted({b.zone for b in t.banks})
    parts = [
        "-- dev/seed/topology_seed.py output: og.service_transformer, og.hub.transformer_id, og.feeder_limit,",
        "-- og.substation_limit and HOME_BANK og.asset rows (migration 0029 registry, 09 S2.6). Generated; do not",
        "-- edit by hand -- change the config or the script and regenerate. Idempotent: safe to re-run.",
        f"-- {len(t.hubs)} hubs, {len(t.banks)} banks, {len(seed.transformers)} transformers, "
        f"{len(seed.feeders)} feeders, {len(seed.substations)} substations; zones {', '.join(zones)}"
        " (plus the dedicated-connection banks already in og.bank).",
    ]
    if only_missing:
        parts.append(
            "-- --only-missing: insert-only; no existing row is rewritten, a hub is mapped only while NULL."
        )
    parts += ["", "BEGIN;", "", "SET search_path TO og;", ""]
    for statement in seed_statements(seed, only_missing=only_missing):
        parts += [statement.comment, statement.sql + ";", ""]
    parts += [
        "COMMIT;",
        "",
        "-- Verification (read-only): every opengrid.fleet.topology_audit.UNMAPPED_QUERIES count is 0, e.g.",
        "--   SELECT count(*) FROM og.hub WHERE transformer_id IS NULL;              -- expect 0",
        "--   SELECT count(*) FROM og.bank WHERE feeder_id IS NULL;                  -- expect 0",
        "--   SELECT rating_kva, count(*) FROM og.service_transformer GROUP BY 1 ORDER BY 1;",
        "--   SELECT feeder_id, thermal_kw, reverse_kw FROM og.feeder_limit ORDER BY 1;",
        "--   SELECT substation_id, rating_kva, reverse_kw FROM og.substation_limit ORDER BY 1;",
        "",
    ]
    return "\n".join(parts)


# --- apply -------------------------------------------------------------------------------------------------

#: Existing rows `--only-missing` must leave untouched: table -> SELECT (key, row text). og.hub's text omits
#: the columns a backfill may fill while NULL (transformer_id, and a dedicated hub's service_kw /
#: export_limit_kw); a value already set in any of them is guarded on its own.
_HUB_FILLABLE = ("transformer_id", "service_kw", "export_limit_kw")
_GUARDED_ROWS: dict[str, str] = {
    "og.bank": "SELECT bank_id, to_jsonb(b)::text FROM og.bank b",
    "og.hub (all but the NULL-fillable columns)": (
        "SELECT hub_id, (to_jsonb(h) - 'transformer_id' - 'service_kw' - 'export_limit_kw')::text FROM og.hub h"
    ),
    **{
        f"og.hub.{col} (already set)": f"SELECT hub_id, {col}::text FROM og.hub WHERE {col} IS NOT NULL"  # noqa: S608
        for col in _HUB_FILLABLE
    },
    "og.service_transformer": "SELECT transformer_id, to_jsonb(t)::text FROM og.service_transformer t",
    "og.feeder_limit": "SELECT feeder_id, to_jsonb(f)::text FROM og.feeder_limit f",
    "og.substation_limit": "SELECT substation_id, to_jsonb(s)::text FROM og.substation_limit s",
    "og.asset": "SELECT asset_id, to_jsonb(a)::text FROM og.asset a",
}

#: Informational, in the plan: what G-27/F3 will bind once the rows exist (read inside the transaction).
#: Per zone: transformers, their kVA, the hubs' inverter kW, and the export G-27/F3 allow at zero home load
#: (per transformer, the lesser of its rating and its members' inverter kW).
_IMPACT_SQL = """
SELECT b.zone, count(*), round(sum(x.rating_kva)::numeric, 0), round(sum(x.hub_kw)::numeric, 0),
       round(sum(least(x.rating_kva, x.hub_kw))::numeric, 0)
FROM (
    SELECT st.transformer_id, st.bank_id, st.rating_kva, coalesce(sum(h.p_kw), 0) AS hub_kw
    FROM og.service_transformer st LEFT JOIN og.hub h ON h.transformer_id = st.transformer_id
    GROUP BY st.transformer_id, st.bank_id, st.rating_kva
) x JOIN og.bank b ON b.bank_id = x.bank_id
GROUP BY b.zone ORDER BY b.zone
"""
_OVER_RATING_SQL = """
SELECT count(*) FROM (
    SELECT st.transformer_id FROM og.service_transformer st
    JOIN og.hub h ON h.transformer_id = st.transformer_id JOIN og.hub_state hs ON hs.hub_id = h.hub_id
    GROUP BY st.transformer_id, st.rating_kva HAVING abs(sum(hs.p_kw)) > st.rating_kva
) x
"""


@dataclass(frozen=True, slots=True)
class ApplyReport:
    rows: tuple[tuple[str, int], ...]
    unmapped_before: dict[str, int]
    unmapped_after: dict[str, int]
    committed: bool
    hub_ready: bool | None = None  # `ready_hub`'s state inside the transaction (None: not asked)


#: A hub the guardian can dispatch at its nameplate: on an existing transformer, POI premise set.
_HUB_READY_SQL = """
SELECT h.service_kw IS NOT NULL AND h.export_limit_kw IS NOT NULL AND st.transformer_id IS NOT NULL
FROM og.hub h LEFT JOIN og.service_transformer st ON st.transformer_id = h.transformer_id
WHERE h.hub_id = %s
"""


def _guarded_rows(conn: Any) -> dict[str, dict[str, str]]:
    return {
        name: {str(k): str(v) for k, v in conn.execute(q).fetchall()} for name, q in _GUARDED_ROWS.items()
    }


def _changed_existing(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> list[str]:
    """Tables where a row that existed before is gone or differs."""
    return [
        name
        for name, rows in before.items()
        if any(after[name].get(key) != text for key, text in rows.items())
    ]


def apply_statements(
    conn: Any,
    statements: Sequence[Statement],
    *,
    guard_existing: bool,
    dry_run: bool,
    ready_hub: str | None = None,
) -> ApplyReport:
    """Run `statements` in ONE transaction on `conn` (a psycopg connection, not autocommit), printing each
    one's row count. `guard_existing`: roll back and raise when any row that existed before changed.
    `dry_run`: always roll back (the plan). `ready_hub`: also report whether that hub ends up mapped with its
    POI premise set."""
    hub_ready: bool | None = None
    committed = False
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '120s'")
        before_unmapped = count_unmapped(conn)
        before_rows = _guarded_rows(conn) if guard_existing else {}
        rows: list[tuple[str, int]] = []
        for statement in statements:
            rows.append((statement.label, conn.execute(statement.sql).rowcount))
        after_unmapped = count_unmapped(conn)
        if guard_existing:
            changed = _changed_existing(before_rows, _guarded_rows(conn))
            if changed:
                raise RuntimeError(f"existing rows would change in {', '.join(changed)}: rolled back")
        if dry_run:
            print("planned (dry run, rolled back):")
        else:
            print("applied:")
        for label, count in rows:
            print(f"  {count:6d}  {label}")
        for zone, n_xfmr, kva, hub_kw, export_kw in conn.execute(_IMPACT_SQL).fetchall():
            print(
                f"  [info] {zone}: {n_xfmr} service transformers, {kva} kVA; hub inverters {hub_kw} kW,"
                f" G-27/F3 export cap at zero home load {export_kw} kW"
            )
        over = conn.execute(_OVER_RATING_SQL).fetchone()
        print(
            f"  [info] transformers whose hubs' present |sum p_kw| exceeds the rating: {over[0] if over else 0}"
        )
        for label, count in after_unmapped.items():
            print(f"  unmapped {label}: {before_unmapped[label]} -> {count}")
        if ready_hub is not None:
            ready = conn.execute(_HUB_READY_SQL, (ready_hub,)).fetchone()
            hub_ready = bool(ready and ready[0])
            print(f"  {ready_hub}: mapped with POI premise: {'OK' if hub_ready else 'NO'}")
        if guard_existing:
            print(f"  existing rows unchanged: OK ({', '.join(_GUARDED_ROWS)})")
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            committed = True
    except BaseException:
        conn.rollback()
        raise
    return ApplyReport(tuple(rows), before_unmapped, after_unmapped, committed, hub_ready)


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
        help="comma-separated zone blocks to enable IN ADDITION to fleet.yaml's enabled ones (what-if SQL only)",
    )
    parser.add_argument("--banks-per-feeder", type=int, default=BANKS_PER_FEEDER_DEFAULT)
    parser.add_argument("--out", type=Path, default=None, help="write the SQL here instead of stdout")
    parser.add_argument("--dsn", default=None, help="apply the SQL to this database instead of printing it")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="insert-only backfill: never rewrite an existing row (verified in the transaction with --dsn)",
    )
    parser.add_argument("--dry-run", action="store_true", help="with --dsn: run, print the plan, roll back")
    parser.add_argument(
        "--only",
        metavar="HUB_ID",
        default=None,
        help="with --dsn: only this dedicated-connection hub's bank (e.g. sub-LZ_AEN-00, the toll hotfix); "
        "insert-only and guarded like --only-missing",
    )
    args = parser.parse_args(argv)
    if args.dry_run and not args.dsn:
        parser.error("--dry-run needs --dsn")
    if args.only and not args.dsn:
        parser.error("--only needs --dsn")
    if args.only:
        import psycopg

        with psycopg.connect(args.dsn) as conn:
            report = apply_statements(
                conn,
                _dedicated_statements(only_missing=True, only_hub=args.only),
                guard_existing=True,
                dry_run=args.dry_run,
                ready_hub=args.only,
            )
        print(f"{'applied' if report.committed else 'dry run'}: {args.only} ready: {report.hub_ready}")
        return 0 if report.hub_ready else 1

    blocks = None if args.blocks is None else [z for z in args.blocks.split(",") if z]
    config = load_fleet_config(args.fleet_config, args.scada_config, blocks)
    seed = build_seed(config, load_zone_territory(args.tdsp_tariffs), banks_per_feeder=args.banks_per_feeder)
    if args.dsn:
        import psycopg

        with psycopg.connect(args.dsn) as conn:
            report = apply_statements(
                conn,
                seed_statements(seed, only_missing=args.only_missing),
                guard_existing=args.only_missing,
                dry_run=args.dry_run,
            )
        remaining = sum(report.unmapped_after.values())
        print(
            f"{'applied' if report.committed else 'dry run'}: {len(seed.transformers)} home-bank transformers, "
            f"{len(seed.feeders)} feeders, {len(seed.substations)} substations, {len(seed.assets)} home-bank "
            f"assets in the config; unmapped after: {remaining}"
        )
        return 0 if remaining == 0 else 1
    sql = render_sql(seed, only_missing=args.only_missing)
    if args.out is not None:
        args.out.write_text(sql, encoding="utf-8")
    else:
        sys.stdout.write(sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
