from __future__ import annotations

from opengrid.core.pq.envelope import compliance_ratios, evaluate_envelope, step_hysteresis, worst_verdict
from opengrid.core.pq.types import (
    ComplianceState,
    HysteresisConfig,
    HysteresisState,
    PqEnvelopeLimits,
    PqMeasurement,
)

LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=3.0,
    voltage_band_pct=5.0,
    freq_tolerance_hz=0.5,
    pf_min=0.90,
    thd_voltage_limit_pct=5.0,
    thd_current_limit_pct=5.0,
    current_limit_a=100.0,
)


def _measurement(**overrides: float) -> PqMeasurement:
    base = {
        "imbalance_pct": 0.0,
        "voltage_deviation_pct": 0.0,
        "freq_deviation_hz": 0.0,
        "pf": 1.0,
        "thd_voltage_pct": 0.0,
        "thd_current_pct": 0.0,
        "current_a": 0.0,
    }
    base.update(overrides)
    return PqMeasurement(**base)  # type: ignore[arg-type]


def test_compliance_ratios_nominal_when_zero() -> None:
    ratios = compliance_ratios(_measurement(), LIMITS)
    assert all(ratio == 0.0 for ratio in ratios.values())


def test_compliance_ratios_includes_current_only_when_both_sides_set() -> None:
    limits_no_current = PqEnvelopeLimits(
        max_phase_imbalance_pct=3.0,
        voltage_band_pct=5.0,
        freq_tolerance_hz=0.5,
        pf_min=0.90,
        thd_voltage_limit_pct=5.0,
        thd_current_limit_pct=5.0,
    )
    ratios = compliance_ratios(_measurement(current_a=50.0), limits_no_current)
    assert "current_a" not in ratios


def test_evaluate_envelope_pass_warn_breach_thresholds() -> None:
    verdicts = evaluate_envelope(_measurement(imbalance_pct=1.0), LIMITS)  # 33% of 3.0
    assert verdicts["imbalance_pct"] == ComplianceState.NOMINAL

    verdicts = evaluate_envelope(_measurement(imbalance_pct=2.2), LIMITS)  # ~73% of 3.0
    assert verdicts["imbalance_pct"] == ComplianceState.WARN

    verdicts = evaluate_envelope(_measurement(imbalance_pct=3.5), LIMITS)  # over 100%
    assert verdicts["imbalance_pct"] == ComplianceState.BREACH


def test_worst_verdict_picks_most_severe() -> None:
    verdicts = {"a": ComplianceState.NOMINAL, "b": ComplianceState.WARN, "c": ComplianceState.BREACH}
    assert worst_verdict(verdicts) == ComplianceState.BREACH


def test_worst_verdict_empty_is_nominal() -> None:
    assert worst_verdict({}) == ComplianceState.NOMINAL


def test_step_hysteresis_requires_dwell_before_escalating_to_warn() -> None:
    config = HysteresisConfig(warn_dwell_s=60.0)
    state = HysteresisState()
    state = step_hysteresis(state, ratio=0.75, now_s=0.0, config=config)
    assert state.verdict == ComplianceState.NOMINAL  # candidate only, not yet dwelled
    assert state.candidate == ComplianceState.WARN

    state = step_hysteresis(state, ratio=0.75, now_s=59.0, config=config)
    assert state.verdict == ComplianceState.NOMINAL  # still short of the 60 s dwell

    state = step_hysteresis(state, ratio=0.75, now_s=61.0, config=config)
    assert state.verdict == ComplianceState.WARN


def test_step_hysteresis_recovers_only_below_recovery_ratio_after_dwell() -> None:
    config = HysteresisConfig(recovery_dwell_s=60.0, recovery_ratio=0.60)
    state = HysteresisState(verdict=ComplianceState.WARN)

    # Above the recovery ratio: no de-escalation candidate is started.
    state = step_hysteresis(state, ratio=0.65, now_s=100.0, config=config)
    assert state.verdict == ComplianceState.WARN
    assert state.candidate is None

    state = step_hysteresis(state, ratio=0.50, now_s=100.0, config=config)
    assert state.candidate == ComplianceState.NOMINAL
    state = step_hysteresis(state, ratio=0.50, now_s=159.0, config=config)
    assert state.verdict == ComplianceState.WARN  # dwell not yet satisfied
    state = step_hysteresis(state, ratio=0.50, now_s=161.0, config=config)
    assert state.verdict == ComplianceState.NOMINAL


def test_step_hysteresis_never_chatters_on_a_single_noisy_sample() -> None:
    config = HysteresisConfig(warn_dwell_s=60.0)
    state = HysteresisState()
    state = step_hysteresis(state, ratio=0.95, now_s=0.0, config=config)  # spike toward WARN
    assert state.verdict == ComplianceState.NOMINAL
    state = step_hysteresis(state, ratio=0.10, now_s=1.0, config=config)  # spike clears
    assert state.verdict == ComplianceState.NOMINAL
    assert state.candidate is None


def test_step_hysteresis_escalates_directly_from_nominal_to_breach() -> None:
    config = HysteresisConfig(breach_dwell_s=30.0)
    state = HysteresisState()
    state = step_hysteresis(state, ratio=1.5, now_s=0.0, config=config)
    assert state.candidate == ComplianceState.BREACH
    state = step_hysteresis(state, ratio=1.5, now_s=31.0, config=config)
    assert state.verdict == ComplianceState.BREACH
