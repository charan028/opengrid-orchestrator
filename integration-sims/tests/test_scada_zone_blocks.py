"""Tests for ogsim.scada's zone-block coverage (build phase, 2026-09-26): SCADA must build
background load and bank signals for every bank in the fleet topology, base plus enabled
zone blocks (Austin Energy `LZ_AEN`/CPS Energy `LZ_CPS`), not just the base `bank_count`
banks. Blocks stay disabled by default -- no behaviour change today."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from ogsim.common.config import MqttSettings, ZoneBlockConfig, load_fleet_config, load_scada_config
from ogsim.fleet.state import build_fleet_state
from ogsim.scada.runtime import ScadaEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")

AEN_BLOCK = ZoneBlockConfig(zone="LZ_AEN", banks=10, homes_per_bank=50, enabled=True)
CPS_BLOCK_DISABLED = ZoneBlockConfig(zone="LZ_CPS", banks=10, homes_per_bank=50, enabled=False)


@pytest.fixture
def base_config():
    # Hermetic: no substation history file, so both engines use the synthetic base (the server has one,
    # where the per-bank base is the history mean split over the bank count). `substation_assets=()`:
    # this file is about ZONE BLOCKS specifically -- the shipped scada.yaml's own substation asset
    # (D-29(b)) is covered by test_scada_substation_asset.py instead, so it's isolated out here.
    return replace(
        load_scada_config(),
        mqtt=MQTT,
        bank_count=40,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        history_tsv_path="/nonexistent/substation_history.tsv",
        substation_assets=(),
    )


# ---------------------------------------------------------------------------
# Disabled by default: no behaviour change today.
# ---------------------------------------------------------------------------


def test_disabled_zone_blocks_do_not_change_the_base_bank_roster(base_config) -> None:
    """A disabled block adds nothing to the roster; an enabled one appends after the base banks in
    `zone_blocks:` list order. Expected counts are computed from `base_config.zone_blocks` itself (not
    hardcoded), so this test doesn't need updating every time the shipped defaults change which blocks
    are on (OWNER DECISION D-32, 2026-09-26, enables LZ_LCRA/LZ_RAYBN by default)."""
    engine = ScadaEngine(base_config, seed=1)
    assert engine.bank_ids[: base_config.bank_count] == [
        f"bank-{i:03d}" for i in range(base_config.bank_count)
    ]
    expected_total = base_config.bank_count + sum(b.banks for b in base_config.zone_blocks if b.enabled)
    assert len(engine.bank_ids) == expected_total


def test_shipped_scada_config_ships_aen_cps_disabled_lcra_raybn_enabled() -> None:
    """OWNER DECISION D-32, 2026-09-26: the free-market zones LZ_LCRA/LZ_RAYBN are live in the shipped
    default; the regulated zones LZ_AEN/LZ_CPS stay off (production enables AEN via its own override)."""
    config = load_scada_config()
    enabled_by_zone = {block.zone: block.enabled for block in config.zone_blocks}
    assert enabled_by_zone == {"LZ_AEN": False, "LZ_CPS": False, "LZ_LCRA": True, "LZ_RAYBN": True}


# ---------------------------------------------------------------------------
# AEN block enabled: bank-040..049 covered.
# ---------------------------------------------------------------------------


@pytest.fixture
def aen_config(base_config):
    return replace(base_config, zone_blocks=(AEN_BLOCK, CPS_BLOCK_DISABLED))


def test_aen_block_enabled_covers_bank_040_through_049(aen_config) -> None:
    engine = ScadaEngine(aen_config, seed=1)
    assert engine.bank_ids == [f"bank-{i:03d}" for i in range(40)] + [f"bank-{i:03d}" for i in range(40, 50)]
    assert engine.zones[40:50] == ["LZ_AEN"] * 10
    # The disabled CPS block must not reserve any ids at all (ZoneBlockConfig's docstring).
    assert "bank-050" not in engine.bank_ids


def test_aen_block_banks_emit_signals_each_under_kva_rating(aen_config) -> None:
    engine = ScadaEngine(aen_config, seed=1)
    signals, _instructions = engine.tick(0.0)
    # Bug fix, 2026-09-26 (R3): `tick()` now publishes both APPARENT_POWER_KVA and REAL_POWER_KW on
    # the same `scada/<bank_id>` topic, so a plain `dict(signals)` would collapse to just one of them.
    kva_by_topic = {topic: m for topic, m in signals if m["signal"] == "APPARENT_POWER_KVA"}

    for i in range(40, 50):
        bank_id = f"bank-{i:03d}"
        topic = f"scada/{bank_id}"
        assert topic in kva_by_topic, f"no APPARENT_POWER_KVA signal published for AEN bank {bank_id}"
        msg = kva_by_topic[topic]
        assert msg["bank_id"] == bank_id
        assert msg["signal"] == "APPARENT_POWER_KVA"
        assert 0.0 <= msg["value"] < 600.0
        assert msg["quality"] == "good"


def test_aen_block_banks_get_their_own_kva_rating_and_overload_eligibility(aen_config) -> None:
    engine = ScadaEngine(aen_config, seed=1)
    for i in range(40, 50):
        assert engine.kva_rating[f"bank-{i:03d}"] == aen_config.bank_kva_rating_default


def test_aen_block_banks_can_receive_targeted_anomalies(aen_config) -> None:
    """A scenario targeting an AEN bank by id, or the whole LZ_AEN zone, must resolve to
    real banks in `engine.anomalies` -- not silently no-op because the block bank wasn't in
    the anomaly manager's roster."""
    engine = ScadaEngine(aen_config, seed=1)
    assert engine.handle_scenario_cmd(
        {
            "id": "anom-aen-overload",
            "target": {"kind": "zone", "ref": "LZ_AEN"},
            "type": "SCADA_BANK_OVERLOAD",
            "params": {"kva_over_rating_pct": 50.0},
            "start": "2026-09-26T00:00:00.000Z",
            "duration_s": 60,
        }
    )
    for i in range(40, 50):
        assert engine.anomalies.modifiers[f"bank-{i:03d}"].overload_pct == 50.0
    # Base-fleet banks are untouched by a zone-scoped anomaly.
    assert engine.anomalies.modifiers["bank-000"].overload_pct == 0.0


