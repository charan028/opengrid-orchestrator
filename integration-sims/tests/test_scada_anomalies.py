"""Tests for every SCADA_* anomaly type: applies while active, reverts on
duration elapse. Uses ogsim.scada.runtime.ScadaEngine with a small config."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ogsim.common.config import load_scada_config
from ogsim.common.scenario import WIRE_TYPE_TO_CATALOGUE_ID
from ogsim.common.schemas import validate
from ogsim.scada.runtime import ScadaEngine

_CATALOGUE_ID_TO_WIRE_TYPE = {v: k for k, v in WIRE_TYPE_TO_CATALOGUE_ID.items()}


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
        "target": {"kind": "bank", "ref": target},
        "type": _CATALOGUE_ID_TO_WIRE_TYPE[anomaly_type],
        "params": params,
        "start": datetime.fromtimestamp(start, tz=UTC).isoformat(),
        "duration_s": duration,
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


# ---------------------------------------------------------------------------
# utility_instruction lift (blocker fix: ending the anomaly never lifted the BLOCK)
# ---------------------------------------------------------------------------


def test_utility_instruction_natural_expiry_publishes_a_lift(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 0.0, 10.0)
    _, instructions_start = engine.tick(1.0)
    block_msg = dict(instructions_start)[f"scada/instruction/{bank_id}"]
    assert block_msg["kind"] == "BLOCK"
    assert block_msg["expires_at"] is None
    validate("scada_utility_instruction", block_msg)

    # Duration (10s) elapses -- tick()'s expiry sweep must revert AND publish a lift.
    _, instructions_after = engine.tick(11.0)
    lift_msg = dict(instructions_after)[f"scada/instruction/{bank_id}"]
    assert lift_msg["expires_at"] is not None
    assert lift_msg["expires_at"] <= lift_msg["issued_at"]  # already expired on arrival
    validate("scada_utility_instruction", lift_msg)

    # No lingering BLOCK: fleet's own read of the instruction stream must see it as inactive.
    assert engine.anomalies.modifiers[bank_id].pending_instruction is None


def test_utility_instruction_manual_cancel_lifts_with_no_lingering_block(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    # A long-running (effectively indefinite) BLOCK, cancelled well before it would naturally
    # expire -- exercises the manual-cancel path, not natural duration elapse.
    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 0.0, 3600.0)
    _, instructions_start = engine.tick(1.0)
    assert dict(instructions_start)[f"scada/instruction/{bank_id}"]["kind"] == "BLOCK"

    # ogsim.control.injector.Injector.cancel's wire shape: same id/type/target/params, start
    # UNCHANGED (the original injection's start), duration_s=0 ("no cancel verb in the wire
    # schema" -- see injector.py's own comment).
    cancel_raw = {
        "id": "anom-utility_instruction",
        "target": {"kind": "bank", "ref": bank_id},
        "type": _CATALOGUE_ID_TO_WIRE_TYPE["utility_instruction"],
        "params": {"mode": "block"},
        "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
        "duration_s": 0,
    }
    assert engine.handle_scenario_cmd(cancel_raw) is True

    # The anomaly is gone from the active registry immediately -- no lingering active entry
    # waiting for a future tick to clean it up.
    assert "anom-utility_instruction" not in engine.anomalies._active

    # And the lift is queued immediately (available on the very next tick, whenever it runs),
    # not deferred to a later expiry sweep.
    _, instructions_after_cancel = engine.tick(2.0)
    lift_msg = dict(instructions_after_cancel)[f"scada/instruction/{bank_id}"]
    assert lift_msg["expires_at"] is not None
    assert lift_msg["expires_at"] <= lift_msg["issued_at"]
    validate("scada_utility_instruction", lift_msg)
    assert engine.anomalies.modifiers[bank_id].pending_instruction is None


def test_utility_instruction_manual_cancel_never_re_issues_the_block(engine: ScadaEngine) -> None:
    """The specific risk the lead flagged: does cancelling briefly re-publish the BLOCK before
    lifting it? `start()` must skip `_apply()` entirely for an already-expired (duration<=0)
    command, so only ONE message -- the lift -- is ever queued, never BLOCK-then-lift."""
    bank_id = engine.bank_ids[0]
    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 0.0, 3600.0)
    engine.tick(1.0)  # consumes/publishes the initial BLOCK

    cancel_raw = {
        "id": "anom-utility_instruction",
        "target": {"kind": "bank", "ref": bank_id},
        "type": _CATALOGUE_ID_TO_WIRE_TYPE["utility_instruction"],
        "params": {"mode": "block"},
        "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
        "duration_s": 0,
    }
    engine.handle_scenario_cmd(cancel_raw)

    # Immediately after the cancel is processed (before any further tick), the pending
    # instruction slot must hold the LIFT, never a re-issued BLOCK.
    pending = engine.anomalies.modifiers[bank_id].pending_instruction
    assert pending is not None
    assert pending["lift"] is True

    # And the only instruction message the next tick actually publishes for this bank is
    # that one lift -- there is no separate BLOCK message anywhere in the batch.
    _, instructions = engine.tick(2.0)
    bank_instructions = [msg for suffix, msg in instructions if suffix == f"scada/instruction/{bank_id}"]
    assert len(bank_instructions) == 1
    assert bank_instructions[0]["expires_at"] is not None


def test_utility_instruction_estop_mode_lift_preserves_kind(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "utility_instruction", bank_id, {"mode": "estop"}, 0.0, 5.0)
    engine.tick(1.0)
    _, instructions_after = engine.tick(6.0)
    lift_msg = dict(instructions_after)[f"scada/instruction/{bank_id}"]
    assert lift_msg["kind"] == "ESTOP"
    assert lift_msg["limit_kw"] is None
    assert lift_msg["expires_at"] is not None
    validate("scada_utility_instruction", lift_msg)


def test_time_skew_offsets_timestamp_and_reverts(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "time_skew", bank_id, {"skew_s": 300.0}, 0.0, 10.0)
    engine.tick(1.0)
    assert engine.anomalies.modifiers[bank_id].time_skew_s == 300.0
    engine.tick(11.0)
    assert engine.anomalies.modifiers[bank_id].time_skew_s == 0.0


def test_auto_limit_rule_fires_on_sustained_overload(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    # 700% (8x baseline feeder load) comfortably clears the 600 kVA feeder-segment rating
    # (bank_kva_rating_default, confirmed by Base 2026-09-25) regardless of the diurnal/per-bank
    # multiplier applied to the ~200 kW synthetic background load.
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 100.0)
    engine.tick(1.0)
    _, instructions = engine.tick(2.0)
    assert dict(instructions)[f"scada/instruction/{bank_id}"]["kind"] == "LIMIT"
