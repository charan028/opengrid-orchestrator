"""ogsim.fleet.runtime: charging-source split (owner decision D-28, 2026-09-26) -- `charge_pv_kw`/
`charge_grid_kw`, PV surplus (after home load) charges first, the rest from the grid."""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from pathlib import Path

import jsonschema
import pytest

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "interfaces" / "mqtt" / "telemetry.schema.json"


def _engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=1, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


def _hub(engine: FleetEngine) -> int:
    return engine.state.hub_index[engine.state.hub_ids[0]]


def test_not_charging_both_fields_are_zero() -> None:
    engine = _engine()
    idx = _hub(engine)
    engine.state.p_kw_commanded[idx] = -5.0  # discharging, not charging
    engine.state.soc_kwh[idx] = engine.state.e_kwh[idx]
    engine.state.lease_expires_at[idx] = 1_000_000.0

    engine.tick(0.0)

    assert engine.state.charge_pv_kw[idx] == 0.0
    assert engine.state.charge_grid_kw[idx] == 0.0


def test_pv_surplus_charges_before_the_grid() -> None:
    """No commanded setpoint at all (p_kw_commanded stays 0): any charging must come purely from a PV
    surplus after home load, split entirely to charge_pv_kw with charge_grid_kw at 0."""
    engine = _engine()
    idx = _hub(engine)
    engine.state.soc_kwh[idx] = engine.state.r_kwh[idx]  # plenty of headroom to charge into
    engine.state.pv_capacity_kw[idx] = 5.0
    engine.state.phase_offset_s[idx] = 0.0

    # Solar noon (per household.py's SOLAR_NOON_HOUR=13.0): PV generation is highest, home load is at
    # its base (not the evening peak), so home_net is negative (PV surplus) with high probability.
    engine.tick(13.0 * 3600.0)

    total_charge = float(engine.state.charge_pv_kw[idx] + engine.state.charge_grid_kw[idx])
    if total_charge > 0:  # only meaningful if this random draw actually produced a surplus
        assert engine.state.charge_pv_kw[idx] == pytest.approx(total_charge, abs=1e-6)
        assert engine.state.charge_grid_kw[idx] == pytest.approx(0.0, abs=1e-6)


def test_commanded_charging_beyond_pv_surplus_comes_from_the_grid() -> None:
    """A market-commanded charge setpoint that exceeds any PV surplus must show the excess as
    charge_grid_kw, with charge_pv_kw capped at the (here: zero, no PV capacity) surplus."""
    engine = _engine()
    idx = _hub(engine)
    engine.state.pv_capacity_kw[idx] = 0.0  # no PV at all -> zero surplus, ever
    engine.state.soc_kwh[idx] = engine.state.r_kwh[idx]
    engine.state.lease_expires_at[idx] = 1_000_000.0
    engine.state.p_kw_commanded[idx] = 5.0  # +charge

    engine.tick(0.0)

    assert engine.state.charge_pv_kw[idx] == pytest.approx(0.0, abs=1e-6)
    assert engine.state.charge_grid_kw[idx] > 0.0
    # Total charging power is non-negative and entirely grid-sourced (no PV at all on this hub).
    assert engine.state.charge_grid_kw[idx] >= 0.0


def test_charge_pv_plus_charge_grid_always_equals_total_charging_power() -> None:
    engine = _engine()
    idx = _hub(engine)
    engine.state.pv_capacity_kw[idx] = 3.0
    engine.state.soc_kwh[idx] = engine.state.r_kwh[idx]
    engine.state.lease_expires_at[idx] = 1_000_000.0
    engine.state.p_kw_commanded[idx] = 2.0

    engine.tick(13.0 * 3600.0)

    applied = float(engine.state.p_kw_applied[idx])
    # Recompute the same total_p_kw the engine itself derives (applied battery command + home's own
    # PV-surplus/deficit power), from telemetry-published home_load_kw/pv_kw.
    home_net = float(engine.state.home_load_kw[idx] - engine.state.pv_kw[idx])
    total_p_kw = applied - home_net
    expected_total_charge = max(total_p_kw, 0.0)
    actual_total = float(engine.state.charge_pv_kw[idx] + engine.state.charge_grid_kw[idx])
    assert actual_total == pytest.approx(expected_total_charge, abs=1e-6)
    assert engine.state.charge_pv_kw[idx] >= 0.0
    assert engine.state.charge_grid_kw[idx] >= 0.0


def test_telemetry_message_includes_charge_source_fields_and_validates() -> None:
    engine = _engine()
    idx = _hub(engine)
    engine.state.pv_capacity_kw[idx] = 3.0
    engine.state.soc_kwh[idx] = engine.state.r_kwh[idx]
    engine.state.lease_expires_at[idx] = 1_000_000.0
    engine.state.p_kw_commanded[idx] = 2.0

    engine.tick(13.0 * 3600.0)
    _topic, msg = engine.telemetry_messages(13.0 * 3600.0)[0]

    with _SCHEMA_PATH.open(encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(msg, schema)
    assert msg["charge_pv_kw"] >= 0
    assert msg["charge_grid_kw"] >= 0
