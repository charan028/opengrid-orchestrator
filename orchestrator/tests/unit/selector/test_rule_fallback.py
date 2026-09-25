"""F2 rule-based fallback (02a S3.7): firm first, then AS, then market; also the KPI-22 baseline."""

from __future__ import annotations

from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.types import ScenarioPrice
from opengrid.selector.validate import validate_plan
from unit.selector.factories import (
    binary_candidate,
    committed,
    continuous_candidate,
    make_bank,
    simple_inputs,
    zero_price_scenario,
)


def test_f2_serves_committed_before_any_candidate():
    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 6.0}, ("B1",))
    rich_candidate = binary_candidate("c1", 10.0, 1000.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (locked,), (rich_candidate,), n_intervals=1)

    plan = rule_fallback_f2(inputs)

    assert plan.plan_mode == "RULE_FALLBACK"
    assert plan.bank_interval_allocation["o-commit", "B1", 0] == 6.0
    assert plan.selected_x["c1"] is False  # only 4kW left, binary needs all 10kW

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations


def test_f2_orders_firm_before_as_before_market_at_equal_value():
    """Given equal $/MWh, F2 must grant the FIRM candidate before the MARKET one when capacity is tight."""
    bank = make_bank("B1", 5.0, range(1))
    scenario = zero_price_scenario(range(1))
    firm = binary_candidate("firm-1", 5.0, 50.0, (0,), ("B1",), category="FIRM")
    market = binary_candidate("market-1", 5.0, 50.0, (0,), ("B1",), category="MARKET")
    inputs = simple_inputs((bank,), (scenario,), (), (market, firm), n_intervals=1)

    plan = rule_fallback_f2(inputs)

    assert plan.selected_x["firm-1"] is True
    assert plan.selected_q.get("market-1", 0.0) == 0.0 or plan.selected_x.get("market-1") is False


def test_f2_never_schedules_headroom_at_a_negative_price():
    bank = make_bank("B1", 10.0, range(1))
    negative_scenario = ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh={0: -50.0})
    inputs = simple_inputs((bank,), (negative_scenario,), (), (), n_intervals=1)

    plan = rule_fallback_f2(inputs)

    assert plan.headroom_schedule["B1", 0, "P50"] == 0.0


def test_f2_respects_continuous_product_rule_rounding():
    bank = make_bank("B1", 3.0, range(1))
    scenario = zero_price_scenario(range(1))
    c = continuous_candidate("c1", 10.0, 20.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c,), n_intervals=1)

    plan = rule_fallback_f2(inputs)

    assert plan.selected_q["c1"] == 3.0  # capped at available capacity, not the full 10kW request
    ok, violations = validate_plan(inputs, plan)
    assert ok, violations
