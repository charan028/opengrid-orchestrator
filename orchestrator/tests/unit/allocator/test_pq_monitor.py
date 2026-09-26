"""TS-14a/14b-style coverage at the allocator level (07-delivery/06-service-profiles-and-power-quality.md
S5.4): continuous PQ monitoring hysteresis and the corrective-action ladder.

`core.pq.step_hysteresis` commits a verdict change only after a "candidate" verdict has been observed
(S5.4's chatter-prevention dwell, tested in `opengrid.core.pq`'s own test suite) -- the FIRST observation
of a new verdict always just sets the candidate, and a second observation (at or after its own dwell
window) commits it. Tests here use a zero-dwell `HysteresisConfig` and the `_settle` helper below to
observe the COMMITTED verdict deterministically without asserting on `core.pq`'s own dwell-timing
mechanics a second time.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.allocator import reasons
from opengrid.allocator.pq_eligibility import HubPqCandidate
from opengrid.allocator.pq_monitor import (
    DEFAULT_MAX_BREACH_CYCLES,
    PQ_MONITOR_WARN_ALERT,
    R_PQ_DRIFT_AT_RISK,
    CorrectionActionType,
    LadderState,
    MonitorResult,
    evaluate_obligation_pq,
    rank_hubs_by_deviation,
)
from opengrid.core.pq import ComplianceState, HysteresisConfig, PqEnvelopeLimits, PqMeasurement

LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=1.5,
    voltage_band_pct=2.0,
    freq_tolerance_hz=0.5,
    pf_min=0.95,
    thd_voltage_limit_pct=2.5,
    thd_current_limit_pct=2.5,
)

#: Zero dwell everywhere -- isolates the LADDER logic under test from `core.pq.step_hysteresis`'s own
#: (separately tested) dwell-timing mechanics.
_INSTANT = HysteresisConfig(warn_dwell_s=0.0, breach_dwell_s=0.0, recovery_dwell_s=0.0)


def _measurement(**overrides: float) -> PqMeasurement:
    base: dict[str, float] = {
        "imbalance_pct": 0.2,
        "voltage_deviation_pct": 0.2,
        "freq_deviation_hz": 0.02,
        "pf": 0.98,
        "thd_voltage_pct": 0.5,
        "thd_current_pct": 0.5,
    }
    base.update(overrides)
    return PqMeasurement(**base)  # type: ignore[arg-type]


def _settle(
    obligation_id: str, measurement: PqMeasurement, state: LadderState, now_s: float, **kwargs: object
) -> MonitorResult:
    """Two observations of the same `measurement` to commit `step_hysteresis`'s candidate verdict
    immediately (zero-dwell config) -- see this module's own docstring."""
    primed = evaluate_obligation_pq(
        obligation_id, measurement, LIMITS, state, now_s, hysteresis_config=_INSTANT, **kwargs
    )
    return evaluate_obligation_pq(
        obligation_id, measurement, LIMITS, primed.ladder_state, now_s, hysteresis_config=_INSTANT, **kwargs
    )


def test_nominal_measurement_yields_no_actions() -> None:
    result = evaluate_obligation_pq("ob-1", _measurement(), LIMITS, LadderState(), now_s=0.0)
    assert result.verdict == ComplianceState.NOMINAL
    assert result.actions == ()
    assert result.ladder_state.cycles_in_breach == 0


def test_first_observation_of_a_new_verdict_only_primes_the_candidate() -> None:
    """Documents the two-step commit this test module's other cases route around via `_settle`."""
    result = evaluate_obligation_pq(
        "ob-1", _measurement(voltage_deviation_pct=5.0), LIMITS, LadderState(), now_s=0.0
    )
    assert result.verdict == ComplianceState.NOMINAL
    assert result.actions == ()
    assert result.ladder_state.hysteresis.candidate == ComplianceState.BREACH


def test_warn_raises_an_alert_and_does_not_act() -> None:
    # 75% of the 2.0% voltage band -> WARN (>= 70% default threshold).
    result = _settle("ob-1", _measurement(voltage_deviation_pct=1.5), LadderState(), now_s=0.0)
    assert result.verdict == ComplianceState.WARN
    assert len(result.actions) == 1
    assert result.actions[0].action == CorrectionActionType.ALERT
    assert result.actions[0].reason_code == PQ_MONITOR_WARN_ALERT


