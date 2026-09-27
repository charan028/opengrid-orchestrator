"""Tests for every SCADA_* anomaly type: applies while active, reverts on
duration elapse. Uses ogsim.scada.runtime.ScadaEngine with a small config."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ogsim.common.config import load_scada_config
from ogsim.common.scenario import WIRE_TYPE_TO_CATALOGUE_ID
from ogsim.common.schemas import validate
from ogsim.scada.anomalies import normalize_bank_ref
from ogsim.scada.runtime import ScadaEngine

_BANK_IDS = [f"bank-{i:03d}" for i in range(3)]


@pytest.mark.parametrize(
    "ref",
    ["bank-001", "BANK_001", "bank_001", "BANK-001", "bank001", "BANK001", " BANK_001 "],
)
def test_normalize_bank_ref_accepts_common_placeholder_shapes(ref: str) -> None:
    assert normalize_bank_ref(ref, _BANK_IDS) == "bank-001"


def test_normalize_bank_ref_zero_pads_a_short_digit_run() -> None:
    assert normalize_bank_ref("BANK_1", _BANK_IDS) == "bank-001"


def test_normalize_bank_ref_rejects_an_out_of_range_index() -> None:
    assert normalize_bank_ref("BANK_999", _BANK_IDS) is None


def test_normalize_bank_ref_rejects_a_non_bank_ref() -> None:
    assert normalize_bank_ref("hub-00001", _BANK_IDS) is None
    assert normalize_bank_ref("LZ_NORTH", _BANK_IDS) is None


_CATALOGUE_ID_TO_WIRE_TYPE = {v: k for k, v in WIRE_TYPE_TO_CATALOGUE_ID.items()}


def _kva_message(signals, bank_id: str) -> dict:
    """The APPARENT_POWER_KVA message for `bank_id` (bug fix, 2026-09-26, R3: `tick()` now also
    publishes a REAL_POWER_KW message on the SAME topic `scada/<bank_id>`, so a plain `dict(signals)`
    collapses to whichever of the two was appended last -- tests that care about the kVA reading
    specifically must filter by `signal`, not rely on topic-keyed dict collapse)."""
    return next(
        m for topic, m in signals if topic == f"scada/{bank_id}" and m["signal"] == "APPARENT_POWER_KVA"
    )


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


def test_bank_overload_via_a_placeholder_ref_raises_kva_above_rating_for_its_duration(
    engine: ScadaEngine,
) -> None:
    """Live bug fix, 2026-09-26 (FLEET-SIM, e2e A11 regression: "a bank_overload injection from
    ogsim.control never changes the SCADA sim's readings"). Root cause: shipped scenario files name
    banks in an uppercase/underscore placeholder format (e.g. `BANK_07`) that never exact-matched this
    sim's real `bank-NNN` ids, so `_resolve_targets` silently resolved to zero banks. This targets
    `bank-000` as `BANK_00` -- exactly that mismatched shape -- and proves the reported kVA actually
    rises above the bank's rating for the anomaly's duration, then drops back after it ends."""
    bank_id = engine.bank_ids[0]  # "bank-000"
    placeholder_ref = "BANK_00"
    rating = engine.kva_rating[bank_id]
    # Big enough that background load (~200 kW default, well under a 600 kVA rating) times the
    # multiplier clears the rating with room to spare, so this isn't sensitive to the noise draw.
    _inject(engine, "bank_overload", placeholder_ref, {"kva_over_rating_pct": 300.0}, 0.0, 10.0)

    signals, _ = engine.tick(1.0)
    value = dict(signals)[f"scada/{bank_id}"]["value"]
    assert value > rating

    engine.tick(11.0)  # past the 10 s duration
    signals_after, _ = engine.tick(12.0)
    value_after = dict(signals_after)[f"scada/{bank_id}"]["value"]
    assert value_after <= rating
    assert engine.anomalies.modifiers[bank_id].overload_pct == 0.0


def test_bank_overload_sets_kva_relative_to_the_rating_not_the_reading(engine: ScadaEngine) -> None:
    """#43 B1: demo-02's `kva_over_rating_pct: 25` on a 600 kVA bank must read 750 kVA (125% of
    rating, over the orchestrator's 120% ALR-SCADA-OVERLOAD critical threshold) while active --
    independent of the ~200 kW background load -- and fall back under the rating after it ends. Before
    the fix it multiplied the actual reading, so +25% landed near 250 kVA and never alerted."""
    bank_id = engine.bank_ids[0]
    rating = engine.kva_rating[bank_id]
    assert rating == 600.0  # shipped scada.yaml feeder-segment rating
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 25.0}, 0.0, 10.0)

    for t in (1.0, 2.0, 3.0):
        signals, _ = engine.tick(t)
        assert _kva_message(signals, bank_id)["value"] == pytest.approx(750.0, abs=0.01)
        assert _kva_message(signals, bank_id)["value"] / rating > 1.20

    engine.tick(11.0)  # past the 10 s duration
    signals_after, _ = engine.tick(12.0)
    assert _kva_message(signals_after, bank_id)["value"] < rating
    assert engine.anomalies.modifiers[bank_id].overload_active is False


def test_overload_auto_lift_never_overwrites_a_scenario_block_on_the_same_bank(engine: ScadaEngine) -> None:
    """R3.4 fix: the SCADA overload auto-LIFT must only ever end an auto-LIMIT it itself issued, never
    a scenario-driven BLOCK/ESTOP on the same bank. Previously, once this rule's own auto-LIMIT had
    been issued for a bank, a LATER scenario BLOCK on that same bank did not clear the rule's own
    bookkeeping -- once the overload cleared, `check_lift` would still fire and publish an expired
    LIMIT that (per the orchestrator's "latest instruction per bank wins" semantics,
    opengrid.fleet.ingest_utility_instruction) would silently end the scenario's BLOCK."""
    bank_id = engine.bank_ids[0]
    assert engine.overload_rule.threshold_samples == 2  # fixture's overload_consecutive_samples

    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 10.0)
    engine.tick(1.0)
    _, issue = engine.tick(2.0)  # 2nd consecutive overloaded sample -> auto LIMIT
    assert dict(issue)[f"scada/instruction/{bank_id}"]["issued_by"] == "SCADA_AUTO_RULE"
    assert bank_id in engine.overload_rule._active_since

    # A scenario now BLOCKs this same bank directly.
    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 3.0, 100.0)
    _, block_tick = engine.tick(3.0)
    block_msg = dict(block_tick)[f"scada/instruction/{bank_id}"]
    assert block_msg["kind"] == "BLOCK"
    # The rule must have forgotten its own auto-LIMIT bookkeeping for this bank -- otherwise a later
    # clear streak would still fire `check_lift` and clobber the scenario's BLOCK.
    assert bank_id not in engine.overload_rule._active_since

    # The bank_overload anomaly ends (duration elapsed at t=10) and the overload clears; give the rule
    # several further ticks -- it must publish NOTHING for this bank (no bookkeeping left to lift).
    for t in (11.0, 12.0, 13.0, 14.0):
        _, tick_result = engine.tick(t)
        assert f"scada/instruction/{bank_id}" not in dict(tick_result)


