"""ogsim.fleet.state: optional extra load-zone blocks (build phase 2026-09-26, Austin Energy
`LZ_AEN`/CPS Energy `LZ_CPS`, `docs/orchestrator/07-delivery/08-market-model-two-markets.md`).

Cross-package fixture test: `dev/fixtures/fleet_topology_zone_blocks_expected.json` is the single
source of truth both `ogsim.fleet.state` and `opengrid.fleet.seed` must reproduce for the same
base+zone_blocks config (BUILD.md S1 -- the packages share no code, only the id scheme). The mirror
test lives at `orchestrator/tests/unit/fleet/test_seed_zone_blocks.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ogsim.common.config import FleetConfig, MqttSettings, ZoneBlockConfig
from ogsim.fleet.state import build_fleet_state

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "dev" / "fixtures" / "fleet_topology_zone_blocks_expected.json"
)


def _load_fixture() -> dict:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _config_from_fixture(fixture: dict) -> FleetConfig:
    base = fixture["base"]
    zone_blocks = tuple(
        ZoneBlockConfig(
            zone=b["zone"], banks=b["banks"], homes_per_bank=b["homes_per_bank"], enabled=b["enabled"]
        )
        for b in fixture["zone_blocks"]
    )
    return FleetConfig(
        mqtt=MQTT,
        hub_count=base["hub_count"],
        bank_count=base["bank_count"],
        zones=tuple(base["zones"]),
        dual_unit_share=base["dual_unit_share"],
        e_kwh_default=base["e_kwh_default"],
        e_kwh_dual_unit=base["e_kwh_dual_unit"],
        p_kw_default=base["p_kw_default"],
        p_kw_dual_unit=base["p_kw_dual_unit"],
        reserve_frac_default=base["reserve_frac_default"],
        zone_blocks=zone_blocks,
    )


def test_zone_block_topology_matches_shared_fixture() -> None:
    fixture = _load_fixture()
    config = _config_from_fixture(fixture)
    state = build_fleet_state(config, np.random.default_rng(0))

    actual_hubs = [
        {
            "hub_id": hub_id,
            "bank_id": bank_id,
            "zone": zone,
            "dual_unit": bool(e_kwh == config.e_kwh_dual_unit),
        }
        for hub_id, bank_id, zone, e_kwh in zip(
            state.hub_ids, state.bank_ids, state.zones, state.e_kwh, strict=True
        )
    ]
    assert actual_hubs == fixture["expected_hubs"]

    banks_seen: dict[str, str] = {}
    for bank_id, zone in zip(state.bank_ids, state.zones, strict=True):
        banks_seen.setdefault(bank_id, zone)
    actual_banks = [{"bank_id": bank_id, "zone": zone} for bank_id, zone in sorted(banks_seen.items())]
    assert actual_banks == fixture["expected_banks"]


def test_disabled_zone_block_reserves_no_ids() -> None:
    fixture = _load_fixture()
    config = _config_from_fixture(fixture)
    state = build_fleet_state(config, np.random.default_rng(0))

    assert "LZ_CPS" not in state.zones
    assert len(state.hub_ids) == 8
    assert len(set(state.bank_ids)) == 3


def test_base_ids_unchanged_when_zone_block_enabled() -> None:
    fixture = _load_fixture()
    config = _config_from_fixture(fixture)
    base_only = FleetConfig(
        mqtt=MQTT,
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

    with_block = build_fleet_state(config, np.random.default_rng(0))
    without_block = build_fleet_state(base_only, np.random.default_rng(0))

    n = config.hub_count
    assert with_block.hub_ids[:n] == without_block.hub_ids
    assert with_block.bank_ids[:n] == without_block.bank_ids
    assert with_block.zones[:n] == without_block.zones


def test_dual_unit_homes_are_10_per_new_bank_at_50_homes_per_bank() -> None:
    config = FleetConfig(
        mqtt=MQTT,
        hub_count=1,
        bank_count=1,
        zones=("LZ_NORTH",),
        zone_blocks=(ZoneBlockConfig(zone="LZ_AEN", banks=10, homes_per_bank=50, enabled=True),),
    )
    state = build_fleet_state(config, np.random.default_rng(0))

    dual_unit_by_bank: dict[str, int] = {}
    for bank_id, zone, e_kwh in zip(state.bank_ids, state.zones, state.e_kwh, strict=True):
        if zone == "LZ_AEN" and e_kwh == config.e_kwh_dual_unit:
            dual_unit_by_bank[bank_id] = dual_unit_by_bank.get(bank_id, 0) + 1
    assert len(dual_unit_by_bank) == 10
    assert all(count == 10 for count in dual_unit_by_bank.values())
