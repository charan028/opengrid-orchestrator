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


def test_a_genuinely_recovered_scope_clears_after_three_good_ticks_and_the_count_decays():
    tracker = EscalationTracker()
    scope = ("BANK", "bank-000")
    tracker.observe_tick(_tick(50), {})
    tracker.observe_tick(_tick(50), {})
    assert tracker.observe_tick(_tick(1), {}) == [] and tracker.observe_tick(_tick(0), {}) == []
    assert tracker.posture(scope) == "CONSERVATIVE"  # one or two good ticks are not yet recovery
    assert [t.kind for t in tracker.observe_tick(_tick(2), {})] == ["CLEAR"]
    assert tracker.posture(scope) == "NORMAL" and not tracker.stop_requested(scope)
    assert tracker.escalation_count(scope) == 1  # decayed by one, not reset
    tracker.observe_tick(_tick(0), {})
    assert tracker.escalation_count(scope) == 0  # every further good tick decays it
    kinds = [tracker.observe_tick(_tick(50), {})[0].kind for _ in range(2)]
    assert kinds == ["ENTER_CONSERVATIVE", "STAY_CONSERVATIVE"]  # a fresh episode: no early stop request


def test_a_flapping_veto_ratio_escalates_to_a_stop_request():
    """Review fix (K7): 6 %, 4 %, 6 %, ... used to alternate ENTER/CLEAR forever and never reach the request."""
    tracker = EscalationTracker()
    kinds = [[t.kind for t in tracker.observe_tick(_tick(v), {})] for v in (6, 4, 6, 4, 6)]
    assert kinds == [["ENTER_CONSERVATIVE"], [], ["STAY_CONSERVATIVE"], [], ["REQUEST_SAFE_STOP"]]
    assert tracker.posture(("BANK", "bank-000")) == "CONSERVATIVE"


def test_a_fault_on_every_other_tick_escalates_even_with_clean_ticks_between():
    tracker = EscalationTracker()
    kinds = [[t.kind for t in tracker.observe_tick(_tick(v), {})] for v in (6, 0, 6, 0, 6)]
    assert kinds[-1] == ["REQUEST_SAFE_STOP"] and ["CLEAR"] not in kinds


def test_a_tick_between_the_thresholds_breaks_the_good_streak():
    tracker = EscalationTracker()
    tracker.observe_tick(_tick(50), {})
    for vetoed in (0, 0, 4, 0, 0):  # 4 % is under the 5 % entry ratio but over the 2.5 % clear ratio
        assert tracker.observe_tick(_tick(vetoed), {}) == []
    assert [t.kind for t in tracker.observe_tick(_tick(0), {})] == ["CLEAR"]


def test_a_relapse_soon_after_clearing_resumes_close_to_a_stop_request():
    tracker = EscalationTracker()
    for vetoed in (50, 50, 0, 0, 0):  # escalation count 2, cleared -> decays to 1
        tracker.observe_tick(_tick(vetoed), {})
    kinds = [tracker.observe_tick(_tick(50), {})[0].kind for _ in range(2)]
    assert kinds == ["ENTER_CONSERVATIVE", "REQUEST_SAFE_STOP"]