def test_r7_auto_limit_never_replaces_an_active_scenario_block_and_requires_a_fresh_trigger_after(
    engine: ScadaEngine,
) -> None:
    """R7 review fix, 2026-09-26: two layers.

    (1) Absolute: while a scenario BLOCK/ESTOP is ACTIVE on a bank, the auto rule must never touch
    that bank's instruction slot at all -- even if the overload condition persists uninterrupted, well
    past `threshold_samples` more overloaded readings (`ScadaAnomalyManager.has_active_utility_
    instruction` gates `observe`/`check_lift` out entirely for the scenario's whole duration).

    (2) After the scenario instruction ends: a `cancel()` (or the scenario's own natural end) must NOT
    let the rule immediately re-arm while the SAME overload condition is still ongoing -- only a fresh
    rising edge (clear, then overloaded again) is a genuine new trigger (`OverloadRule._held`)."""
    bank_id = engine.bank_ids[0]
    assert engine.overload_rule.threshold_samples == 2

    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 1000.0)  # persists
    engine.tick(1.0)
    _, issue = engine.tick(2.0)
    assert dict(issue)[f"scada/instruction/{bank_id}"]["issued_by"] == "SCADA_AUTO_RULE"

    _inject(engine, "utility_instruction", bank_id, {"mode": "block"}, 3.0, 5.0)  # BLOCK, ends at t=8
    _, block_tick = engine.tick(3.0)
    assert dict(block_tick)[f"scada/instruction/{bank_id}"]["kind"] == "BLOCK"

    # (1) While the BLOCK is still active, even though the overload persists uninterrupted and well
    # past threshold_samples, the rule must publish NOTHING for this bank -- the absolute gate.
    for t in (4.0, 5.0, 6.0, 7.0):
        _, tick_result = engine.tick(t)
        assert f"scada/instruction/{bank_id}" not in dict(tick_result)

    # The BLOCK naturally ends at t=8 -- its own lift publishes (SCENARIO_ANOMALY, not the auto rule).
    _, end_tick = engine.tick(8.0)
    end_msg = dict(end_tick)[f"scada/instruction/{bank_id}"]
    assert end_msg["issued_by"] == "SCENARIO_ANOMALY"

    # (2) The overload STILL persists uninterrupted after the BLOCK ends -- the rule must NOT
    # immediately re-arm here either; several more overloaded ticks past threshold_samples must still
    # publish nothing (a fresh trigger, not a continuation, is required).
    for t in (9.0, 10.0, 11.0, 12.0):
        _, tick_result = engine.tick(t)
        assert f"scada/instruction/{bank_id}" not in dict(tick_result)

    # Only once the condition actually clears and then recurs is that a genuine fresh trigger.
    _inject(engine, "bank_overload", bank_id, {}, 13.0, 0.0)  # explicit cancel/clear
    engine.tick(13.0)
    assert engine.anomalies.modifiers[bank_id].overload_pct == 0.0

    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 14.0, 10.0)
    engine.tick(14.0)
    _, fresh_issue = engine.tick(15.0)
    fresh_msg = dict(fresh_issue)[f"scada/instruction/{bank_id}"]
    assert fresh_msg["issued_by"] == "SCADA_AUTO_RULE"
    assert fresh_msg["kind"] == "LIMIT"


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
    assert _kva_message(signals, bank_id)["value"] == pytest.approx(1.0 / 0.98, abs=1e-3)
    assert _kva_message(signals, bank_id)["quality"] == "out_of_range"
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