def test_breach_proposes_ladder_in_order_rebalance_then_pf_then_exclude() -> None:
    result = _settle("ob-1", _measurement(voltage_deviation_pct=5.0), LadderState(), now_s=0.0)
    assert result.verdict == ComplianceState.BREACH
    action_types = [a.action for a in result.actions]
    assert action_types == [
        CorrectionActionType.REBALANCE_PHASES,
        CorrectionActionType.ADJUST_PF,
        CorrectionActionType.EXCLUDE_HUB,
    ]


def test_breach_includes_substitution_when_a_candidate_exists() -> None:
    result = _settle(
        "ob-1",
        _measurement(voltage_deviation_pct=5.0),
        LadderState(),
        now_s=0.0,
        has_substitution_candidate=True,
    )
    action_types = [a.action for a in result.actions]
    assert CorrectionActionType.SUBSTITUTE_HUBS in action_types
    sub_action = next(a for a in result.actions if a.action == CorrectionActionType.SUBSTITUTE_HUBS)
    assert sub_action.reason_code == reasons.R_SUBSTITUTION
    # S5.4: substitution comes after rebalance, before PF adjustment/exclusion.
    assert action_types.index(CorrectionActionType.SUBSTITUTE_HUBS) == 1
    assert action_types.index(CorrectionActionType.ADJUST_PF) > action_types.index(
        CorrectionActionType.SUBSTITUTE_HUBS
    )


def test_breach_includes_recalibration_handoff_marker_when_eligible() -> None:
    result = _settle(
        "ob-1",
        _measurement(voltage_deviation_pct=5.0),
        LadderState(),
        now_s=0.0,
        has_substitution_candidate=True,
        recalibration_eligible=True,
    )
    action_types = [a.action for a in result.actions]
    assert action_types == [
        CorrectionActionType.REBALANCE_PHASES,
        CorrectionActionType.SUBSTITUTE_HUBS,
        CorrectionActionType.RECALIBRATE,
        CorrectionActionType.ADJUST_PF,
        CorrectionActionType.EXCLUDE_HUB,
    ]


def test_ts14a_style_committed_kw_is_never_part_of_the_ladder_decision() -> None:
    """TS-14a/ES14: this module never touches committed kW -- it has no such parameter at all, so the
    ladder cannot reduce it by construction (K13)."""
    import inspect

    params = inspect.signature(evaluate_obligation_pq).parameters
    assert "committed_kw" not in params
    assert "granted_kw" not in params


def test_ts14b_style_escalates_to_at_risk_only_after_max_breach_cycles() -> None:
    """TS-14b: reaches AT_RISK if and only if the ladder fails to restore compliance within the
    configured cycle budget."""
    breaching = _measurement(voltage_deviation_pct=5.0)
    settled = _settle("ob-1", breaching, LadderState(), now_s=0.0)
    assert settled.ladder_state.cycles_in_breach == 1
    assert not settled.ladder_state.at_risk

    state = settled.ladder_state
    for cycle in range(1, DEFAULT_MAX_BREACH_CYCLES - 1):
        result = evaluate_obligation_pq("ob-1", breaching, LIMITS, state, now_s=float(cycle))
        assert not result.ladder_state.at_risk
        assert CorrectionActionType.ESCALATE_AT_RISK not in [a.action for a in result.actions]
        state = result.ladder_state

    result = evaluate_obligation_pq("ob-1", breaching, LIMITS, state, now_s=float(DEFAULT_MAX_BREACH_CYCLES))
    assert result.ladder_state.at_risk
    escalation = next(a for a in result.actions if a.action == CorrectionActionType.ESCALATE_AT_RISK)
    assert escalation.reason_code == R_PQ_DRIFT_AT_RISK


