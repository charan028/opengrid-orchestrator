"""Q10 (r3.4.5): every lift the SCADA sim publishes names the instruction it ends
(`lifts_instruction_id`), so a utility-initiated stop can be released (after the lift, the operators'
two-person release). A lift can also name an instruction issued before a sim restart (the id is in the
orchestrator's stop reason)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ogsim.common.config import load_scada_config
from ogsim.common.scenario import WIRE_TYPE_TO_CATALOGUE_ID
from ogsim.common.schemas import validate
from ogsim.scada.runtime import ScadaEngine

_WIRE = {v: k for k, v in WIRE_TYPE_TO_CATALOGUE_ID.items()}
OLD_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def engine(tmp_path) -> ScadaEngine:
    config = replace(
        load_scada_config(),
        bank_count=2,
        zones=("LZ_NORTH", "LZ_SOUTH"),
        history_tsv_path=str(tmp_path / "no-history.tsv"),
        overload_consecutive_samples=2,
    )
    return ScadaEngine(config, seed=7)


def _inject(
    engine: ScadaEngine, anomaly: str, bank: str, params: dict, start: float, duration: float
) -> None:
    raw = {
        "id": f"anom-{anomaly}-{start}",
        "target": {"kind": "bank", "ref": bank},
        "type": _WIRE[anomaly],
        "params": params,
        "start": datetime.fromtimestamp(start, tz=UTC).isoformat(),
        "duration_s": duration,
    }
    assert engine.handle_scenario_cmd(raw) is True


def _instruction(tick_result, bank: str) -> dict:
    message = dict(tick_result)[f"scada/instruction/{bank}"]
    validate("scada_utility_instruction", message)  # the shared interfaces/ schema
    return message


def test_a_scenario_block_ending_names_the_block_in_its_lift(engine: ScadaEngine) -> None:
    bank = engine.bank_ids[0]
    _inject(engine, "utility_instruction", bank, {"mode": "block"}, 1.0, 5.0)
    block = _instruction(engine.tick(1.0)[1], bank)
    assert block["kind"] == "BLOCK" and block["expires_at"] is None and block["lifts_instruction_id"] is None
    lift = _instruction(engine.tick(6.0)[1], bank)
    assert lift["kind"] == "BLOCK" and lift["expires_at"] == lift["issued_at"]
    assert lift["lifts_instruction_id"] == block["instruction_id"]
    assert lift["instruction_id"] != block["instruction_id"]


def test_a_cancel_with_an_explicit_id_lifts_an_instruction_issued_before_a_restart(
    engine: ScadaEngine,
) -> None:
    bank = engine.bank_ids[1]
    _inject(engine, "utility_instruction", bank, {"mode": "block", "lifts_instruction_id": OLD_ID}, 1.0, 0.0)
    lift = _instruction(engine.tick(1.0)[1], bank)
    assert lift["kind"] == "BLOCK" and lift["expires_at"] == lift["issued_at"]
    assert lift["lifts_instruction_id"] == OLD_ID


def test_the_auto_rule_lift_names_its_own_limit(engine: ScadaEngine) -> None:
    bank = engine.bank_ids[0]
    _inject(engine, "bank_overload", bank, {"kva_over_rating_pct": 700.0}, 0.0, 3.0)
    engine.tick(1.0)
    limit = _instruction(engine.tick(2.0)[1], bank)
    assert limit["issued_by"] == "SCADA_AUTO_RULE" and limit["kind"] == "LIMIT"
    lift = None
    for t in (4.0, 5.0, 6.0, 7.0, 8.0):
        result = dict(engine.tick(t)[1])
        if f"scada/instruction/{bank}" in result:
            lift = _instruction(list(result.items()), bank)
            break
    assert lift is not None and lift["lifts_instruction_id"] == limit["instruction_id"]
