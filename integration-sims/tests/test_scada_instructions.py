"""Tests for ogsim.scada.instructions -- rule-based auto LIMIT on overload."""

from __future__ import annotations

from ogsim.scada.instructions import OverloadRule, limit_instruction


def test_rule_does_not_fire_below_threshold_samples() -> None:
    rule = OverloadRule(threshold_samples=3)
    assert rule.observe("bank-000", 80.0, 75.0) is False
    assert rule.observe("bank-000", 80.0, 75.0) is False


def test_rule_fires_on_the_nth_consecutive_overload_sample() -> None:
    rule = OverloadRule(threshold_samples=3)
    assert rule.observe("bank-000", 80.0, 75.0) is False
    assert rule.observe("bank-000", 80.0, 75.0) is False
    assert rule.observe("bank-000", 80.0, 75.0) is True


def test_rule_resets_counter_on_a_non_overload_sample() -> None:
    rule = OverloadRule(threshold_samples=3)
    rule.observe("bank-000", 80.0, 75.0)
    rule.observe("bank-000", 80.0, 75.0)
    rule.observe("bank-000", 60.0, 75.0)  # back under rating
    assert rule.observe("bank-000", 80.0, 75.0) is False


def test_rule_tracks_banks_independently() -> None:
    rule = OverloadRule(threshold_samples=2)
    rule.observe("bank-000", 80.0, 75.0)
    assert rule.observe("bank-001", 80.0, 75.0) is False
    assert rule.observe("bank-000", 80.0, 75.0) is True


def test_rule_does_not_refire_every_tick_while_still_overloaded() -> None:
    rule = OverloadRule(threshold_samples=2)
    rule.observe("bank-000", 80.0, 75.0)
    assert rule.observe("bank-000", 80.0, 75.0) is True
    assert rule.observe("bank-000", 80.0, 75.0) is False


def test_limit_instruction_shape() -> None:
    msg = limit_instruction("id-1", "bank-000", 67.5, "2026-09-26T18:00:00.000Z")
    assert msg["kind"] == "LIMIT"
    assert msg["bank_id"] == "bank-000"
    assert msg["limit_kw"] == 67.5
    assert msg["issued_by"] == "SCADA_AUTO_RULE"
