"""ogsim.fleet.runtime: discharge-flow-limit telemetry fields (09-optimizer-dispatcher-update.md
S1.9/G11 -- "Hub telemetry has no meter net power, PV, cell temperature, BMS limits or peak budget").
Additive to `interfaces/mqtt/telemetry.schema.json`; validated here against the real schema file so a
field-name typo or a schema `additionalProperties: false` mismatch fails the unit suite, not a live
run."""

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


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


def _telemetry_schema() -> dict:
    with _SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_telemetry_messages_include_discharge_flow_fields_and_validate(engine: FleetEngine) -> None:
    engine.tick(now=0.0)
    messages = engine.telemetry_messages(now=0.0)
    # Not a hardcoded 4: the shipped fleet.yaml's substation asset (D-29(b)) adds one more hub on top
    # of this fixture's hub_count=4 override.
    assert len(messages) == len(engine.state.hub_ids)

    schema = _telemetry_schema()
    for _topic, msg in messages:
        jsonschema.validate(msg, schema)
        assert msg["home_load_kw"] >= 0
        assert msg["pv_kw"] >= 0
        assert msg["p_dis_max_kw"] >= 0
        assert msg["p_ch_max_kw"] >= 0
        assert msg["peak_power_budget_kws"] == pytest.approx(0.0)  # F5 inactive by default (OQ-10)
        # F2: M_i = L_net_i + p_i, using the applied (clipped) battery power.
        assert msg["meter_kw"] == pytest.approx((msg["home_load_kw"] - msg["pv_kw"]) + msg["p_kw"], abs=1e-6)


def test_p_dis_max_kw_is_full_rating_at_mid_soc_and_mild_temperature(engine: FleetEngine) -> None:
    """Every hub in the fixture starts SoC-uniform(0.4, 0.9) of a 39.2 kWh single-unit hub, i.e. well
    inside the SoC sweet spot (>= 0.30); mild synthetic ambient temperature keeps it inside the
    plateau too, so p_dis_max_kw should equal the hub's full rated power (11.0 kW)."""
    engine.tick(now=0.0)
    for p_dis_max, p_kw_limit, cell_temp in zip(
        engine.state.p_dis_max_kw, engine.state.p_kw_limit, engine.state.cell_temp_c, strict=True
    ):
        if 15.0 <= cell_temp <= 35.0:
            assert p_dis_max == pytest.approx(p_kw_limit)


def test_cell_temp_c_is_populated_after_a_tick(engine: FleetEngine) -> None:
    assert all(t == 0.0 for t in engine.state.cell_temp_c)  # placeholder before any tick
    engine.tick(now=3600.0 * 15)  # 15:00, ambient peak hour
    assert all(t != 0.0 for t in engine.state.cell_temp_c)
