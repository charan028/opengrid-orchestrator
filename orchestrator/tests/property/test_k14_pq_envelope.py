"""K14 power-quality envelope (00-invariants.md K14; 06-service-profiles-and-power-quality.md S5.3, G-21..G-23).

Dispatch serving a customer must keep aggregated imbalance, voltage/frequency deviation and THD within that
customer's envelope, and a breach must never stand silently. Guardian G-21..G-23 are not wired yet, so these
properties pin the pure `opengrid.core.pq` functions they (and the allocator self-check) are specified to call.
"""

from __future__ import annotations

import math
from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.pq import (
    ComplianceState,
    HysteresisConfig,
    HysteresisState,
    KvaCircle,
    PqEnvelopeLimits,
    PqMeasurement,
    aggregate_offset_std,
    bank_thd_current_pct,
    compliance_ratios,
    evaluate_envelope,
    kva_point_feasible,
    per_phase_imbalance_pct,
    step_hysteresis,
    worst_verdict,
)

_SEVERITY = {ComplianceState.NOMINAL: 0, ComplianceState.WARN: 1, ComplianceState.BREACH: 2}
_EPSILON = 1e-9

_limit = st.floats(min_value=0.1, max_value=20.0, allow_nan=False)
_envelopes = st.builds(
    PqEnvelopeLimits,
    max_phase_imbalance_pct=_limit,
    voltage_band_pct=_limit,
    freq_tolerance_hz=_limit,
    pf_min=st.floats(min_value=0.5, max_value=1.0),
    thd_voltage_limit_pct=_limit,
    thd_current_limit_pct=_limit,
)
_reading = st.floats(min_value=0.0, max_value=40.0, allow_nan=False)
_measurements = st.builds(
    PqMeasurement,
    imbalance_pct=_reading,
    voltage_deviation_pct=_reading,
    freq_deviation_hz=_reading,
    pf=st.floats(min_value=0.5, max_value=1.0),
    thd_voltage_pct=_reading,
    thd_current_pct=_reading,
)


def _tightened(limits: PqEnvelopeLimits, factor: float) -> PqEnvelopeLimits:
    return replace(
        limits,
        max_phase_imbalance_pct=limits.max_phase_imbalance_pct * factor,
        voltage_band_pct=limits.voltage_band_pct * factor,
        freq_tolerance_hz=limits.freq_tolerance_hz * factor,
        thd_voltage_limit_pct=limits.thd_voltage_limit_pct * factor,
        thd_current_limit_pct=limits.thd_current_limit_pct * factor,
    )


@given(_measurements, _envelopes)
def test_k14_a_reading_over_any_limit_is_always_a_breach(measurement, limits):
    ratios = compliance_ratios(measurement, limits)

    verdict = worst_verdict(evaluate_envelope(measurement, limits))

    assert (verdict == ComplianceState.BREACH) == any(ratio >= 1.0 for ratio in ratios.values())


@given(_measurements, _envelopes)
def test_k14_a_reading_well_inside_every_limit_is_nominal(measurement, limits):
    ratios = compliance_ratios(measurement, limits)

    verdict = worst_verdict(evaluate_envelope(measurement, limits))

    if all(ratio < 0.7 for ratio in ratios.values()):
        assert verdict == ComplianceState.NOMINAL


@given(_measurements, _envelopes, st.floats(min_value=0.05, max_value=1.0))
def test_k14_tightening_the_envelope_never_improves_the_verdict(measurement, limits, factor):
    loose = worst_verdict(evaluate_envelope(measurement, limits))
    tight = worst_verdict(evaluate_envelope(measurement, _tightened(limits, factor)))

    assert _SEVERITY[tight] >= _SEVERITY[loose]


