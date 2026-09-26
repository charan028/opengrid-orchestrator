"""ES06-S04 / K7: > 5 % vetoed on a tick -> CONSERVATIVE; 3 consecutive -> a safe stop REQUESTED of a person
(alert + operator-action proposal), never engaged; recovery clears. Plus the per-batch veto count."""

from __future__ import annotations

from typing import Any

import pytest

from opengrid.guardian import main as guardian_main
from opengrid.guardian.escalation import (
    CONSERVATIVE_ALERT_RULE,
    STOP_REQUEST_ALERT_RULE,
    BatchOutcome,
    EscalationTracker,
    vetoed_command_count,
)

ZONES = {"bank-000": "LZ_NORTH", "bank-004": "LZ_NORTH", "bank-001": "LZ_SOUTH"}


def _tick(vetoed: int, commands: int = 100, bank: str = "bank-000") -> list[BatchOutcome]:
    return [BatchOutcome(bank_id=bank, commands=commands, vetoed_commands=vetoed)]


# --- which commands count as vetoed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "rules", "violated", "expected"),
    [
        ("PASS", [], [], 0),
        ("TIMEOUT", ["G-20"], [], 0),  # a hold, never a veto (K7)
        ("TIMEOUT", [], [], 0),
        ("VETOED", ["SAFE_STOP"], [], 0),  # refused because a stop is already engaged: not a content veto
        ("VETOED", ["G-19"], [], 3),
        ("VETOED", ["SAFE_STOP", "G-03"], [], 3),
        ("PARTLY_VETOED", ["G-01"], ["hub-b"], 1),
        ("PARTLY_VETOED", ["G-01", "G-04"], ["hub-a", "hub-c", None], 2),
    ],
)
def test_vetoed_command_count(outcome, rules, violated, expected):
    assert vetoed_command_count(outcome, rules, ["hub-a", "hub-b", "hub-c"], violated) == expected


# --- the per-scope counter ---------------------------------------------------------------------------------


def test_more_than_five_percent_vetoed_makes_bank_and_zone_conservative():
    tracker = EscalationTracker()
    transitions = tracker.observe_tick(_tick(6), ZONES)
    assert {(t.scope, t.kind) for t in transitions} == {
        (("BANK", "bank-000"), "ENTER_CONSERVATIVE"),
        (("ZONE", "LZ_NORTH"), "ENTER_CONSERVATIVE"),
    }
    assert tracker.posture(("BANK", "bank-000")) == "CONSERVATIVE"


def test_exactly_five_percent_is_not_conservative():
    tracker = EscalationTracker()
    assert tracker.observe_tick(_tick(5), ZONES) == []


def test_three_consecutive_conservative_ticks_request_a_stop_once():
    tracker = EscalationTracker()
    kinds = [[t.kind for t in tracker.observe_tick(_tick(50), {})] for _ in range(5)]
    assert kinds == [
        ["ENTER_CONSERVATIVE"],
        ["STAY_CONSERVATIVE"],
        ["REQUEST_SAFE_STOP"],
        ["STAY_CONSERVATIVE"],
        ["STAY_CONSERVATIVE"],
    ]


def test_recovery_clears_and_the_count_restarts():
    tracker = EscalationTracker()
    tracker.observe_tick(_tick(50), {})
    tracker.observe_tick(_tick(50), {})
    assert [t.kind for t in tracker.observe_tick(_tick(1), {})] == ["CLEAR"]
    assert tracker.posture(("BANK", "bank-000")) == "NORMAL"
    kinds = [tracker.observe_tick(_tick(50), {})[0].kind for _ in range(2)]
    assert kinds == ["ENTER_CONSERVATIVE", "STAY_CONSERVATIVE"]  # a fresh episode: no early stop request


def test_a_zone_is_judged_on_all_its_banks_together():
    tracker = EscalationTracker()
    outcomes = _tick(10, bank="bank-000") + _tick(0, bank="bank-004")  # 10 of 200 in LZ_NORTH = 5 %
    scopes = {t.scope for t in tracker.observe_tick(outcomes, ZONES)}
    assert ("BANK", "bank-000") in scopes and ("ZONE", "LZ_NORTH") not in scopes


def test_an_idle_conservative_scope_clears_after_the_idle_window():
    tracker = EscalationTracker(idle_clear_ticks=3)
    tracker.observe_tick(_tick(50), {})
    assert tracker.observe_tick([], {}) == [] and tracker.observe_tick([], {}) == []
    assert [t.kind for t in tracker.observe_tick([], {})] == ["CLEAR"]


# --- publishing: posture, alerts, and a REQUEST (never a stop) ------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def set_posture(self, kind, ref, *, posture, veto_ratio, consecutive, stop_requested):
        self.calls.append(("posture", kind, ref, posture, consecutive, stop_requested))

    async def propose_safe_stop(self, kind, ref, reason):
        self.calls.append(("propose", kind, ref))

    async def raise_alert(self, rule, severity, summary, condition_key, detail):
        self.calls.append(("raise", rule, severity, condition_key))

    async def clear_alert(self, rule, condition_key):
        self.calls.append(("clear", rule, condition_key))


async def _apply(tracker, recorder, outcomes):
    await guardian_main.apply_escalation(
        tracker=tracker, posture=recorder, alerts=recorder, outcomes=outcomes, zone_by_bank={}
    )


async def test_escalation_publishes_posture_alerts_and_only_a_request():
    tracker, rec = EscalationTracker(), _Recorder()
    for _ in range(3):
        await _apply(tracker, rec, _tick(50))

    assert ("raise", CONSERVATIVE_ALERT_RULE, "warning", "BANK:bank-000") in rec.calls
    assert ("raise", STOP_REQUEST_ALERT_RULE, "critical", "BANK:bank-000") in rec.calls
    assert rec.calls.count(("propose", "BANK", "bank-000")) == 1
    assert ("posture", "BANK", "bank-000", "CONSERVATIVE", 3, True) in rec.calls

    rec.calls.clear()
    await _apply(tracker, rec, _tick(0))
    assert rec.calls == [
        ("posture", "BANK", "bank-000", "NORMAL", 0, False),
        ("clear", CONSERVATIVE_ALERT_RULE, "BANK:bank-000"),
        ("clear", STOP_REQUEST_ALERT_RULE, "BANK:bank-000"),
    ]


def test_the_guardian_escalation_path_has_no_way_to_engage_a_stop():
    """K8/ES06-S04: the escalation module and its main-loop applier never import or call safestop."""
    import inspect

    import opengrid.guardian.escalation as escalation

    for source in (inspect.getsource(escalation), inspect.getsource(guardian_main.apply_escalation)):
        assert "safestop" not in source.lower().replace("safe_stop", "").replace("safe stop", "")
        assert "engage(" not in source
