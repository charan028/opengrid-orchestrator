"""opengrid.fleet.seed: optional extra load-zone blocks (build phase 2026-09-26, Austin Energy
`LZ_AEN`/CPS Energy `LZ_CPS`, `docs/orchestrator/07-delivery/08-market-model-two-markets.md`).

Cross-package fixture test: `dev/fixtures/fleet_topology_zone_blocks_expected.json` is the single
source of truth both `opengrid.fleet.seed` and `ogsim.fleet.state` must reproduce for the same
base+zone_blocks config (BUILD.md S1 -- the packages share no code, only the id scheme). The mirror
test lives at `integration-sims/tests/test_fleet_zone_blocks.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from opengrid.fleet.seed import SimFleetTopologyConfig, ZoneBlockConfig, build_topology

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[4] / "dev" / "fixtures" / "fleet_topology_zone_blocks_expected.json"
)


def _load_fixture() -> dict:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _config_from_fixture(fixture: dict) -> tuple[SimFleetTopologyConfig, list[ZoneBlockConfig]]:
    base = fixture["base"]
    zone_blocks = [
        ZoneBlockConfig(
            zone=b["zone"], banks=b["banks"], homes_per_bank=b["homes_per_bank"], enabled=b["enabled"]
        )
        for b in fixture["zone_blocks"]
    ]
    config = SimFleetTopologyConfig(
        hub_count=base["hub_count"],
        bank_count=base["bank_count"],
        zones=tuple(base["zones"]),
        dual_unit_share=base["dual_unit_share"],
        e_kwh_default=base["e_kwh_default"],
        e_kwh_dual_unit=base["e_kwh_dual_unit"],
        p_kw_default=base["p_kw_default"],
        p_kw_dual_unit=base["p_kw_dual_unit"],
        reserve_frac_default=base["reserve_frac_default"],
        zone_blocks=tuple(zone_blocks),
    )
    return config, zone_blocks


def test_zone_block_topology_matches_shared_fixture() -> None:
    fixture = _load_fixture()
    config, _ = _config_from_fixture(fixture)

    topology = build_topology(config)

    actual_hubs = [
        {
            "hub_id": hub.hub_id,
            "bank_id": hub.bank_id,
            "zone": hub.zone,
            "dual_unit": hub.e_kwh == config.e_kwh_dual_unit,
        }
        for hub in topology.hubs
    ]
    assert actual_hubs == fixture["expected_hubs"]

    actual_banks = [{"bank_id": bank.bank_id, "zone": bank.zone} for bank in topology.banks]
    assert actual_banks == fixture["expected_banks"]


def test_disabled_zone_block_reserves_no_ids() -> None:
    """LZ_CPS is `enabled: false` in the fixture: it must not appear anywhere, and must not shift the
    enabled LZ_AEN block's numbering (hub/bank id stability when a block is merely defined but off)."""
    fixture = _load_fixture()
    config, _ = _config_from_fixture(fixture)

    topology = build_topology(config)

    assert not any(hub.zone == "LZ_CPS" for hub in topology.hubs)
    assert not any(bank.zone == "LZ_CPS" for bank in topology.banks)
    assert len(topology.hubs) == 8
    assert len(topology.banks) == 3


def test_base_ids_unchanged_when_zone_block_enabled() -> None:
    """Hub/bank id stability (build phase requirement): enabling an Austin/CPS block must not change
    any base-fleet hub or bank's id, zone or dual-unit flag."""
    fixture = _load_fixture()
    config, _ = _config_from_fixture(fixture)
    base_only = SimFleetTopologyConfig(
        hub_count=config.hub_count,
        bank_count=config.bank_count,
        zones=config.zones,
        dual_unit_share=config.dual_unit_share,
        e_kwh_default=config.e_kwh_default,
        e_kwh_dual_unit=config.e_kwh_dual_unit,
        p_kw_default=config.p_kw_default,
        p_kw_dual_unit=config.p_kw_dual_unit,
        reserve_frac_default=config.reserve_frac_default,
    )

    with_block = build_topology(config)
    without_block = build_topology(base_only)

    assert with_block.hubs[: config.hub_count] == without_block.hubs
    assert with_block.banks[: config.bank_count] == without_block.banks


def test_new_hubs_and_banks_start_after_base_range() -> None:
    fixture = _load_fixture()
    config, _ = _config_from_fixture(fixture)
    topology = build_topology(config)

    new_hubs = topology.hubs[config.hub_count :]
    new_banks = topology.banks[config.bank_count :]
    assert [h.hub_id for h in new_hubs] == ["hub-00004", "hub-00005", "hub-00006", "hub-00007"]
    assert [b.bank_id for b in new_banks] == ["bank-002"]


def test_zone_block_feeder_id_is_per_zone() -> None:
    config = SimFleetTopologyConfig(
        hub_count=2,
        bank_count=1,
        zones=("LZ_NORTH",),
        zone_blocks=(ZoneBlockConfig(zone="LZ_AEN", banks=12, homes_per_bank=50, enabled=True),),
    )
    topology = build_topology(config, banks_per_feeder=5)

    aen_banks = [b for b in topology.banks if b.zone == "LZ_AEN"]
    assert len(aen_banks) == 12
    assert aen_banks[0].feeder_id == "feeder-LZ_AEN-00"
    assert aen_banks[4].feeder_id == "feeder-LZ_AEN-00"
    assert aen_banks[5].feeder_id == "feeder-LZ_AEN-01"


def test_dual_unit_homes_are_10_per_new_bank_at_50_homes_per_bank() -> None:
    """Build requirement: dual-unit homes are 10 per bank in the new banks too (same 0.2 share, 50
    homes/bank as the base fleet)."""
    config = SimFleetTopologyConfig(
        hub_count=1,
        bank_count=1,
        zones=("LZ_NORTH",),
        zone_blocks=(ZoneBlockConfig(zone="LZ_AEN", banks=10, homes_per_bank=50, enabled=True),),
    )
    topology = build_topology(config)

    aen_hubs = [h for h in topology.hubs if h.zone == "LZ_AEN"]
    dual_unit_by_bank: dict[str, int] = {}
    for hub in aen_hubs:
        if hub.e_kwh == config.e_kwh_dual_unit:
            dual_unit_by_bank[hub.bank_id] = dual_unit_by_bank.get(hub.bank_id, 0) + 1
    assert len(dual_unit_by_bank) == 10
    assert all(count == 10 for count in dual_unit_by_bank.values())