def test_ts14b_style_recovery_before_escalation_never_reaches_at_risk() -> None:
    """ES14: "if compliance is restored before step 6 the obligation never reaches AT_RISK"."""
    breaching = _measurement(voltage_deviation_pct=5.0)
    settled = _settle("ob-1", breaching, LadderState(), now_s=0.0)
    assert not settled.ladder_state.at_risk

    recovered = _settle("ob-1", _measurement(), settled.ladder_state, now_s=1.0)
    assert recovered.verdict == ComplianceState.NOMINAL
    assert not recovered.ladder_state.at_risk
    assert recovered.ladder_state.cycles_in_breach == 0

    # Breaching again afterwards restarts the cycle counter from zero, not from where it left off.
    again = _settle("ob-1", breaching, recovered.ladder_state, now_s=2.0)
    assert again.ladder_state.cycles_in_breach == 1


def test_recovery_uses_hysteresis_not_an_immediate_flip() -> None:
    """S5.4: recovery requires the quantity to fall below 60% of the limit for the recovery dwell
    window, not merely below 100% -- a single sample just under the breach threshold must not
    instantly clear the state (even with zero dwell, RATIO 0.95 is still above the 60% recovery
    threshold, so it can never become a NOMINAL/recovery candidate at all)."""
    settled = _settle("ob-1", _measurement(voltage_deviation_pct=5.0), LadderState(), now_s=0.0)
    assert settled.verdict == ComplianceState.BREACH
    almost_recovered = evaluate_obligation_pq(
        "ob-1",
        _measurement(voltage_deviation_pct=1.9),
        LIMITS,
        settled.ladder_state,
        now_s=1.0,
        hysteresis_config=_INSTANT,
    )
    assert almost_recovered.verdict == ComplianceState.BREACH  # 95% of limit is still >= recovery_ratio


# --- rank_hubs_by_deviation ------------------------------------------------------------------------


def _pq_candidate(hub_id: str, **overrides: object) -> HubPqCandidate:
    base: dict[str, object] = {
        "hub_id": hub_id,
        "phase_connection": "A",
        "kva_rating": 20.0,
        "pf_min_leading": 0.95,
        "pf_min_lagging": 0.95,
        "freq_offset_hz": 0.0,
        "voltage_offset_pct": 0.1,
        "thd_current_pct": 0.5,
        "phase_angle_error_deg": 0.5,
        "quality_score": 0.95,
        "ride_through_class": "CATEGORY_III",
        "asset_state": "OK",
    }
    base.update(overrides)
    return HubPqCandidate(**base)  # type: ignore[arg-type]


def test_rank_hubs_by_deviation_orders_worst_first() -> None:
    hubs = [
        _pq_candidate("clean", thd_current_pct=0.2),
        _pq_candidate("drifting", thd_current_pct=5.0),
        _pq_candidate("mid", thd_current_pct=1.0),
    ]
    ranked = rank_hubs_by_deviation(hubs, LIMITS)
    assert ranked[0] == "drifting"
    assert ranked[-1] == "clean"


def test_rank_hubs_by_deviation_empty_input() -> None:
    assert rank_hubs_by_deviation([], LIMITS) == ()


def test_rank_hubs_by_deviation_deterministic_ties() -> None:
    hubs = [_pq_candidate("b"), _pq_candidate("a")]
    assert rank_hubs_by_deviation(hubs, LIMITS) == rank_hubs_by_deviation(list(reversed(hubs)), LIMITS)


# --- Property: monotonic escalation ------------------------------------------------------------------


@given(st.integers(min_value=1, max_value=6))
def test_property_at_risk_only_at_or_after_configured_cycle_budget(max_cycles: int) -> None:
    breaching = _measurement(thd_current_pct=10.0)
    settled = _settle("ob-1", breaching, LadderState(), now_s=0.0, max_breach_cycles=max_cycles)
    state = settled.ladder_state
    assert settled.ladder_state.at_risk == (max_cycles <= 1)

    for cycle in range(1, max_cycles):
        result = evaluate_obligation_pq(
            "ob-1", breaching, LIMITS, state, now_s=float(cycle), max_breach_cycles=max_cycles
        )
        if cycle < max_cycles - 1:
            assert not result.ladder_state.at_risk
        else:
            assert result.ladder_state.at_risk
        state = result.ladder_state
