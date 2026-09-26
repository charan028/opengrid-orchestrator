"""TS-12a/12b/12c/13a/13b at the allocator level (07-delivery/06-service-profiles-and-power-quality.md
S5.2 steps 1-2, S5.5.3): the eligibility filter and diversity/phase-balance weighting ahead of
`water_fill`/`realize_obligation`."""

from __future__ import annotations

import cmath
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.allocator.models import HubSnapshot
from opengrid.allocator.pq_eligibility import (
    ALWAYS_EXCLUDED_ASSET_STATES,
    PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED,
    PQ_ELIGIBILITY_KVA_INFEASIBLE,
    PQ_ELIGIBILITY_OUTLIER_THD,
    PQ_ELIGIBILITY_OUTLIER_VOLTAGE_OFFSET,
    PQ_ELIGIBILITY_QUALITY_SCORE_BELOW_MINIMUM,
    PQ_ELIGIBILITY_RIDE_THROUGH_INSUFFICIENT,
    PQ_ELIGIBILITY_RIDE_THROUGH_UNKNOWN,
    EligibilityConfig,
    HubPqCandidate,
    apply_eligibility,
    evaluate_hub_eligibility,
    phase_balance_weights,
)

_DATA_CENTER_CONFIG = EligibilityConfig(
    pq_aware_selection=True,
    min_quality_score=0.8,
    required_ride_through_class="CATEGORY_III",
    eligible_asset_states=frozenset({"OK", "WATCH"}),
    excluded_asset_states=frozenset({"DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}),
)

_ARBITRAGE_CONFIG = EligibilityConfig(
    pq_aware_selection=False,
    min_quality_score=0.0,
    required_ride_through_class="CATEGORY_III",
    eligible_asset_states=frozenset({"OK", "WATCH", "DEGRADED"}),
    excluded_asset_states=frozenset(),
)


def _candidate(hub_id: str, **overrides: object) -> HubPqCandidate:
    base: dict[str, object] = {
        "hub_id": hub_id,
        "phase_connection": "A",
        "kva_rating": 20.0,
        "pf_min_leading": 0.95,
        "pf_min_lagging": 0.95,
        "freq_offset_hz": 0.0,
        "voltage_offset_pct": 0.2,
        "thd_current_pct": 1.0,
        "phase_angle_error_deg": 0.5,
        "quality_score": 0.95,
        "ride_through_class": "CATEGORY_III",
        "asset_state": "OK",
        "dominant_harmonics": None,
    }
    base.update(overrides)
    return HubPqCandidate(**base)  # type: ignore[arg-type]


def _harmonics(angle_deg: float, mag: float = 2.0) -> dict[int, complex]:
    return {3: cmath.rect(mag, math.radians(angle_deg))}


# --- TS-12c: a batch that would breach max_phase_imbalance_pct is rejected, not signed ------------------
# (at the eligibility-filter level: the S5.5.3 asset-state floor and ride-through/quality gates keep an
# outlier/ineligible hub out of the candidate pool BEFORE water-fill ever proposes it, so no such hub can
# reach a grant the guardian would need to veto.)


def test_ts12c_style_quarantined_hub_never_eligible_for_pq_sensitive_profile() -> None:
    candidates = [_candidate("h1"), _candidate("h2", asset_state="QUARANTINED")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    assert result.eligible_hub_ids == ("h1",)
    excluded = {v.hub_id: v.reason_code for v in result.excluded}
    assert excluded["h2"] == PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED


@pytest.mark.parametrize("state", sorted(ALWAYS_EXCLUDED_ASSET_STATES))
def test_ts17b_style_always_excluded_states_never_eligible_for_any_pq_sensitive_profile(state: str) -> None:
    """TS-17b: QUARANTINED/AWAITING_REPLACEMENT/RECOMMISSIONING never appear in an eligible set, for any
    profile except HOME (HOME never routes through this filter)."""
    candidates = [_candidate("h1", asset_state=state)]
    assert evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG).eligible_hub_ids == ()
    assert evaluate_hub_eligibility(candidates, _ARBITRAGE_CONFIG).eligible_hub_ids == ()


# --- TS-12a: locked-phase / degraded-quality inverters excluded or reduced for a PQ-sensitive profile ----


def test_ts12a_style_degraded_excluded_immediately_for_pq_sensitive() -> None:
    candidates = [_candidate("h1"), _candidate("h2", asset_state="DEGRADED")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    assert result.eligible_hub_ids == ("h1",)


def test_below_min_quality_score_excluded_for_pq_sensitive() -> None:
    candidates = [_candidate("h1"), _candidate("h2", quality_score=0.5)]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    excluded = {v.hub_id: v.reason_code for v in result.excluded}
    assert excluded["h2"] == PQ_ELIGIBILITY_QUALITY_SCORE_BELOW_MINIMUM


def test_ride_through_insufficient_excluded() -> None:
    candidates = [_candidate("h1"), _candidate("h2", ride_through_class="CATEGORY_I")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    excluded = {v.hub_id: v.reason_code for v in result.excluded}
    assert excluded["h2"] == PQ_ELIGIBILITY_RIDE_THROUGH_INSUFFICIENT


def test_unknown_ride_through_class_fails_closed() -> None:
    candidates = [_candidate("h1", ride_through_class="BOGUS")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    assert result.excluded[0].reason_code == PQ_ELIGIBILITY_RIDE_THROUGH_UNKNOWN


def test_kva_circle_infeasible_point_excluded() -> None:
    config = EligibilityConfig(
        pq_aware_selection=True,
        min_quality_score=0.0,
        required_ride_through_class="CATEGORY_III",
        eligible_asset_states=frozenset({"OK"}),
        excluded_asset_states=frozenset(),
        requested_p_kw=19.0,
        requested_q_kvar=15.0,  # sqrt(19^2+15^2) ~ 24.2 kVA > 20 kVA rating -> infeasible
    )
    candidates = [_candidate("h1", kva_rating=20.0)]
    result = evaluate_hub_eligibility(candidates, config)
    assert result.excluded[0].reason_code == PQ_ELIGIBILITY_KVA_INFEASIBLE


def test_thd_outlier_excluded_when_pq_aware() -> None:
    candidates = [_candidate(f"h{i}", thd_current_pct=1.0) for i in range(10)]
    candidates.append(_candidate("outlier", thd_current_pct=50.0))
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    excluded = {v.hub_id: v.reason_code for v in result.excluded}
    assert excluded["outlier"] == PQ_ELIGIBILITY_OUTLIER_THD


def test_voltage_outlier_excluded_when_pq_aware() -> None:
    candidates = [_candidate(f"h{i}", voltage_offset_pct=0.1) for i in range(10)]
    candidates.append(_candidate("outlier", voltage_offset_pct=10.0))
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    excluded = {v.hub_id: v.reason_code for v in result.excluded}
    assert excluded["outlier"] == PQ_ELIGIBILITY_OUTLIER_VOLTAGE_OFFSET


def test_degenerate_zero_spread_never_flags_an_outlier() -> None:
    candidates = [_candidate(f"h{i}", thd_current_pct=2.0, voltage_offset_pct=0.3) for i in range(5)]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    assert len(result.eligible_hub_ids) == 5


# --- TS-13a: arbitrage draws from PQ-excluded-for-others hubs, grid-code minimum only --------------------


def test_ts13a_arbitrage_uses_grid_code_minimum_only() -> None:
    """A hub excluded from DATA_CENTER on quality/outlier grounds remains eligible for the arbitrage
    (pq_aware_selection=False) profile as long as it clears the asset-state floor and ride-through."""
    candidates = [_candidate("h1", quality_score=0.1, thd_current_pct=50.0, voltage_offset_pct=20.0)]
    dc_result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    arb_result = evaluate_hub_eligibility(candidates, _ARBITRAGE_CONFIG)
    assert dc_result.eligible_hub_ids == ()
    assert arb_result.eligible_hub_ids == ("h1",)


def test_ts13a_degraded_hub_eligible_for_arbitrage_but_not_data_center() -> None:
    """S5.5.3's table: DEGRADED is excluded immediately for PQ-sensitive profiles, but still eligible
    for a grid-code-minimum (arbitrage) profile."""
    candidates = [_candidate("h1", asset_state="DEGRADED")]
    assert evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG).eligible_hub_ids == ()
    assert evaluate_hub_eligibility(candidates, _ARBITRAGE_CONFIG).eligible_hub_ids == ("h1",)


def test_ts13a_no_diversity_weighting_for_arbitrage() -> None:
    """ES13: the diversity-weighting term must not reduce the arbitrage obligation's delivered kW --
    every eligible hub keeps a neutral (1.0) weight when pq_aware_selection is False."""
    candidates = [
        _candidate("h1", dominant_harmonics=_harmonics(0.0)),
        _candidate("h2", dominant_harmonics=_harmonics(0.0)),
    ]
    result = evaluate_hub_eligibility(candidates, _ARBITRAGE_CONFIG)
    assert all(v.diversity_weight == 1.0 for v in result.verdicts)


# --- S5.2 step 2: diversity weighting (S3.2b stacking vs. cancellation) -----------------------------------


def test_harmonic_diversity_prefers_the_angle_far_from_the_bank_mean() -> None:
    """A candidate whose own dominant-harmonic angle matches the bank's current resultant (would STACK)
    gets a strictly lower diversity weight than one ~180 degrees away (would CANCEL) -- S3.2(b)."""
    stacking = [_candidate(f"s{i}", dominant_harmonics=_harmonics(0.0)) for i in range(5)]
    near_mean = _candidate("near", dominant_harmonics=_harmonics(5.0))
    far_from_mean = _candidate("far", dominant_harmonics=_harmonics(175.0))
    result = evaluate_hub_eligibility([*stacking, near_mean, far_from_mean], _DATA_CENTER_CONFIG)
    weights = {v.hub_id: v.diversity_weight for v in result.verdicts}
    assert weights["far"] > weights["near"]


def test_missing_harmonic_data_is_neutral_weight() -> None:
    candidates = [_candidate("h1", dominant_harmonics=None), _candidate("h2", dominant_harmonics=None)]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    assert all(v.diversity_weight == 1.0 for v in result.verdicts)


def test_watch_state_deprioritized_but_still_eligible() -> None:
    candidates = [_candidate("ok"), _candidate("watch", asset_state="WATCH")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    weights = {v.hub_id: v.diversity_weight for v in result.verdicts}
    assert set(result.eligible_hub_ids) == {"ok", "watch"}
    assert weights["watch"] < weights["ok"]


# --- S3.2(c) phase-balance weighting --------------------------------------------------------------------


def test_phase_balance_prefers_the_under_loaded_phase() -> None:
    candidates = [_candidate("a", phase_connection="A"), _candidate("b", phase_connection="B")]
    weights = phase_balance_weights(candidates, {"A": 100.0, "B": 10.0})
    assert weights["b"] > weights["a"]


def test_phase_balance_neutral_with_fewer_than_two_phases_reported() -> None:
    candidates = [_candidate("a", phase_connection="A")]
    assert phase_balance_weights(candidates, {"A": 100.0}) == {"a": 1.0}


def test_phase_balance_neutral_for_multi_phase_hub() -> None:
    candidates = [_candidate("abc", phase_connection="ABC")]
    weights = phase_balance_weights(candidates, {"A": 100.0, "B": 10.0})
    assert weights["abc"] == 1.0


# --- apply_eligibility: the HubSnapshot integration seam ---------------------------------------------


def test_apply_eligibility_drops_ineligible_and_scales_tau() -> None:
    candidates = [_candidate("h1"), _candidate("h2", asset_state="QUARANTINED")]
    result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
    hubs = (
        HubSnapshot(hub_id="h1", bank_id="b1", free_discharge_kw=10.0, tau=1.0),
        HubSnapshot(hub_id="h2", bank_id="b1", free_discharge_kw=10.0, tau=1.0),
    )
    survivors = apply_eligibility(hubs, result)
    assert [h.hub_id for h in survivors] == ["h1"]
    assert survivors[0].tau == pytest.approx(
        1.0 * next(v.diversity_weight for v in result.verdicts if v.eligible)
    )


def test_apply_eligibility_composes_phase_balance_weight() -> None:
    candidates = [_candidate("a", phase_connection="A"), _candidate("b", phase_connection="B")]
    result = evaluate_hub_eligibility(candidates, _ARBITRAGE_CONFIG)
    hubs = (
        HubSnapshot(hub_id="a", bank_id="b1", free_discharge_kw=10.0, tau=1.0),
        HubSnapshot(hub_id="b", bank_id="b1", free_discharge_kw=10.0, tau=1.0),
    )
    survivors = apply_eligibility(
        hubs, result, phase_by_hub_id={"a": "A", "b": "B"}, phase_kw_by_phase={"A": 100.0, "B": 10.0}
    )
    by_id = {h.hub_id: h.tau for h in survivors}
    assert by_id["b"] > by_id["a"]


def test_empty_candidate_set_returns_empty_result() -> None:
    result = evaluate_hub_eligibility([], _DATA_CENTER_CONFIG)
    assert result.eligible_hub_ids == ()
    assert result.excluded == ()


# --- Determinism / order-independence ---------------------------------------------------------------


@given(st.permutations([f"h{i}" for i in range(8)]))
def test_property_result_independent_of_input_order(order: list[str]) -> None:
    base = {f"h{i}": _candidate(f"h{i}", quality_score=0.5 + 0.05 * i) for i in range(8)}
    candidates_in_order = [base[hub_id] for hub_id in order]
    result = evaluate_hub_eligibility(candidates_in_order, _DATA_CENTER_CONFIG)
    reference = evaluate_hub_eligibility(list(base.values()), _DATA_CENTER_CONFIG)
    assert result.eligible_hub_ids == reference.eligible_hub_ids
