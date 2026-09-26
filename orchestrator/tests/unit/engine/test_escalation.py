"""Mid-window shortfall escalation (02a S2.1): a sustained L2 / no-substitute / energy-infeasible signal
moves a DELIVERING obligation to SHORTFALL with the matching K13 reason code (lead review 2026-09-26:
SHORTFALL was only ever reached at window end)."""

from __future__ import annotations

import types

from opengrid.allocator.energy_sufficiency import EnergySufficiencyResult
from opengrid.contracts import IllegalTransitionError
from opengrid.engine import escalate_sustained_shortfalls
from opengrid.engine.escalation import ShortfallEscalator, merge_signals

OBL = "3b60e803-dabf-4df9-a284-1386c0426952"
OTHER = "0db3b37d-a61c-4c71-8b4d-64a7434935ea"


def test_a_signal_escalates_once_after_sustain_cycles_and_a_clean_cycle_resets() -> None:
    escalator = ShortfallEscalator(sustain_cycles=3)
    signal = merge_signals([(OBL, "R-SHORTFALL-NO-SUBSTITUTE")], [])

    assert escalator.observe(signal) == []
    assert escalator.observe({}) == []  # clean cycle resets the count
    assert escalator.observe(signal) == []
    assert escalator.observe(signal) == []
    assert escalator.observe(signal) == [(OBL, "R-COMMIT-LOCK-INFEASIBLE")]
    assert escalator.observe(signal) == []  # escalates only once


def test_l2_override_wins_over_infeasible_and_unmapped_reasons_are_ignored() -> None:
    signals = merge_signals(
        [(OBL, "R-SHORTFALL-L2-INSTRUCTION"), (OTHER, "R-GRANT-HEADROOM")], energy_infeasible=[OBL]
    )
    assert signals == {OBL: {"R-COMMIT-LOCK-OVERRIDE-L2", "R-COMMIT-LOCK-INFEASIBLE"}}
    assert ShortfallEscalator(sustain_cycles=1).observe(signals) == [(OBL, "R-COMMIT-LOCK-OVERRIDE-L2")]


def _energy(obligation_id: str, margin_kwh: float) -> EnergySufficiencyResult:
    return EnergySufficiencyResult(
        obligation_id=obligation_id,
        required_kwh=10.0,
        available_kwh=10.0 + margin_kwh,
        margin_kwh=margin_kwh,
        time_to_depletion_h=0.1,
        at_risk=margin_kwh < 0,
    )


async def test_engine_applies_the_edge_and_skips_an_obligation_not_yet_delivering() -> None:
    calls: list[tuple[str, str, str]] = []

    async def _transition(obligation_id, to_state, *, reason_code, payload=None):
        if str(obligation_id) == OTHER:
            raise IllegalTransitionError(from_state="COMMITTED", to_state=to_state, reason_code=reason_code)
        calls.append((str(obligation_id), to_state, reason_code))

    state = types.SimpleNamespace(
        ledger_gateway=types.SimpleNamespace(last_shortfalls=[]),
        escalator=ShortfallEscalator(sustain_cycles=1),
    )
    escalated = await escalate_sustained_shortfalls(
        state, [_energy(OBL, -5.0), _energy(OTHER, -1.0)], _transition
    )

    assert calls == [(OBL, "SHORTFALL", "R-COMMIT-LOCK-INFEASIBLE")]
    assert escalated == [(OBL, "R-COMMIT-LOCK-INFEASIBLE")]
