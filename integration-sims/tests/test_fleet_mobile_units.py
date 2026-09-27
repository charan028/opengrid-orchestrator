"""Simulated mobile units (trucks, D-31, owner request 2026-09-26): each `simulate: true` entry in
fleet.yaml's `mobile_units:` is its own one-hub bank at its home station, publishes telemetry and a
MOBILE_STORAGE device_info, and never charges away from its home station."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from ogsim.common.config import FleetConfig, MobileUnitConfig, MqttSettings, ScadaConfig, load_fleet_config
from ogsim.fleet.device_info import build_device_info_messages
from ogsim.fleet.runtime import FleetEngine
from ogsim.fleet.state import build_fleet_state
from ogsim.scada.runtime import ScadaEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")
SCHEMA = Path(__file__).resolve().parents[2] / "interfaces" / "mqtt" / "device_info.schema.json"
SHIPPED = Path(__file__).resolve().parents[1] / "config" / "fleet.yaml"


def _truck(at_home: bool = True) -> MobileUnitConfig:
    return MobileUnitConfig(
        trailer_id="truck-dfw-01",
        home_station_id="hs-dfw-irving-01",
        simulate=True,
        bank_id="bank-truck-dfw-01",
        zone="LZ_NORTH",
        lat=32.8385,
        lon=-96.9730,
        at_home=at_home,
    )


def _config(*units: MobileUnitConfig) -> FleetConfig:
    return FleetConfig(mqtt=MQTT, hub_count=2, bank_count=1, zones=("LZ_NORTH",), mobile_units=units)


def test_registry_only_units_are_not_simulated():
    registry_only = MobileUnitConfig(trailer_id="trailer-mb-01", home_station_id="hs-austin-north-01")
    state = build_fleet_state(_config(registry_only), np.random.default_rng(0))
    assert state.hub_ids == ["hub-00000", "hub-00001"]


def test_a_simulated_truck_is_its_own_bank_at_its_home_station():
    state = build_fleet_state(_config(_truck()), np.random.default_rng(0))
    idx = state.hub_index["truck-dfw-01"]
    assert state.bank_ids[idx] == "bank-truck-dfw-01"
    assert state.zones[idx] == "LZ_NORTH"
    assert (state.lat_deg[idx], state.lon_deg[idx]) == (32.8385, -96.9730)
    assert state.p_kw_limit[idx] == 500.0 and state.e_kwh[idx] == 1000.0
    assert state.r_kwh[idx] == pytest.approx(200.0)
    assert state.pv_capacity_kw[idx] == 0.0
    assert bool(state.is_mobile[idx]) and not bool(state.charge_blocked[idx])
    assert not state.is_mobile[:2].any()


def test_truck_device_info_is_mobile_storage_and_schema_valid():
    config = _config(_truck())
    state = build_fleet_state(config, np.random.default_rng(0))
    messages = dict(build_device_info_messages(state, config, now=1_790_000_000.0))
    msg = messages["hub/truck-dfw-01/info"]
    assert msg["asset_class"] == "MOBILE_STORAGE" and msg["units"] == 1
    assert (msg["rated_kw"], msg["rated_kwh"], msg["reserve_floor_pct"]) == (500.0, 1000.0, 20.0)
    assert (msg["lat"], msg["lon"]) == (32.8385, -96.973)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(msg, schema)


def _charge(engine: FleetEngine, p_kw: float) -> float:
    idx = engine.state.hub_index["truck-dfw-01"]
    now = 1_790_000_000.0
    engine.state.p_kw_commanded[idx] = p_kw
    engine.state.lease_expires_at[idx] = now + 60.0
    engine.tick(now=now)
    engine.tick(now=now + 2.0)
    return float(engine.state.p_kw_applied[idx])


def test_a_truck_charges_at_home():
    engine = FleetEngine(_config(_truck(at_home=True)), seed=1)
    assert _charge(engine, 250.0) == pytest.approx(250.0, rel=1e-3)
    idx = engine.state.hub_index["truck-dfw-01"]
    assert engine.state.home_load_kw[idx] == 0.0 and engine.state.pv_kw[idx] == 0.0


def test_a_truck_away_from_home_never_charges_but_may_discharge():
    engine = FleetEngine(_config(_truck(at_home=False)), seed=1)
    assert _charge(engine, 250.0) == 0.0
    idx = engine.state.hub_index["truck-dfw-01"]
    assert engine.state.p_ch_max_kw[idx] == 0.0
    assert _charge(engine, -250.0) == pytest.approx(-250.0, rel=1e-3)


def test_scada_measures_each_simulated_truck_bank():
    scada = ScadaEngine(ScadaConfig(mqtt=MQTT, bank_count=1, zones=("LZ_NORTH",), mobile_units=(_truck(),)))
    assert scada.bank_ids[-1] == "bank-truck-dfw-01"
    assert scada.kva_rating["bank-truck-dfw-01"] >= 500.0


def test_shipped_fleet_yaml_simulates_eight_trucks():
    config = load_fleet_config(str(SHIPPED))
    trucks = [u for u in config.mobile_units if u.simulate]
    assert len(trucks) == 8
    assert all(u.at_home and u.sim_bank_id == f"bank-{u.trailer_id}" for u in trucks)