@given(
    st.lists(st.floats(min_value=0.1, max_value=500.0, allow_nan=False), min_size=1, max_size=3),
    st.floats(min_value=0.1, max_value=10.0),
)
def test_k14_imbalance_is_scale_and_order_invariant(currents, scale):
    phases = {f"L{i}": amps for i, amps in enumerate(currents)}
    reversed_phases = dict(reversed(list(phases.items())))
    scaled = {name: amps * scale for name, amps in phases.items()}

    base = per_phase_imbalance_pct(phases)

    assert base >= 0.0
    assert math.isclose(per_phase_imbalance_pct(reversed_phases), base, rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(per_phase_imbalance_pct(scaled), base, rel_tol=1e-6, abs_tol=1e-6)


@given(st.floats(min_value=0.1, max_value=500.0, allow_nan=False), st.integers(min_value=1, max_value=3))
def test_k14_perfectly_balanced_phases_have_zero_imbalance(amps, phase_count):
    phases = {f"L{i}": amps for i in range(phase_count)}

    assert per_phase_imbalance_pct(phases) <= _EPSILON


_magnitude = st.floats(min_value=0.0, max_value=5.0, allow_nan=False)
_angle = st.floats(min_value=0.0, max_value=2 * math.pi, allow_nan=False)
_hub_harmonics = st.lists(
    st.tuples(_magnitude, _angle, _magnitude, _angle),
    min_size=1,
    max_size=25,
)


@given(_hub_harmonics, st.floats(min_value=10.0, max_value=60.0, allow_nan=False))
def test_k14_bank_thd_never_exceeds_the_worst_case_aligned_bound(hubs, fundamental_a):
    spectra = [
        {3: complex(m3 * math.cos(a3), m3 * math.sin(a3)), 5: complex(m5 * math.cos(a5), m5 * math.sin(a5))}
        for m3, a3, m5, a5 in hubs
    ]
    aligned = [{3: complex(m3, 0.0), 5: complex(m5, 0.0)} for m3, _a3, m5, _a5 in hubs]
    fundamentals = [fundamental_a] * len(hubs)

    actual = bank_thd_current_pct(spectra, fundamentals)
    worst_case = bank_thd_current_pct(aligned, fundamentals)

    assert actual <= worst_case + 1e-6


@given(st.integers(min_value=1, max_value=200), _magnitude, st.floats(min_value=10.0, max_value=60.0))
def test_k14_identical_firmware_harmonics_never_improve_with_fleet_size(count, magnitude, fundamental_a):
    one = bank_thd_current_pct([{5: complex(magnitude, 0.0)}], [fundamental_a])
    many = bank_thd_current_pct([{5: complex(magnitude, 0.0)}] * count, [fundamental_a] * count)

    assert math.isclose(many, one, rel_tol=1e-6, abs_tol=1e-9)


@given(st.floats(min_value=0.0, max_value=5.0), st.integers(min_value=1, max_value=10_000))
def test_k14_random_offset_averaging_never_grows_with_fleet_size(sigma, n):
    assert aggregate_offset_std(sigma, n + 1) <= aggregate_offset_std(sigma, n) + _EPSILON
    assert aggregate_offset_std(sigma, n) <= sigma + _EPSILON


@given(
    st.floats(min_value=1.0, max_value=1000.0),
    st.floats(min_value=0.5, max_value=1.0),
    st.floats(min_value=0.5, max_value=1.0),
    st.floats(min_value=-3000.0, max_value=3000.0),
    st.floats(min_value=-3000.0, max_value=3000.0),
)
def test_k14_a_feasible_setpoint_never_exceeds_the_kva_rating_or_the_power_factor_floor(
    kva, pf_lagging, pf_leading, p_kw, q_kvar
):
    circle = KvaCircle(kva_rating=kva, pf_min_lagging=pf_lagging, pf_min_leading=pf_leading)

    if kva_point_feasible(p_kw, q_kvar, circle):
        apparent = math.hypot(p_kw, q_kvar)
        assert apparent <= kva + 1e-6
        if apparent > 0.0:
            floor = pf_lagging if q_kvar >= 0.0 else pf_leading
            assert abs(p_kw) / apparent >= floor - 1e-6


@given(
    st.floats(min_value=1.0, max_value=50.0),
    st.floats(min_value=1.0, max_value=600.0),
)
def test_k14_one_noisy_sample_never_commits_a_breach(ratio, now_s):
    config = HysteresisConfig()

    after_one = step_hysteresis(HysteresisState(), ratio, now_s, config)

    assert after_one.verdict == ComplianceState.NOMINAL


@given(st.floats(min_value=1.0, max_value=50.0), st.integers(min_value=0, max_value=1_000))
def test_k14_a_sustained_breach_is_committed_after_the_dwell(ratio, start_s):
    config = HysteresisConfig()

    first = step_hysteresis(HysteresisState(), ratio, start_s, config)
    committed = step_hysteresis(first, ratio, start_s + config.breach_dwell_s, config)

    assert committed.verdict == ComplianceState.BREACH


@given(st.floats(min_value=0.0, max_value=0.59), st.integers(min_value=0, max_value=1_000))
def test_k14_a_committed_breach_only_clears_after_sustained_recovery(ratio, start_s):
    config = HysteresisConfig()
    breached = HysteresisState(verdict=ComplianceState.BREACH)

    first = step_hysteresis(breached, ratio, start_s, config)
    early = step_hysteresis(first, ratio, start_s + config.recovery_dwell_s - 1.0, config)
    done = step_hysteresis(first, ratio, start_s + config.recovery_dwell_s, config)

    assert first.verdict == ComplianceState.BREACH
    assert early.verdict == ComplianceState.BREACH
    assert done.verdict == ComplianceState.NOMINAL
