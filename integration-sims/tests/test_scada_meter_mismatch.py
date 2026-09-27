"""The SCADA sim's bank meter (REAL_POWER_KW) for delivery corroboration (D-38): consistent with hub
telemetry by default, and the `meter_mismatch` anomaly makes it disagree on demand."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ogsim.common.config import MqttSettings, SubstationAssetConfig, load_scada_config
from ogsim.control.catalogue import CATALOGUE
from ogsim.scada.runtime import ScadaEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")
BANK = "bank-sub-LZ_AEN-00"
SUBSTATION = SubstationAssetConfig(
    asset_id="sub-LZ_AEN-00", zone="LZ_AEN", rated_mw=20.0, duration_h=2.0, enabled=True
)


def _engine() -> ScadaEngine:
    config = replace(
        load_scada_config(),
        mqtt=MQTT,
        bank_count=2,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        history_tsv_path="/nonexistent/meter_history.tsv",
        substation_assets=(SUBSTATION,),
        mobile_units=(),
    )
    engine = ScadaEngine(config, seed=3)
    engine.ingest_telemetry("sub-LZ_AEN-00", BANK, -18_000.0)
    return engine


def _meter_kw(engine: ScadaEngine, now: float) -> float:
    signals, _ = engine.tick(now)
    return next(m["value"] for topic, m in signals if m["bank_id"] == BANK and m["signal"] == "REAL_POWER_KW")


def test_the_substation_meter_reads_the_batteries_real_power() -> None:
    assert _meter_kw(_engine(), 1000.0) == pytest.approx(-18_000.0)


def test_a_meter_mismatch_scales_the_meter_only_and_reverts() -> None:
    engine = _engine()
    engine.handle_scenario_cmd(
        {
            "id": "00000000-0000-4000-8000-00000000d038",
            "target": {"kind": "bank", "ref": BANK},
            "type": "SCADA_METER_MISMATCH",
            "params": {"battery_scale": 0.5, "offset_kw": 100.0},
            "start": "1970-01-01T00:16:40Z",
            "duration_s": 60,
        }
    )

    assert _meter_kw(engine, 1010.0) == pytest.approx(-8_900.0)
    assert _meter_kw(engine, 1100.0) == pytest.approx(-18_000.0)  # the anomaly ended


def test_meter_mismatch_is_in_the_control_catalogue() -> None:
    entry = next(a for a in CATALOGUE if a.id == "meter_mismatch")
    assert entry.owner == "scada" and entry.wire_type == "SCADA_METER_MISMATCH"
