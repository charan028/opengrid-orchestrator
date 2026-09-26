"""Envelope compliance verdicts and monitoring-loop hysteresis (07-delivery/06 K14, G-21..G-23, S5.4).

Pure functions: the allocator's post-assignment self-check (S5.2 step 3), the guardian's independent
G-21..G-23 re-derivation (S5.3), and the continuous PQ-monitoring loop (S5.4) all call these on their
own independently-read inputs (the "primary check + independent check" shape K2/K14 require) --
never re-derived per caller.
"""

from __future__ import annotations

from collections.abc import Mapping

from opengrid.core.pq.constants import BREACH_RATIO_DEFAULT, WARN_RATIO_DEFAULT
from opengrid.core.pq.types import (
    ComplianceState,
    HysteresisConfig,
    HysteresisState,
    PqEnvelopeLimits,
    PqMeasurement,
)

_DEFAULT_HYSTERESIS_CONFIG = HysteresisConfig()

_SEVERITY: dict[ComplianceState, int] = {
    ComplianceState.NOMINAL: 0,
    ComplianceState.WARN: 1,
    ComplianceState.BREACH: 2,
}


def _ratio(value: float, limit: float) -> float:
    if limit <= 0.0:
        return 0.0 if value <= 0.0 else float("inf")
    return abs(value) / limit


def compliance_ratios(measurement: PqMeasurement, limits: PqEnvelopeLimits) -> dict[str, float]:
    """Ratio of each monitored quantity to its envelope limit (>= 1.0 means at/over limit) -- S5.4's
    "70%/100%/60% of the envelope limit" thresholds are expressed against this ratio. `current_a` is
    included only when both the measurement and the envelope carry a current cap (S2's
    `current_limit_a`, optional)."""
    ratios = {
        "imbalance_pct": _ratio(measurement.imbalance_pct, limits.max_phase_imbalance_pct),
        "voltage_deviation_pct": _ratio(measurement.voltage_deviation_pct, limits.voltage_band_pct),
        "freq_deviation_hz": _ratio(measurement.freq_deviation_hz, limits.freq_tolerance_hz),
        "thd_voltage_pct": _ratio(measurement.thd_voltage_pct, limits.thd_voltage_limit_pct),
        "thd_current_pct": _ratio(measurement.thd_current_pct, limits.thd_current_limit_pct),
    }
    if limits.current_limit_a is not None and measurement.current_a is not None:
        ratios["current_a"] = _ratio(measurement.current_a, limits.current_limit_a)
    return ratios


def evaluate_envelope(
    measurement: PqMeasurement,
    limits: PqEnvelopeLimits,
    *,
    warn_ratio: float = WARN_RATIO_DEFAULT,
    breach_ratio: float = BREACH_RATIO_DEFAULT,
) -> dict[str, ComplianceState]:
    """K14/G-21..G-23: per-dimension PASS(NOMINAL)/WARN/BREACH verdict -- WARN at >= `warn_ratio` of
    the limit (default 70%), BREACH at >= `breach_ratio` (default 100%). Use `worst_verdict` for the
    single overall verdict; per-dimension detail is kept for S6.6's PQ panel."""
    ratios = compliance_ratios(measurement, limits)
    return {
        dimension: (
            ComplianceState.BREACH
            if ratio >= breach_ratio
            else ComplianceState.WARN
            if ratio >= warn_ratio
            else ComplianceState.NOMINAL
        )
        for dimension, ratio in ratios.items()
    }


def worst_verdict(verdicts: Mapping[str, ComplianceState]) -> ComplianceState:
    """The single most-severe verdict across dimensions (NOMINAL < WARN < BREACH); NOMINAL if empty."""
    if not verdicts:
        return ComplianceState.NOMINAL
    return max(verdicts.values(), key=lambda state: _SEVERITY[state])


def _target_state(ratio: float, current: ComplianceState, config: HysteresisConfig) -> ComplianceState:
    """The instantaneous (undwelled) verdict this single observation would produce, given the state
    currently held -- escalation crosses the upper (warn/breach) thresholds; de-escalation only
    crosses the lower `recovery_ratio` threshold, per S5.4's asymmetric hysteresis."""
    if current == ComplianceState.NOMINAL:
        if ratio >= config.breach_ratio:
            return ComplianceState.BREACH
        if ratio >= config.warn_ratio:
            return ComplianceState.WARN
        return ComplianceState.NOMINAL
    if current == ComplianceState.WARN:
        if ratio >= config.breach_ratio:
            return ComplianceState.BREACH
        if ratio < config.recovery_ratio:
            return ComplianceState.NOMINAL
        return ComplianceState.WARN
    # current == BREACH
    if ratio < config.recovery_ratio:
        return ComplianceState.NOMINAL
    return ComplianceState.BREACH


def _dwell_s(target: ComplianceState, config: HysteresisConfig) -> float:
    if target == ComplianceState.WARN:
        return config.warn_dwell_s
    if target == ComplianceState.BREACH:
        return config.breach_dwell_s
    return config.recovery_dwell_s


def step_hysteresis(
    state: HysteresisState,
    ratio: float,
    now_s: float,
    config: HysteresisConfig = _DEFAULT_HYSTERESIS_CONFIG,
) -> HysteresisState:
    """S5.4's WARN/BREACH/recovery dwell state machine, advanced by one observation.

    A verdict change only commits once the instantaneous target has been sustained for its dwell
    window (WARN: default 60 s; BREACH: the `ServiceProfile.deadband`/`accuracy_tolerance` window,
    passed via `config.breach_dwell_s`; recovery: default 60 s below `recovery_ratio`) -- this is the
    chatter-prevention pattern already used for the $5/MWh price-response dwell and the
    `DIST_DEFERRAL` deadband. `now_s` is an injected clock reading (BUILD.md S5a: no flaky sleeps);
    callers own the clock.
    """
    target = _target_state(ratio, state.verdict, config)
    if target == state.verdict:
        return HysteresisState(verdict=state.verdict)
    if state.candidate != target or state.candidate_since_s is None:
        return HysteresisState(verdict=state.verdict, candidate=target, candidate_since_s=now_s)
    if now_s - state.candidate_since_s >= _dwell_s(target, config):
        return HysteresisState(verdict=target)
    return state
