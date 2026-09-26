"""Tests for ogsim.scada.instructions -- rule-based auto LIMIT on overload, and its lift (bug fix,
2026-09-26, R3: the auto-issued LIMIT never expired, leaving a bank_overload demo capped at 90% of
rating forever)."""

from __future__ import annotations

from ogsim.scada.instructions import OverloadRule, lift_instruction, limit_instruction


def test_rule_does_not_fire_below_threshold_samples() -> None:
    rule = OverloadRule(threshold_samples=3)
    assert rule.observe("bank-000", 80.0, 75.0, now=0.0) is False
    assert rule.observe("bank-000", 80.0, 75.0, now=2.0) is False


def test_rule_fires_on_the_nth_consecutive_overload_sample() -> None:
    rule = OverloadRule(threshold_samples=3)
    assert rule.observe("bank-000", 80.0, 75.0, now=0.0) is False
    assert rule.observe("bank-000", 80.0, 75.0, now=2.0) is False
    assert rule.observe("bank-000", 80.0, 75.0, now=4.0) is True


def test_rule_resets_counter_on_a_non_overload_sample() -> None:
    rule = OverloadRule(threshold_samples=3)
    rule.observe("bank-000", 80.0, 75.0, now=0.0)
    rule.observe("bank-000", 80.0, 75.0, now=2.0)
    rule.observe("bank-000", 60.0, 75.0, now=4.0)  # back under rating
    assert rule.observe("bank-000", 80.0, 75.0, now=6.0) is False


def test_rule_tracks_banks_independently() -> None:
    rule = OverloadRule(threshold_samples=2)
    rule.observe("bank-000", 80.0, 75.0, now=0.0)
    assert rule.observe("bank-001", 80.0, 75.0, now=0.0) is False
    assert rule.observe("bank-000", 80.0, 75.0, now=2.0) is True


def test_rule_does_not_refire_every_tick_while_still_overloaded() -> None:
    rule = OverloadRule(threshold_samples=2)
    rule.observe("bank-000", 80.0, 75.0, now=0.0)
    assert rule.observe("bank-000", 80.0, 75.0, now=2.0) is True
    assert rule.observe("bank-000", 80.0, 75.0, now=4.0) is False  # still overloaded, still locked


def test_limit_instruction_shape() -> None:
    msg = limit_instruction("id-1", "bank-000", 67.5, "2026-09-26T18:00:00.000Z")
    assert msg["kind"] == "LIMIT"
    assert msg["bank_id"] == "bank-000"
    assert msg["limit_kw"] == 67.5
    assert msg["issued_by"] == "SCADA_AUTO_RULE"
    assert msg["expires_at"] is None


# ---- lift: bug fix, 2026-09-26, R3 ----------------------------------------------------------------


def _fire(rule: OverloadRule, bank_id: str = "bank-000", now: float = 0.0) -> None:
    """Drives `rule` to the firing sample for `bank_id` at time `now` (threshold_samples=2 assumed)."""
    rule.observe(bank_id, 80.0, 75.0, now=now - 2.0)
    fired = rule.observe(bank_id, 80.0, 75.0, now=now)
    assert fired is True


def test_check_lift_is_false_when_no_limit_is_active() -> None:
    rule = OverloadRule(threshold_samples=2, clear_samples=2)
    assert rule.check_lift("bank-000", 60.0, 75.0, now=100.0) is False


def test_check_lift_after_clear_samples_consecutive_readings_under_rating() -> None:
    rule = OverloadRule(threshold_samples=2, clear_samples=2, max_duration_s=10_000.0)
    _fire(rule, now=0.0)
    assert rule.check_lift("bank-000", 60.0, 75.0, now=2.0) is False  # 1st clear reading
    assert rule.check_lift("bank-000", 60.0, 75.0, now=4.0) is True  # 2nd -> lift


def test_check_lift_clear_streak_resets_on_a_still_overloaded_reading() -> None:
    rule = OverloadRule(threshold_samples=2, clear_samples=2, max_duration_s=10_000.0)
    _fire(rule, now=0.0)
    assert rule.check_lift("bank-000", 60.0, 75.0, now=2.0) is False  # 1st clear reading
    assert rule.check_lift("bank-000", 80.0, 75.0, now=4.0) is False  # still overloaded -> resets
    assert rule.check_lift("bank-000", 60.0, 75.0, now=6.0) is False  # 1st clear reading (again)
    assert rule.check_lift("bank-000", 60.0, 75.0, now=8.0) is True  # 2nd -> lift


def test_check_lift_after_max_duration_even_while_still_overloaded() -> None:
    """The bounded worst case: a LIMIT must lift after max_duration_s regardless of whether the
    overload ever actually clears (e.g. a stuck anomaly)."""
    rule = OverloadRule(threshold_samples=2, clear_samples=100, max_duration_s=900.0)
    _fire(rule, now=0.0)
    assert rule.check_lift("bank-000", 80.0, 75.0, now=500.0) is False  # still within max_duration_s
    assert rule.check_lift("bank-000", 80.0, 75.0, now=900.0) is True  # max_duration_s elapsed


def test_check_lift_resets_state_so_observe_can_fire_again() -> None:
    rule = OverloadRule(threshold_samples=2, clear_samples=1, max_duration_s=10_000.0)
    _fire(rule, now=0.0)
    assert rule.observe("bank-000", 80.0, 75.0, now=2.0) is False  # locked while active
    assert rule.check_lift("bank-000", 60.0, 75.0, now=4.0) is True  # lifted

    # A fresh overload can now fire a new LIMIT.
    assert rule.observe("bank-000", 80.0, 75.0, now=6.0) is False
    assert rule.observe("bank-000", 80.0, 75.0, now=8.0) is True


def test_check_lift_tracks_banks_independently() -> None:
    rule = OverloadRule(threshold_samples=2, clear_samples=1, max_duration_s=10_000.0)
    _fire(rule, "bank-000", now=0.0)
    _fire(rule, "bank-001", now=0.0)
    assert rule.check_lift("bank-000", 60.0, 75.0, now=2.0) is True
    # bank-001's own LIMIT is unaffected by bank-000's lift.
    assert rule.check_lift("bank-001", 80.0, 75.0, now=2.0) is False


def test_lift_instruction_shape_reuses_limit_kind_with_an_elapsed_expiry() -> None:
    """The wire schema has no separate lift kind: a lift is the SAME kind=LIMIT (limit_kw stays
    required/non-null), just with expires_at set to its own issued_at -- already elapsed on arrival."""
    msg = lift_instruction("id-2", "bank-000", 67.5, "2026-09-26T18:00:00.000Z")
    assert msg["kind"] == "LIMIT"
    assert msg["bank_id"] == "bank-000"
    assert msg["limit_kw"] == 67.5
    assert msg["issued_by"] == "SCADA_AUTO_RULE"
    assert msg["expires_at"] == msg["issued_at"] == "2026-09-26T18:00:00.000Z"
