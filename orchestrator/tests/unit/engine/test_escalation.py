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


def test_the_real_allocators_shortfall_reasons_escalate() -> None:
    """Live 2026-09-26 05:17: an L2 BLOCK on the bank serving a DELIVERING test obligation stopped its
    grants but never escalated -- the allocator reports the K13 reason itself (R-COMMIT-LOCK-OVERRIDE-L2 /
    R-COMMIT-LOCK-INFEASIBLE), and the escalator only mapped the R-SHORTFALL-* codes. Driven here through
    the real allocator cycle so the two can't drift apart again."""
    from datetime import UTC, datetime

    from opengrid.allocator.cycle import cycle
    from opengrid.allocator.models import (
        BankSnapshot,
        FleetState,
        HubSnapshot,
        Instruction,
        LedgerView,
        ObligationCall,
        Schedule,
    )

    hub = HubSnapshot(
        hub_id="h1",
        bank_id="b1",
        free_discharge_kw=50.0,
        health="OK",
        soc_kwh=1_000.0,
        reserve_kwh=0.0,
        e_kwh=1_000.0,
    )
    bank = BankSnapshot(bank_id="b1", capability_kw=100.0, kva_rating=100.0)
    fleet = FleetState(hubs=(hub,), banks=(bank,))
    call = ObligationCall(
        obligation_id=OBL,
        bank_id="b1",
        service_type="PARTNER_CAPACITY",
        tier="T3",
        committed_kw=50.0,
        eligible_hub_ids=("h1",),
    )
    blocked = cycle(
        datetime(2026, 9, 26, 10, 17, tzinfo=UTC),
        fleet,
        LedgerView(calls=(call,)),
        Schedule(),
        {},
        (Instruction(scope="BANK", scope_ref="b1", kind="BLOCK"),),
    )

    signals = merge_signals([(s.obligation_id, s.reason_code) for s in blocked.shortfalls], [])

    assert signals == {OBL: {"R-COMMIT-LOCK-OVERRIDE-L2"}}
    assert ShortfallEscalator(sustain_cycles=1).observe(signals) == [(OBL, "R-COMMIT-LOCK-OVERRIDE-L2")]
    assert merge_signals([(OBL, "R-COMMIT-LOCK-INFEASIBLE")], []) == {OBL: {"R-COMMIT-LOCK-INFEASIBLE"}}


def test_l0_and_l1_shortfalls_escalate_with_tier_precedence() -> None:
    signals = merge_signals(
        [
            (OBL, "R-COMMIT-LOCK-OVERRIDE-L1"),
            (OBL, "R-COMMIT-LOCK-OVERRIDE-L2"),
            (OTHER, "R-COMMIT-LOCK-OVERRIDE-L0"),
        ],
        energy_infeasible=[OBL],
    )
    due = dict(ShortfallEscalator(sustain_cycles=1).observe(signals))
    assert due == {OBL: "R-COMMIT-LOCK-OVERRIDE-L1", OTHER: "R-COMMIT-LOCK-OVERRIDE-L0"}


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
