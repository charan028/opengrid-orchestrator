"""Property tests (Hypothesis) for the selector invariants (BUILD.md): committed obligations never
lose y-hat between solves (K13), SoC/one-buyer bounds hold (K1/K2), no headroom sold twice, and product
rules are respected -- checked against both the LP/MILP path and the F2 rule-fallback path, since both
must satisfy the same invariants (F2 is also the KPI-22 baseline).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import (
    binary_candidate,
    committed,
    continuous_candidate,
    make_bank,
    semi_continuous_candidate,
    simple_inputs,
    zero_price_scenario,
)

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=10.0)


def _bounded_kw(min_value: float, max_value: float) -> st.SearchStrategy[float]:
    """A kW/price magnitude strategy that excludes the pathologically-tiny-but-nonzero floats real
    telemetry/config never produces (e.g. 1e-45 kW) -- those only trip HiGHS's small-coefficient guard,
    they are not a realistic bank capacity or commitment."""
    return st.one_of(
        st.just(0.0),
        st.floats(
            min_value=min_value,
            max_value=max_value,
            allow_nan=False,
            allow_infinity=False,
            allow_subnormal=False,
        ),
    )


_bank_capacity = _bounded_kw(0.01, 50.0)
_committed_kw = _bounded_kw(0.01, 20.0)
_candidate_kw = st.floats(
    min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False, allow_subnormal=False
)
_value = st.floats(
    min_value=1.0, max_value=500.0, allow_nan=False, allow_infinity=False, allow_subnormal=False
)


def _build_random_instance(capacity, committed_kw, candidate_kind, candidate_kw, candidate_value):
    bank = make_bank("B1", capacity, range(1))
    scenario = zero_price_scenario(range(1))
    committed_kw = min(committed_kw, capacity)  # a committed obligation is only ever frozen within capability
    locked = (committed("o-commit", {0: committed_kw}, ("B1",)),) if committed_kw > 1e-9 else ()

    if candidate_kind == "BINARY":
        candidate = binary_candidate("c1", candidate_kw, candidate_value, (0,), ("B1",))
    elif candidate_kind == "CONTINUOUS":
        candidate = continuous_candidate("c1", candidate_kw, candidate_value, (0,), ("B1",))
    else:
        candidate = semi_continuous_candidate(
            "c1", candidate_kw, candidate_kw / 4, candidate_kw / 8, candidate_value, (0,), ("B1",)
        )

    inputs = simple_inputs((bank,), (scenario,), locked, (candidate,), n_intervals=1)
    return inputs


@given(
    capacity=_bank_capacity,
    committed_kw=_committed_kw,
    candidate_kind=st.sampled_from(["BINARY", "CONTINUOUS", "SEMI_CONTINUOUS"]),
    candidate_kw=_candidate_kw,
    candidate_value=_value,
)
@settings(max_examples=60, deadline=None)
def test_lp_path_never_violates_k13_k2_or_product_rules(
    capacity, committed_kw, candidate_kind, candidate_kw, candidate_value
):
    inputs = _build_random_instance(capacity, committed_kw, candidate_kind, candidate_kw, candidate_value)
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    if outcome.status == "OPTIMAL":
        ok, violations = validate_plan(inputs, plan)
        assert ok, violations


@given(
    capacity=_bank_capacity,
    committed_kw=_committed_kw,
    candidate_kind=st.sampled_from(["BINARY", "CONTINUOUS", "SEMI_CONTINUOUS"]),
    candidate_kw=_candidate_kw,
    candidate_value=_value,
)
@settings(max_examples=100, deadline=None)
def test_rule_fallback_never_violates_k2_or_product_rules(
    capacity, committed_kw, candidate_kind, candidate_kw, candidate_value
):
    """F2 must never oversell a bank (K2) or select an unrepresentable product quantity, for any
    randomized instance -- it is the degraded path AND the KPI-22 baseline, so it must be unconditionally
    safe even when it cannot fully serve a commitment (that shortfall is a K13 exception, not a K2 one)."""
    inputs = _build_random_instance(capacity, committed_kw, candidate_kind, candidate_kw, candidate_value)
    plan = rule_fallback_f2(inputs)

    _ok, violations = validate_plan(inputs, plan)
    k13_only = [v for v in violations if not v.startswith("K13")]
    assert not k13_only, violations


@given(committed_kw=_committed_kw, capacity=_bank_capacity)
@settings(max_examples=60, deadline=None)
def test_k13_committed_never_reduced_across_successive_solves(committed_kw, capacity):
    """K13: solving the SAME committed obligation again (a later gate) with a *richer* competing
    candidate must never reduce its delivered total below the frozen commitment."""
    capacity = max(capacity, committed_kw)  # commitments are only ever frozen within capability
    bank = make_bank("B1", capacity, range(1))
    scenario = zero_price_scenario(range(1))
    locked = (committed("o-commit", {0: committed_kw}, ("B1",)),) if committed_kw > 1e-9 else ()
    rich = binary_candidate("c1", capacity, 100_000.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), locked, (rich,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID") if outcome.status == "OPTIMAL" else rule_fallback_f2(inputs)

    delivered = sum(kw for (oid, _b, _t), kw in plan.bank_interval_allocation.items() if oid == "o-commit")
    assert delivered >= committed_kw - 1e-6