# ---- REAL_POWER_KW signal: live bug fix, 2026-09-26, R3 (taken off Frank's #39) -------------------
# The guardian now fails closed on unknown flow direction (G-30 vetoes discharge increases in
# regulated territories, e.g. Austin, without a signed real-power series).


def test_every_tick_publishes_both_kva_and_kw_signals_per_bank(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    signals, _instructions = engine.tick(0.0)
    bank_signals = [m for topic, m in signals if topic == f"scada/{bank_id}"]
    assert {m["signal"] for m in bank_signals} == {"APPARENT_POWER_KVA", "REAL_POWER_KW"}


def test_real_power_kw_signal_shape_and_units(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    signals, _instructions = engine.tick(0.0)
    kw_msg = next(m for topic, m in signals if topic == f"scada/{bank_id}" and m["signal"] == "REAL_POWER_KW")
    assert kw_msg["bank_id"] == bank_id
    assert kw_msg["unit"] == "kW"
    assert kw_msg["quality"] == "good"
    validate("scada_bank_signal", kw_msg)


def test_real_power_kw_signal_uses_the_same_ts_and_quality_as_the_kva_signal(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    _inject(engine, "bad_quality_flag", bank_id, {}, 0.0, 10.0)
    signals, _instructions = engine.tick(1.0)
    kva_msg = _kva_message(signals, bank_id)
    kw_msg = next(m for topic, m in signals if topic == f"scada/{bank_id}" and m["signal"] == "REAL_POWER_KW")
    assert kw_msg["ts"] == kva_msg["ts"]
    assert kw_msg["quality"] == kva_msg["quality"] == "out_of_range"


def test_real_power_kw_sign_is_positive_for_import_negative_for_export(engine: ScadaEngine) -> None:
    """+ = the bank imports from the feeder (net charging/consuming), - = export (net discharging)."""
    bank_id = engine.bank_ids[0]
    engine.ingest_telemetry("some-hub", bank_id, -50.0)  # discharging -> exporting
    signals, _instructions = engine.tick(0.0)
    kw_msg = next(m for topic, m in signals if topic == f"scada/{bank_id}" and m["signal"] == "REAL_POWER_KW")
    kva_msg = _kva_message(signals, bank_id)
    # The KVA reading is always non-negative (apparent power magnitude); the KW reading carries the
    # actual signed flow direction the guardian needs.
    assert kva_msg["value"] >= 0.0
    assert kw_msg["value"] < kva_msg["value"]  # net export pulls the real-power reading down


def test_real_power_kw_covers_the_substation_bank_too(engine: ScadaEngine) -> None:
    """The same tick() loop already covers every bank in self.bank_ids -- no separate branch needed
    for a substation bank once one is configured (see test_scada_substation_asset.py for a dedicated
    substation-asset fixture; here we just confirm the general loop has no home-bank-only special
    case for REAL_POWER_KW)."""
    signals, _instructions = engine.tick(0.0)
    kw_bank_ids = {m["bank_id"] for topic, m in signals if m["signal"] == "REAL_POWER_KW"}
    assert kw_bank_ids == set(engine.bank_ids)


def test_auto_limit_rule_fires_on_sustained_overload(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    # 700% (8x baseline feeder load) comfortably clears the 600 kVA feeder-segment rating
    # (bank_kva_rating_default, confirmed by Base 2026-09-25) regardless of the diurnal/per-bank
    # multiplier applied to the ~200 kW synthetic background load.
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 100.0)
    engine.tick(1.0)
    _, instructions = engine.tick(2.0)
    assert dict(instructions)[f"scada/instruction/{bank_id}"]["kind"] == "LIMIT"
    assert dict(instructions)[f"scada/instruction/{bank_id}"]["expires_at"] is None


# ---- auto LIMIT lift: bug fix, 2026-09-26, R3 (the auto LIMIT never expired before this) ----------


def test_auto_limit_lifts_after_clear_samples_once_the_overload_ends(engine: ScadaEngine) -> None:
    bank_id = engine.bank_ids[0]
    assert engine.overload_rule.clear_samples == 3  # shipped scada.yaml default
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 4.0)
    engine.tick(1.0)
    _, issue_instructions = engine.tick(2.0)  # 2nd consecutive overloaded sample -> LIMIT
    assert dict(issue_instructions)[f"scada/instruction/{bank_id}"]["kind"] == "LIMIT"

    # The anomaly's own duration (4s) ends the overload; readings 4/5/6 are the 3 consecutive
    # clear_samples back under rating.
    _, clear_1 = engine.tick(4.0)
    assert f"scada/instruction/{bank_id}" not in dict(clear_1)
    _, clear_2 = engine.tick(5.0)
    assert f"scada/instruction/{bank_id}" not in dict(clear_2)
    _, clear_3 = engine.tick(6.0)
    lift_msg = dict(clear_3)[f"scada/instruction/{bank_id}"]
    assert lift_msg["kind"] == "LIMIT"  # no separate lift kind on the wire
    assert lift_msg["expires_at"] is not None
    assert lift_msg["expires_at"] <= lift_msg["issued_at"]  # already expired on arrival
    validate("scada_utility_instruction", lift_msg)


def test_auto_limit_clear_streak_resets_on_a_still_overloaded_reading(engine: ScadaEngine) -> None:
    """A single reading back over rating during the clear streak must not count toward
    clear_samples -- the lift needs `clear_samples` CONSECUTIVE clear readings."""
    bank_id = engine.bank_ids[0]
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 4.0)
    engine.tick(1.0)
    engine.tick(2.0)  # LIMIT issued

    _, clear_1 = engine.tick(4.0)
    assert f"scada/instruction/{bank_id}" not in dict(clear_1)
    # Re-inject a fresh overload sample before the clear streak completes.
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 5.0, 1.0)
    _, still_overloaded = engine.tick(5.0)
    assert f"scada/instruction/{bank_id}" not in dict(still_overloaded)  # no re-issue (still locked)

    # Clear streak must restart from zero, not continue from 1.
    _, clear_1_again = engine.tick(7.0)
    assert f"scada/instruction/{bank_id}" not in dict(clear_1_again)
    _, clear_2_again = engine.tick(8.0)
    assert f"scada/instruction/{bank_id}" not in dict(clear_2_again)
    _, clear_3_again = engine.tick(9.0)
    assert dict(clear_3_again)[f"scada/instruction/{bank_id}"]["expires_at"] is not None


