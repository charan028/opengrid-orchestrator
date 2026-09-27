"""ogsim.scada awareness of substation-sited battery-set assets (OWNER DECISION D-29(b), 2026-09-26:
enabling the simulated Austin substation for the utility toll demo must also give ogsim.scada
measurements for it -- ScadaConfig.substation_assets mirrors FleetConfig.substation_assets)."""

from __future__ import annotations

from dataclasses import replace

from ogsim.common.config import MqttSettings, SubstationAssetConfig, load_scada_config
from ogsim.scada.aggregation import kw_to_kva
from ogsim.scada.runtime import ScadaEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")

SUBSTATION = SubstationAssetConfig(
    asset_id="sub-LZ_AEN-00", zone="LZ_AEN", rated_mw=20.0, duration_h=2.0, enabled=True
)


def _config(**overrides):
    base = {
        "mqtt": MQTT,
        "bank_count": 2,
        "zones": ("LZ_NORTH", "LZ_SOUTH"),
        "history_tsv_path": "/nonexistent/substation_history.tsv",
        "substation_assets": (SUBSTATION,),
        "mobile_units": (),  # the shipped scada.yaml's trucks are covered by test_fleet_mobile_units.py
    }
    base.update(overrides)
    return replace(load_scada_config(), **base)


def _expected_home_bank_count(config) -> int:
    """`config.bank_count` plus every ENABLED zone block's banks -- computed from the config itself
    (not hardcoded), so this stays correct regardless of which zone blocks the shipped default enables
    (LZ_LCRA/LZ_RAYBN are enabled by default: D-32, regulated/unavailable per D-37; `_config()` here doesn't
    override `zone_blocks`, so it inherits whatever the shipped scada.yaml currently declares)."""
    return config.bank_count + sum(b.banks for b in config.zone_blocks if b.enabled)


def test_disabled_substation_asset_adds_no_bank():
    config = _config(substation_assets=(replace(SUBSTATION, enabled=False),))
    engine = ScadaEngine(config, seed=1)
    assert "bank-sub-LZ_AEN-00" not in engine.bank_ids
    assert len(engine.bank_ids) == _expected_home_bank_count(config)


def test_enabled_substation_asset_gets_its_own_bank_with_its_own_zone():
    engine = ScadaEngine(_config(), seed=1)
    assert engine.bank_ids[-1] == "bank-sub-LZ_AEN-00"
    assert engine.zones[-1] == "LZ_AEN"


def test_substation_bank_kva_rating_is_derived_from_rated_mw_not_the_feeder_default():
    engine = ScadaEngine(_config(), seed=1)
    assert engine.kva_rating["bank-sub-LZ_AEN-00"] == kw_to_kva(20_000.0)
    assert engine.kva_rating["bank-sub-LZ_AEN-00"] > engine.config.bank_kva_rating_default * 10


def test_substation_bank_gets_no_residential_background_load():
    """A substation is Base-owned generation/storage, not a ~50-home feeder segment -- it must never
    get a share of the synthetic residential background-load model."""
    engine = ScadaEngine(_config(), seed=1)
    signals, _instructions = engine.tick(0.0)
    sub_signal = dict(signals)["scada/bank-sub-LZ_AEN-00"]
    # No telemetry ingested and no background load: the reading must be exactly 0, not some nonzero
    # residential-style noise/base-load figure.
    assert sub_signal["value"] == 0.0


def _kva_message(signals, bank_id: str) -> dict:
    """Bug fix, 2026-09-26 (R3): `tick()` now publishes both APPARENT_POWER_KVA and REAL_POWER_KW on
    the same `scada/<bank_id>` topic -- filter by `signal`, don't rely on `dict(signals)` collapse."""
    return next(
        m for topic, m in signals if topic == f"scada/{bank_id}" and m["signal"] == "APPARENT_POWER_KVA"
    )


def test_substation_telemetry_is_ingested_under_its_own_bank_id():
    engine = ScadaEngine(_config(), seed=1)
    engine.ingest_telemetry("sub-LZ_AEN-00", "bank-sub-LZ_AEN-00", -15_000.0)
    signals, _instructions = engine.tick(0.0)
    sub_signal = _kva_message(signals, "bank-sub-LZ_AEN-00")
    assert sub_signal["value"] == round(kw_to_kva(15_000.0), 3)


def test_substation_real_power_kw_signal_matches_the_pre_kva_reading():
    """Live bug fix, 2026-09-26 (R3, taken off Frank's #39): the guardian fails closed on unknown
    flow direction without a signed REAL_POWER_KW series -- the substation bank must publish one too,
    with + = import from the feeder, - = export (same sign as the telemetry p_kw that drives it)."""
    engine = ScadaEngine(_config(), seed=1)
    engine.ingest_telemetry("sub-LZ_AEN-00", "bank-sub-LZ_AEN-00", -15_000.0)  # discharging -> export
    signals, _instructions = engine.tick(0.0)
    kw_msg = next(
        m for topic, m in signals if topic == "scada/bank-sub-LZ_AEN-00" and m["signal"] == "REAL_POWER_KW"
    )
    assert kw_msg["unit"] == "kW"
    assert kw_msg["value"] == round(-15_000.0, 3)  # export, negative, matches the telemetry sign


def test_substation_asset_does_not_shrink_the_home_background_model():
    """Regression: the background-load model must stay sized to the HOME bank count, not the full
    roster including the substation -- otherwise every home bank's background share would be diluted
    by a bank that never contributes any of its own residential load."""
    config = _config()
    engine = ScadaEngine(config, seed=1)
    expected_home_banks = _expected_home_bank_count(config)
    assert engine._home_bank_count == expected_home_banks
    signals, _instructions = engine.tick(0.0)
    home_values = [v for topic, v in signals if not topic.endswith("bank-sub-LZ_AEN-00")]
    assert len(home_values) == expected_home_banks * 2  # 2 signals each (KVA + KW)
    assert all(_kva_message(signals, f"bank-{i:03d}")["value"] > 0.0 for i in range(config.bank_count))


def test_shipped_scada_config_has_the_austin_substation_enabled():
    """OWNER DECISION D-29(b): the shipped scada.yaml must mirror fleet.yaml's enabled Austin
    substation, not just declare it disabled."""
    config = load_scada_config()
    assets = {a.asset_id: a for a in config.substation_assets}
    assert "sub-LZ_AEN-00" in assets
    assert assets["sub-LZ_AEN-00"].enabled is True
    assert assets["sub-LZ_AEN-00"].zone == "LZ_AEN"
