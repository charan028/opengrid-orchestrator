"""Tests for every SCADA_* anomaly type: applies while active, reverts on
duration elapse. Uses ogsim.scada.runtime.ScadaEngine with a small config."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ogsim.common.config import load_scada_config
from ogsim.scada.runtime import ScadaEngine


@pytest.fixture
def engine(tmp_path) -> ScadaEngine:
    missing_history = tmp_path / "no-history.tsv"
    config = replace(
        load_scada_config(),
        bank_count=2,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        history_tsv_path=str(missing_history),
        overload_consecutive_samples=2,
    )
    return ScadaEngine(config, seed=7)


def _inject(
    engine: ScadaEngine, anomaly_type: str, target: str, params: dict, start: float, duration: float | None
):
    raw = {
        "id": f"anom-{anomaly_type}",
        "target": target,
        "type": anomaly_type,
        "params": params,
        "start": start,
        "duration": duration,
    }
    assert engine.handle_scenario_cmd(raw) is True


def test_bank_overload_inflates_reading_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 50.0}, 0.0, 10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers[bank_id].overload_pct == 50.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].overload_pct == 0.0


def test_load_spike_multiplies_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "load_spike", bank_id, {"multiplier": 3.0}, 0.0, 10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers[bank_id].load_multiplier == 3.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].load_multiplier == 1.0


def test_frozen_value_holds_then_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "frozen_value", bank_id, {}, 0.0, 10.0)
    signals_1, _ = engine.tick(1.0)
    signals_2, _ = engine.tick(2.0)
    value_1 = dict(signals_1)[f"scada/{bank_id}"]["value"]
    value_2 = dict(signals_2)[f"scada/{bank_id}"]["value"]
    assert value_1 == value_2
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].frozen is False


def test_bad_quality_flag_overrides_quality_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "bad_quality_flag", bank_id, {}, 0.0, 10.0)
    signals, _ = engine.tick(1.0)
    assert dict(signals)[f"scada/{bank_id}"]["quality"] == "out_of_range"
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].quality_override is None


def test_stale_no_update_suppresses_publish_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "stale_no_update", bank_id, {}, 0.0, 10.0)
    signals, _ = engine.tick(1.0)
    assert f"scada/{bank_id}" not in dict(signals)
    engine.tick(11.0)
    signals_after, _ = engine.tick(12.0)
    assert f"scada/{bank_id}" in dict(signals_after)


def test_out_of_range_value_overrides_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "out_of_range_value", bank_id, {"value": -1.0}, 0.0, 10.0)
    signals, _ = engine.tick(1.0)
    assert dict(signals)[f"scada/{bank_id}"]["value"] == pytest.approx(1.0 / 0.98, abs=1e-3)
    assert dict(signals)[f"scada/{bank_id}"]["quality"] == "out_of_range"
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].out_of_range_value is None


def test_oscillation_varies_value_over_time_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "oscillation", bank_id, {"amplitude_pct": 50.0, "period_s": 4.0}, 0.0, 20.0)
    signals_a, _ = engine.tick(1.0)
    signals_b, _ = engine.tick(2.0)
    value_a = dict(signals_a)[f"scada/{bank_id}"]["value"]
    value_b = dict(signals_b)[f"scada/{bank_id}"]["value"]
    assert value_a != value_b
    engine.tick(21.0)
    assert engine.anomalies.modifiers[bank_id].oscillation is None


def test_phase_imbalance_scales_load_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "phase_imbalance", bank_id, {"imbalance_pct": 30.0}, 0.0, 10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers[bank_id].load_multiplier == pytest.approx(1.3)
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].load_multiplier == 1.0


def test_breaker_open_marks_comm_fail_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "breaker_open", bank_id, {}, 0.0, 10.0)
    signals, _ = engine.tick(1.0)
    assert dict(signals)[f"scada/{bank_id}"]["quality"] == "comm_fail"
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].breaker_open is False


def test_comms_loss_suppresses_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "comms_loss", bank_id, {}, 0.0, 10.0)
    signals, _ = engine.tick(1.0)
    assert f"scada/{bank_id}" not in dict(signals)
    engine.tick(11.0)
    signals_after, _ = engine.tick(12.0)
    assert f"scada/{bank_id}" in dict(signals_after)


def test_utility_instruction_publishes_and_is_one_shot(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 0.0, 10.0)
    _, instructions_1 = engine.tick(1.0)
    assert dict(instructions_1)[f"scada/instruction/{bank_id}"]["kind"] == "BLOCK"
    _, instructions_2 = engine.tick(2.0)
    assert f"scada/instruction/{bank_id}" not in dict(instructions_2)


def test_time_skew_offsets_timestamp_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "time_skew", bank_id, {"skew_s": 300.0}, 0.0, 10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers[bank_id].time_skew_s == 300.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].time_skew_s == 0.0


def test_auto_limit_rule_fires_on_sustained_overload(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 200.0}, 0.0, 100.0)
    engine.tick(1.0)
    _, instructions = engine.tick(2.0)
    assert dict(instructions)[f"scada/instruction/{bank_id}"]["kind"] == "LIMIT"