def test_auto_limit_lifts_after_max_duration_even_if_overload_never_clears() -> None:
    """Bounded worst case: a stuck bank_overload anomaly must not cap the bank forever -- the LIMIT
    lifts after overload_limit_max_duration_s regardless."""
    config = replace(
        load_scada_config(),
        bank_count=2,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        history_tsv_path="/nonexistent/no-history.tsv",
        overload_consecutive_samples=2,
        overload_clear_samples=1000,  # effectively unreachable -- only max_duration_s can lift
        overload_limit_max_duration_s=10.0,
    )
    engine = ScadaEngine(config, seed=7)
    bank_id = engine.bank_ids[0]
    _inject(engine, "bank_overload", bank_id, {"kva_over_rating_pct": 700.0}, 0.0, 3600.0)
    engine.tick(1.0)
    _, issue_instructions = engine.tick(2.0)
    assert dict(issue_instructions)[f"scada/instruction/{bank_id}"]["kind"] == "LIMIT"

    _, before_max = engine.tick(8.0)  # still overloaded, within max_duration_s of the issue at t=2
    assert f"scada/instruction/{bank_id}" not in dict(before_max)

    _, at_max = engine.tick(12.0)  # 10s elapsed since issue (t=2) -> forced lift
    lift_msg = dict(at_max)[f"scada/instruction/{bank_id}"]
    assert lift_msg["kind"] == "LIMIT"
    assert lift_msg["expires_at"] is not None
