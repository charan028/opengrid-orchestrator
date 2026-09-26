"""Independent validator (02a S3.8): re-derives constraints from raw numbers, catching a manufactured
bad plan even when it never touched `model.py`/`solve.py` (defense-in-depth)."""

from __future__ import annotations

from dataclasses import replace

from opengrid.selector.types import ExtractedPlan
from opengrid.selector.validate import validate_plan
from unit.selector.factories import binary_candidate, committed, make_bank, simple_inputs, zero_price_scenario


def _base_inputs():
    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 6.0}, ("B1",))
    c1 = binary_candidate("c1", 4.0, 10.0, (0,), ("B1",))
    return simple_inputs((bank,), (scenario,), (locked,), (c1,), n_intervals=1)


def _plan(**overrides) -> ExtractedPlan:
    base = {
        "solver_status": "OPTIMAL",
        "plan_mode": "L-ID",
        "objective_value": 0.0,
        "solver_gap": 0.0,
        "solver_time_ms": 1,
        "selected_x": {"c1": True},
        "selected_q": {},
        "bank_interval_allocation": {("o-commit", "B1", 0): 6.0, ("c1", "B1", 0): 4.0},
        "committed_profile": {"o-commit": {0: 6.0}},
        "headroom_schedule": {("B1", 0, "P50"): 0.0},
        "bank_capacity_duals": {},
    }
    base.update(overrides)
    return ExtractedPlan(**base)


def test_valid_plan_passes():
    ok, violations = validate_plan(_base_inputs(), _plan())
    assert ok, violations


def test_k13_violation_is_caught():
    bad = _plan(bank_interval_allocation={("o-commit", "B1", 0): 5.0, ("c1", "B1", 0): 4.0})
    ok, violations = validate_plan(_base_inputs(), bad)
    assert not ok
    assert any("K13" in v for v in violations)


def test_k2_one_buyer_violation_is_caught():
    bad = _plan(
        bank_interval_allocation={("o-commit", "B1", 0): 6.0, ("c1", "B1", 0): 4.0},
        headroom_schedule={("B1", 0, "P50"): 1.0},  # 6 + 4 + 1 = 11 > 10kW capacity
    )
    ok, violations = validate_plan(_base_inputs(), bad)
    assert not ok
    assert any("K2" in v for v in violations)


def test_soc_recurrence_tolerates_mip_residuals_but_not_real_errors():
    """HiGHS accepts MIP rows with residuals up to 1e-6 each and the C1 recurrence accumulates them, so a
    SoC a few 1e-5 kWh off the re-derived balance is a valid plan (the flaky property test), while a real
    energy error (10 Wh) is still caught."""
    bank = replace(
        make_bank("B1", 10.0, range(2)), capacity_kwh=10.0, initial_soc_kwh=5.0, self_discharge_kwh_per_h=0.0
    )
    inputs = simple_inputs((bank,), (zero_price_scenario(range(2)),), (), (), n_intervals=2)
    base = {
        "selected_x": {},
        "bank_interval_allocation": {},
        "committed_profile": {},
        "headroom_schedule": {("B1", 0, "P50"): 4.0, ("B1", 1, "P50"): 0.0},  # 1 kWh AC out in interval 0
    }
    expected_end = 5.0 - 0.25 / bank.eta_d * 4.0

    ok, violations = validate_plan(
        inputs, _plan(**base, soc_by_bank_interval_scenario={("B1", 1, "P50"): expected_end + 3e-5})
    )
    assert ok, violations
    ok, violations = validate_plan(
        inputs, _plan(**base, soc_by_bank_interval_scenario={("B1", 1, "P50"): expected_end + 1e-2})
    )
    assert not ok
    assert any("C1" in v for v in violations)


def test_product_rule_mismatch_is_caught():
    """Claiming c1 (BINARY, 4kW) is selected but only delivering 2kW is not representable."""
    bad = _plan(bank_interval_allocation={("o-commit", "B1", 0): 6.0, ("c1", "B1", 0): 2.0})
    ok, violations = validate_plan(_base_inputs(), bad)
    assert not ok
    assert any("product-rule" in v for v in violations)
