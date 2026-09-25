"""TS-05: lexicographic tier allocation, S3 (02a S5.1/S5.2). K13's tier-priority squeeze behavior."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.allocator import reasons
from opengrid.allocator.lexicographic import allocate_tiers
from opengrid.allocator.models import ObligationCall


def _call(oid: str, tier: str, kw: float, service: str = "DIST_DEFERRAL") -> ObligationCall:
    return ObligationCall(
        obligation_id=oid,
        bank_id="b1",
        service_type=service,
        tier=tier,
        committed_kw=kw,
        eligible_hub_ids=(),
    )


def test_ts_05_10_higher_tier_served_before_lower_when_capacity_short() -> None:
    calls = (_call("o-t1", "T1", 60.0), _call("o-t3", "T3", 60.0, service="ERCOT_ENERGY"))
    result = allocate_tiers("b1", calls, capability_kw=80.0)
    assert result.granted_kw["o-t1"] == 60.0
    assert result.granted_kw["o-t3"] == 20.0
    assert len(result.shortfalls) == 1
    assert result.shortfalls[0].obligation_id == "o-t3"
    assert result.shortfalls[0].shortfall_kw == 40.0
    assert result.shortfalls[0].reason_code == reasons.R_COMMIT_LOCK_INFEASIBLE


def test_ts_05_11_higher_tier_shortfall_is_reported_against_itself_not_lower_tier() -> None:
    """A T1 shortfall is reported against T1 itself -- T1's unmet demand is never satisfied by
    clawing capacity back from T3's already-granted allocation (K13: no reallocation across
    obligations)."""
    calls = (_call("o-t1", "T1", 100.0), _call("o-t3", "T3", 20.0, service="ERCOT_ENERGY"))
    result = allocate_tiers("b1", calls, capability_kw=80.0)
    # T1 has absolute priority: it takes all 80 kW of capability, T3 gets nothing this cycle.
    assert result.granted_kw["o-t1"] == 80.0
    assert result.granted_kw["o-t3"] == 0.0
    assert {s.obligation_id for s in result.shortfalls} == {"o-t1", "o-t3"}
    t1_shortfall = next(s for s in result.shortfalls if s.obligation_id == "o-t1")
    assert t1_shortfall.shortfall_kw == 20.0


def test_ts_05_12_deterministic_tie_break_within_tier() -> None:
    calls = (_call("o-b", "T1", 60.0), _call("o-a", "T1", 60.0))
    result = allocate_tiers("b1", calls, capability_kw=60.0)
    # o-a sorts before o-b, so it is served first and gets the full 60; o-b gets 0.
    assert result.granted_kw["o-a"] == 60.0
    assert result.granted_kw["o-b"] == 0.0


def test_ts_05_13_custom_shortfall_reason_for_l2() -> None:
    calls = (_call("o1", "T1", 100.0),)
    result = allocate_tiers(
        "b1", calls, capability_kw=10.0, shortfall_reason=reasons.R_COMMIT_LOCK_OVERRIDE_L2
    )
    assert result.shortfalls[0].reason_code == reasons.R_COMMIT_LOCK_OVERRIDE_L2


def test_ts_05_14_no_calls_leaves_full_headroom() -> None:
    result = allocate_tiers("b1", (), capability_kw=100.0)
    assert result.granted_kw == {}
    assert result.remaining_capability_kw == 100.0


@given(
    t1=st.floats(min_value=0, max_value=500, allow_nan=False),
    t2=st.floats(min_value=0, max_value=500, allow_nan=False),
    t3=st.floats(min_value=0, max_value=500, allow_nan=False),
    cap=st.floats(min_value=0, max_value=1000, allow_nan=False),
)
def test_ts_05_15_property_never_exceeds_capability_and_respects_priority(
    t1: float, t2: float, t3: float, cap: float
) -> None:
    calls = (
        _call("o-t1", "T1", t1),
        _call("o-t2", "T2", t2, service="ERCOT_AS"),
        _call("o-t3", "T3", t3, service="ERCOT_ENERGY"),
    )
    result = allocate_tiers("b1", calls, capability_kw=cap)
    total_granted = sum(result.granted_kw.values())
    assert total_granted <= cap + 1e-6  # K4: never exceeds bank capability
    for kw in result.granted_kw.values():
        assert kw >= -1e-9

    # Lexicographic priority == a greedy fill in tier order: the cumulative grant through any
    # prefix of tiers equals min(cumulative demand through that prefix, capability) -- a lower
    # tier's shortfall never lets it "borrow" from what a higher tier already claimed.
    cum_demand_1 = t1
    cum_demand_2 = t1 + t2
    cum_granted_1 = result.granted_kw["o-t1"]
    cum_granted_2 = cum_granted_1 + result.granted_kw["o-t2"]
    assert cum_granted_1 == pytest.approx(min(cum_demand_1, cap), abs=1e-6)
    assert cum_granted_2 == pytest.approx(min(cum_demand_2, cap), abs=1e-6)
