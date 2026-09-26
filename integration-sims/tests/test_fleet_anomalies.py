"""Tests for every FLEET_* anomaly type: applies while active, reverts on
duration elapse. Uses ogsim.fleet.runtime.FleetEngine with a small fleet."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import sign
from ogsim.common.scenario import WIRE_TYPE_TO_CATALOGUE_ID
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")

_CATALOGUE_ID_TO_WIRE_TYPE = {v: k for k, v in WIRE_TYPE_TO_CATALOGUE_ID.items()}

# Which target.kind the schema-conformant wire message carries for each
# FLEET_* anomaly type used by this test file (mirrors ogsim.control.catalogue).
_TARGET_KIND = {
    "hub_offline": "hub",
    "zone_mass_disconnect": "zone",
    "not_following_commands": "hub",
    "inverter_trip": "hub",
    "soc_sensor_drift": "hub",
    "telemetry_delay_burst": "hub",
    "lease_loss": "hub",
    "clock_skew": "hub",
    "reserve_floor_pressure": "hub",
}


@pytest.fixture
def engine() -> FleetEngine:
    config = replace(
        load_fleet_config(), mqtt=MQTT, hub_count=8, bank_count=2, zones=("LZ_NORTH", "LZ_SOUTH")
    )
    return FleetEngine(config, seed=42)


def _inject(engine: FleetEngine, anomaly_type: str, target: str, params: dict, start: float, duration: float):
    raw = {
        "id": f"anom-{anomaly_type}",
        "target": {"kind": _TARGET_KIND[anomaly_type], "ref": target},
        "type": _CATALOGUE_ID_TO_WIRE_TYPE[anomaly_type],
        "params": params,
        "start": datetime.fromtimestamp(start, tz=UTC).isoformat(),
        "duration_s": duration,
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
    assert math.isnan(engine.anomalies.modifiers.forced_home_load_kw[idx])


def _stop_event(signing_key: Ed25519PrivateKey, *, action: str) -> dict:
    event = {
        "stop_id": str(uuid.uuid4()),
        "scope": "bank",
        "scope_id": "bank-000",
        "action": action,
        "reason": "test",
        "issued_by": "operator-1",
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "approver_ref": None,
    }
    fields = ("stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref")
    signing_fields = {k: event[k] for k in fields if k in event}
    event["key_id"] = "test-key"
    event["signature"] = sign(signing_key, signing_fields)
    return event


def test_handle_stop_event_applies_a_correctly_signed_engage(engine: FleetEngine) -> None:
    safestop_key = Ed25519PrivateKey.generate()
    guardian_key = Ed25519PrivateKey.generate()
    event = _stop_event(safestop_key, action="ENGAGE")
    applied = engine.handle_stop_event(event, safestop_key.public_key(), guardian_key.public_key())
    assert applied is True
    assert engine.stops.is_stopped("LZ_NORTH", "bank-000") is True


def test_handle_stop_event_rejects_engage_signed_by_guardian_key(engine: FleetEngine) -> None:
    safestop_key = Ed25519PrivateKey.generate()
    guardian_key = Ed25519PrivateKey.generate()
    event = _stop_event(guardian_key, action="ENGAGE")
    applied = engine.handle_stop_event(event, safestop_key.public_key(), guardian_key.public_key())
    assert applied is False
    # Rejected -- never reaches the StopRegistry.
    assert engine.stops.is_stopped("LZ_NORTH", "bank-000") is False


def test_tampered_unsigned_command_selftest_is_rejected(engine: FleetEngine) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public_key = Ed25519PrivateKey.generate().public_key()
    bank_id = engine.state.bank_ids[0]
    verdict = engine.self_test_tampered_unsigned_command(bank_id, public_key)
    assert verdict.accepted is False
    assert verdict.reject_reason == "BAD_SIGNATURE"


# ---- demo gap #14, 2026-09-26: tampered_unsigned_command had no observable effect ----------------
# The self-test above always worked in isolation; it was simply never invoked when the anomaly
# actually started, and no rejected ack ever reached the orchestrator to trace.


def _inject_tampered_unsigned_command(engine: FleetEngine, target: str, guardian_public_key):
    raw = {
        "id": str(uuid.uuid4()),
        "target": {"kind": "hub" if target in engine.state.hub_index else "bank", "ref": target},
        "type": "FLEET_TAMPERED_UNSIGNED_COMMAND",
        "params": {},
        "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
        "duration_s": 0,
    }
    return engine.handle_scenario_cmd(raw, guardian_public_key)


def test_tampered_unsigned_command_on_anomaly_start_returns_a_rejected_verdict(
    engine: FleetEngine,
) -> None:
    guardian_key = Ed25519PrivateKey.generate()
    hub_id = engine.state.hub_ids[0]

    verdict = _inject_tampered_unsigned_command(engine, hub_id, guardian_key.public_key())

    assert verdict is not None
    assert verdict.accepted is False
    assert verdict.reject_reason == "BAD_SIGNATURE"
    assert verdict.hub_id is not None


def test_tampered_unsigned_command_resolves_a_bank_target_too(engine: FleetEngine) -> None:
    guardian_key = Ed25519PrivateKey.generate()
    bank_id = engine.state.bank_ids[0]

    verdict = _inject_tampered_unsigned_command(engine, bank_id, guardian_key.public_key())

    assert verdict is not None
    assert verdict.accepted is False


def test_tampered_unsigned_command_without_a_public_key_is_inert(engine: FleetEngine) -> None:
    """`handle_scenario_cmd`'s `guardian_public_key` is optional (every pre-existing caller that
    doesn't pass one must keep working unchanged) -- omitting it must not raise, it just skips the
    self-test entirely."""
    hub_id = engine.state.hub_ids[0]
    assert _inject_tampered_unsigned_command_no_key(engine, hub_id) is None


def _inject_tampered_unsigned_command_no_key(engine: FleetEngine, target: str):
    raw = {
        "id": str(uuid.uuid4()),
        "target": {"kind": "hub", "ref": target},
        "type": "FLEET_TAMPERED_UNSIGNED_COMMAND",
        "params": {},
        "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
        "duration_s": 0,
    }
    return engine.handle_scenario_cmd(raw)


def test_tampered_unsigned_command_unknown_target_returns_none(engine: FleetEngine) -> None:
    guardian_key = Ed25519PrivateKey.generate()
    result = _inject_tampered_unsigned_command(engine, "no-such-hub-or-bank", guardian_key.public_key())
    assert result is None