def test_the_hysteresis_is_configurable():
    tracker = EscalationTracker(clear_after_good_ticks=1, clear_ratio_factor=1.0)
    tracker.observe_tick(_tick(50), {})
    assert [t.kind for t in tracker.observe_tick(_tick(5), {})] == ["CLEAR"]


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
    """Posture + alert ports; alerts behave like `PgAlertPort` (raise is a no-op while one is open)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.open: set[tuple[str, str]] = set()

    async def set_posture(self, kind, ref, *, posture, veto_ratio, consecutive, stop_requested):
        self.calls.append(("posture", kind, ref, posture, consecutive, stop_requested))

    async def propose_safe_stop(self, kind, ref, reason):
        self.calls.append(("propose", kind, ref))

    async def raise_alert(self, rule, severity, summary, condition_key, detail):
        self.calls.append(("raise", rule, severity, condition_key))
        self.open.add((rule, condition_key))

    async def clear_alert(self, rule, condition_key):
        self.calls.append(("clear", rule, condition_key))
        self.open.discard((rule, condition_key))

    async def open_condition_keys(self, rule):
        return sorted(key for r, key in self.open if r == rule)


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
    await _apply(tracker, rec, _tick(0))
    assert rec.calls == []  # not yet recovered: the posture and both alerts stay
    await _apply(tracker, rec, _tick(0))
    assert rec.calls == [
        ("posture", "BANK", "bank-000", "NORMAL", 0, False),
        ("clear", CONSERVATIVE_ALERT_RULE, "BANK:bank-000"),
        ("clear", STOP_REQUEST_ALERT_RULE, "BANK:bank-000"),
    ]


async def test_a_flapping_fault_raises_the_stop_request_alert():
    """Review fix (K7): the 6 %/4 %/6 % flap reaches ALR-SAFE-STOP-REQUESTED and one operator proposal."""
    tracker, rec = EscalationTracker(), _Recorder()
    for vetoed in (6, 4, 6, 4, 6):
        await _apply(tracker, rec, _tick(vetoed))

    assert ("raise", STOP_REQUEST_ALERT_RULE, "critical", "BANK:bank-000") in rec.calls
    assert rec.calls.count(("propose", "BANK", "bank-000")) == 1
    assert not any(call[0] == "clear" for call in rec.calls)


def test_the_hysteresis_settings_are_read_from_config():
    from opengrid.guardian.config import load_guardian_config
    from opengrid.platform.config import Config

    cfg = load_guardian_config(
        Config({"guardian": {"escalation_clear_after_good_ticks": 5, "escalation_clear_ratio_factor": 0.25}})
    )
    assert cfg.escalation_clear_after_good_ticks == 5 and cfg.escalation_clear_ratio_factor == 0.25
    defaults = load_guardian_config(Config({}))
    assert defaults.escalation_clear_after_good_ticks == 3 and defaults.escalation_clear_ratio_factor == 0.5


async def test_an_escalation_alert_left_open_for_a_normal_scope_is_cleared():
    """Base 2026-09-26: ALR-SCOPE-CONSERVATIVE stayed open after its scope went NORMAL (a missed clear, or a
    guardian restart losing the tracker). The next tick clears it and sets the posture back to NORMAL."""
    tracker, rec = EscalationTracker(), _Recorder()
    rec.open = {(CONSERVATIVE_ALERT_RULE, "BANK:bank-000"), (STOP_REQUEST_ALERT_RULE, "BANK:bank-000")}

    await _apply(tracker, rec, _tick(0))

    assert rec.open == set()
    assert ("posture", "BANK", "bank-000", "NORMAL", 0, False) in rec.calls


async def test_a_conservative_scope_keeps_its_alert_and_never_raises_a_duplicate():
    tracker, rec = EscalationTracker(), _Recorder()
    for vetoed in (50, 4, 50, 4):  # stays CONSERVATIVE (4 % is not a good tick), count 2: no stop request
        await _apply(tracker, rec, _tick(vetoed))

    assert rec.open == {(CONSERVATIVE_ALERT_RULE, "BANK:bank-000")}
    assert [c for c in rec.calls if c[0] == "raise" and c[1] == CONSERVATIVE_ALERT_RULE] == [
        ("raise", CONSERVATIVE_ALERT_RULE, "warning", "BANK:bank-000")
    ]
    assert not any(c[0] == "clear" for c in rec.calls)


async def test_a_failed_clear_of_one_alert_still_clears_the_other():
    tracker, rec = EscalationTracker(clear_after_good_ticks=1), _Recorder()
    await _apply(tracker, rec, _tick(50))
    rec.open.add((STOP_REQUEST_ALERT_RULE, "BANK:bank-000"))
    real_clear = rec.clear_alert

    async def flaky_clear(rule, condition_key):
        if rule == CONSERVATIVE_ALERT_RULE:
            raise RuntimeError("db hiccup")
        await real_clear(rule, condition_key)

    rec.clear_alert = flaky_clear  # type: ignore[method-assign]
    await _apply(tracker, rec, _tick(0))

    assert (STOP_REQUEST_ALERT_RULE, "BANK:bank-000") not in rec.open


def test_pg_alert_port_matches_alerts_by_stored_key_or_by_scope():
    """An alert without the stored `condition_key` (an older writer) is still found by its scope, so it is
    deduplicated and cleared instead of staying open forever."""
    from opengrid.guardian import repo

    assert "detail ->> 'condition_key'" in repo._OPEN_ALERTS_SQL
    assert "scope_kind || ':' || scope_ref" in repo._OPEN_ALERTS_SQL
    assert "scope_kind || ':' || scope_ref" in repo._OPEN_ALERT_KEYS_SQL


def test_the_guardian_escalation_path_has_no_way_to_engage_a_stop():
    """K8/ES06-S04: the escalation module and its main-loop applier never import or call safestop."""
    import inspect

    import opengrid.guardian.escalation as escalation

    for source in (inspect.getsource(escalation), inspect.getsource(guardian_main.apply_escalation)):
        assert "safestop" not in source.lower().replace("safe_stop", "").replace("safe stop", "")
        assert "engage(" not in source