# ---------------------------------------------------------------------------
# Background load: per-bank share scaled consistently with the existing per-bank fix
# (background.py's history_mean / bank_count division), now over the FULL bank roster.
# ---------------------------------------------------------------------------


def test_background_load_model_is_sized_to_the_full_bank_roster(aen_config) -> None:
    engine = ScadaEngine(aen_config, seed=1)
    assert engine.background.per_bank_multiplier.shape == (50,)


def test_tick_does_not_raise_a_shape_mismatch_with_blocks_enabled(aen_config) -> None:
    """Regression: the noise array used to be sized to config.bank_count (40) while
    self.background could be sized to the full roster -- a broadcast error waiting to
    happen once a block was enabled."""
    engine = ScadaEngine(aen_config, seed=1)
    signals, instructions = engine.tick(0.0)
    # 2 signals per bank now (APPARENT_POWER_KVA + REAL_POWER_KW, bug fix 2026-09-26, R3).
    assert len(signals) == 50 * 2


def test_background_load_per_bank_share_matches_base_fleet_semantics(base_config, aen_config) -> None:
    """Both engines see the SAME synthetic base_kw (no history file in this test environment,
    background.py falls back to config.base_load_kw_default) -- confirms the AEN block's banks
    get their own per-bank multiplier draw from the same model, not a re-used/duplicated base
    fleet value."""
    base_engine = ScadaEngine(base_config, seed=1)
    aen_engine = ScadaEngine(aen_config, seed=1)
    assert (
        base_engine.background.base_kw
        == aen_engine.background.base_kw
        == pytest.approx(aen_config.base_load_kw_default)
    )
    # aen_config REPLACES zone_blocks entirely with its own (AEN_BLOCK, CPS_BLOCK_DISABLED), so its
    # expected total is self-contained; base_config's is computed from its own zone_blocks (not
    # hardcoded), since OWNER DECISION D-32 (2026-09-26) may enable some of the shipped defaults it
    # otherwise inherits.
    assert aen_engine.background.per_bank_multiplier.shape[0] == base_config.bank_count + AEN_BLOCK.banks
    base_extra = sum(b.banks for b in base_config.zone_blocks if b.enabled)
    assert base_engine.background.per_bank_multiplier.shape[0] == base_config.bank_count + base_extra


# ---------------------------------------------------------------------------
# Cross-package parity: ogsim.scada's bank roster matches ogsim.fleet's, for the SAME config
# (BUILD.md S1-style "share no code between sibling sim modules": ogsim.fleet.state and
# ogsim.common.config.bank_topology independently compute the same numbering scheme; this
# proves them equal by test, not by import).
# ---------------------------------------------------------------------------


def test_scada_bank_roster_matches_fleet_topology_with_aen_enabled() -> None:
    fleet_config = replace(
        load_fleet_config(),
        mqtt=MQTT,
        hub_count=80,
        bank_count=40,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        zone_blocks=(AEN_BLOCK, CPS_BLOCK_DISABLED),
    )
    scada_config = replace(
        load_scada_config(),
        mqtt=MQTT,
        bank_count=40,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        zone_blocks=(AEN_BLOCK, CPS_BLOCK_DISABLED),
    )

    fleet_state = build_fleet_state(fleet_config, np.random.default_rng(1))
    scada_engine = ScadaEngine(scada_config, seed=1)

    assert set(scada_engine.bank_ids) == set(fleet_state.bank_ids)
    assert "bank-040" in scada_engine.bank_ids
    assert "bank-049" in scada_engine.bank_ids
