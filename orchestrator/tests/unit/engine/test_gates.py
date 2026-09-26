"""Selector-gate isolation inside the og-engine tick (K7/K13): regression for the live 2026-09-26 finding
that a `ReservationError` escaping `selector.run_gate` aborted the whole tick, so the allocator cycle
and guardian hand-off for obligations ALREADY committed were skipped on every failing gate."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opengrid.engine import GateTrigger
from opengrid.engine.gates import ALR_SELECTOR_GATE_FAILED, run_due_gates
from opengrid.ledger import ReservationError

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


class _Trace:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str, dict]] = []

    async def append(self, stream_id, decision_type, event_class, payload):
        self.appended.append((stream_id, decision_type, event_class, payload))


async def test_a_failing_gate_is_traced_and_alerted_and_the_next_gate_still_runs() -> None:
    failing, healthy = uuid4(), uuid4()
    ran: list[object] = []
    alerts: list = []
    trace = _Trace()

    async def _intake(gate_kind, contract_scope, *, now):
        return None

    async def _gate(gate_kind, contract_scope):
        ran.append(contract_scope)
        if contract_scope == failing:
            raise ReservationError("R-COMMIT-LOCK-INFEASIBLE")

    async def _alert(finding):
        alerts.append(finding)

    failed = await run_due_gates(
        [GateTrigger("ADMISSION", failing), GateTrigger("ADMISSION", healthy)],
        now=NOW,
        run_intake=_intake,
        run_gate=_gate,
        trace=trace,
        raise_alert=_alert,
    )

    assert failed == 1
    assert ran == [failing, healthy]
    (_stream, decision_type, event_class, payload) = trace.appended[0]
    assert (decision_type, event_class) == ("ALERT", "GATE_FAILED")
    assert payload["reason_code"] == "R-COMMIT-LOCK-INFEASIBLE"
    assert [a.rule for a in alerts] == [ALR_SELECTOR_GATE_FAILED]


async def test_an_intake_failure_does_not_stop_the_gate() -> None:
    ran: list[object] = []

    async def _intake(gate_kind, contract_scope, *, now):
        raise RuntimeError("feed down")

    async def _gate(gate_kind, contract_scope):
        ran.append(gate_kind)

    async def _alert(finding):
        raise AssertionError("no gate failed, no alert expected")

    failed = await run_due_gates(
        [GateTrigger("SCHEDULED_15MIN")],
        now=NOW,
        run_intake=_intake,
        run_gate=_gate,
        trace=_Trace(),
        raise_alert=_alert,
    )

    assert failed == 0
    assert ran == ["SCHEDULED_15MIN"]


async def test_a_renomination_gate_exercises_the_contracts_due_points() -> None:
    """02a S1.7: nothing exercised a due re-nomination point, so the RENOMINATION gate re-ran every tick
    and the R-RENOM-GATE self-loop never happened."""
    contract = uuid4()
    plan_id = uuid4()
    exercised: list[tuple[object, object]] = []

    async def _intake(gate_kind, contract_scope, *, now):
        return None

    async def _gate(gate_kind, contract_scope):
        return type("Plan", (), {"plan_id": plan_id})()

    async def _on_renomination(contract_id, gate_plan_id):
        exercised.append((contract_id, gate_plan_id))

    async def _alert(finding):
        raise AssertionError("unexpected alert")

    await run_due_gates(
        [GateTrigger("RENOMINATION", contract), GateTrigger("SCHEDULED_15MIN")],
        now=NOW,
        run_intake=_intake,
        run_gate=_gate,
        trace=_Trace(),
        raise_alert=_alert,
        on_renomination=_on_renomination,
    )

    assert exercised == [(contract, plan_id)]


async def test_due_points_are_reselected_when_delivering_else_confirmed(monkeypatch) -> None:
    import types

    import opengrid.contracts as contracts
    from opengrid.contracts import IllegalTransitionError
    from opengrid.engine import exercise_due_renomination_points

    contract, other = uuid4(), uuid4()
    delivering = types.SimpleNamespace(
        renomination_point_id=uuid4(), contract_id=contract, obligation_id=uuid4()
    )
    committed = types.SimpleNamespace(
        renomination_point_id=uuid4(), contract_id=contract, obligation_id=uuid4()
    )
    foreign = types.SimpleNamespace(renomination_point_id=uuid4(), contract_id=other, obligation_id=None)
    calls: list[tuple[object, str]] = []

    async def _due(*, as_of=None):
        return [delivering, committed, foreign]

    async def _exercise(point_id, outcome, *, plan_id=None):
        if point_id == committed.renomination_point_id and outcome == "RESELECTED":
            raise IllegalTransitionError(
                from_state="COMMITTED", to_state="DELIVERING", reason_code="R-RENOM-GATE"
            )
        calls.append((point_id, outcome))

    monkeypatch.setattr(contracts, "due_renomination_points", _due)
    monkeypatch.setattr(contracts, "exercise_renomination_point", _exercise)

    outcomes = await exercise_due_renomination_points(contract, uuid4(), NOW)

    assert outcomes == ["RESELECTED", "CONFIRMED"]
    assert calls == [
        (delivering.renomination_point_id, "RESELECTED"),
        (committed.renomination_point_id, "CONFIRMED"),
    ]
