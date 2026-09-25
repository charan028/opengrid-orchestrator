"""Tests for every FLEET_* anomaly type: applies while active, reverts on
duration elapse. Uses ogsim.fleet.runtime.FleetEngine with a small fleet."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


@pytest.fixture
def engine() -> FleetEngine:
    config = replace(
        load_fleet_config(), mqtt=MQTT, hub_count=8, bank_count=2, zones=("LZ_NORTH", "LZ_SOUTH")
    )
    return FleetEngine(config, seed=42)


def _inject(engine: FleetEngine, anomaly_type: str, target: str, params: dict, start: float, duration: float):
    raw = {
        "id": f"anom-{anomaly_type}",
        "target": target,
        "type": anomaly_type,
        "params": params,
        "start": start,
        "duration": duration,
    }
    return engine.handle_scenario_cmd(raw)


def test_hub_offline_suppresses_and_restores_telemetry(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    _inject(engine, "hub_offline", hub_id, {}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert hub_id not in [msg["hub_id"] for _, msg in engine.telemetry_messages(1.0)]
    engine.tick(11.0)
    assert hub_id in [msg["hub_id"] for _, msg in engine.telemetry_messages(11.0)]


def test_zone_mass_disconnect_suppresses_whole_zone(engine: FleetEngine) -> None:
    _inject(engine, "zone_mass_disconnect", "LZ_NORTH", {}, start=0.0, duration=10.0)
    engine.tick(1.0)
    remaining_zones = {
        engine.state.zones[engine.state.hub_index[m["hub_id"]]] for _, m in engine.telemetry_messages(1.0)
    }
    assert "LZ_NORTH" not in remaining_zones
    engine.tick(11.0)
    remaining_zones_after = {
        engine.state.zones[engine.state.hub_index[m["hub_id"]]] for _, m in engine.telemetry_messages(11.0)
    }
    assert "LZ_NORTH" in remaining_zones_after


def test_not_following_commands_partial_halves_follow_fraction(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    _inject(engine, "not_following_commands", hub_id, {"mode": "partial"}, start=0.0, duration=10.0)
    idx = engine.state.hub_index[hub_id]
    engine.tick(1.0)
    assert engine.anomalies.modifiers.follow_fraction[idx] == 0.5
    engine.tick(11.0)
    assert engine.anomalies.modifiers.follow_fraction[idx] == 1.0


def test_not_following_commands_none_zeroes_follow_fraction(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    _inject(engine, "not_following_commands", hub_id, {"mode": "none"}, start=0.0, duration=10.0)
    idx = engine.state.hub_index[hub_id]
    engine.tick(1.0)
    assert engine.anomalies.modifiers.follow_fraction[idx] == 0.0


def test_inverter_trip_forces_fault_and_recovers(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    _inject(engine, "inverter_trip", hub_id, {}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert engine.state.health[idx] == "fault"
    assert engine.state.fault_code[idx] == "INVERTER_TRIP"
    engine.tick(11.0)
    assert engine.state.health[idx] == "online"
    assert engine.state.fault_code[idx] is None


def test_soc_sensor_drift_accumulates_and_resets(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    _inject(engine, "soc_sensor_drift", hub_id, {"drift_kwh_per_min": 6.0}, start=0.0, duration=10.0)
    engine.tick(1.0)  # dt = 1s -> 0.1 kWh drift
    assert engine.anomalies.modifiers.soc_drift_kwh[idx] > 0.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers.soc_drift_kwh[idx] == 0.0


def test_telemetry_delay_burst_suppresses_then_restores(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    _inject(engine, "telemetry_delay_burst", hub_id, {"delay_s": 5.0}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert hub_id not in [msg["hub_id"] for _, msg in engine.telemetry_messages(1.0)]
    engine.tick(11.0)
    assert hub_id in [msg["hub_id"] for _, msg in engine.telemetry_messages(11.0)]


def test_lease_loss_forces_immediate_expiry(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    engine.state.lease_expires_at[idx] = 1000.0
    _inject(engine, "lease_loss", hub_id, {}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert engine.state.lease_expires_at[idx] < 1.0


def test_clock_skew_offsets_telemetry_timestamp_and_reverts(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    _inject(engine, "clock_skew", hub_id, {"skew_s": 300.0}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers.clock_skew_s[idx] == 300.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers.clock_skew_s[idx] == 0.0


def test_reserve_floor_pressure_overrides_home_load_and_reverts(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    _inject(engine, "reserve_floor_pressure", hub_id, {"home_load_kw": 9.0}, start=0.0, duration=10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers.forced_home_load_kw[idx] == 9.0
    engine.tick(11.0)
    import math

    assert math.isnan(engine.anomalies.modifiers.forced_home_load_kw[idx])


def test_tampered_unsigned_command_selftest_is_rejected(engine: FleetEngine) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public_key = Ed25519PrivateKey.generate().public_key()
    bank_id = engine.state.bank_ids[0]
    verdict = engine.self_test_tampered_unsigned_command(bank_id, public_key)
    assert verdict.accepted is False
    assert verdict.reject_reason == "BAD_SIGNATURE"
