from __future__ import annotations

import cmath
import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.pq.aggregation import (
    aggregate_kva_circle,
    aggregate_offset_std,
    bank_thd_current_pct,
    harmonic_vector_sum,
    inverter_quality_score,
    kva_point_feasible,
    per_phase_imbalance_pct,
)
from opengrid.core.pq.types import KvaCircle


def test_aggregate_offset_std_shrinks_as_one_over_sqrt_n() -> None:
    assert aggregate_offset_std(0.01, 1) == pytest.approx(0.01)
    assert aggregate_offset_std(0.01, 4) == pytest.approx(0.005)
    assert aggregate_offset_std(0.01, 2500) == pytest.approx(0.0002)


def test_aggregate_offset_std_rejects_non_positive_n() -> None:
    with pytest.raises(ValueError, match="n >= 1"):
        aggregate_offset_std(0.01, 0)


def test_harmonic_vector_sum_locked_phase_stacks_linearly() -> None:
    n = 10
    phasors = [cmath.rect(1.0, math.radians(40.0))] * n
    assert abs(harmonic_vector_sum(phasors)) == pytest.approx(n * 1.0, rel=1e-9)


def test_harmonic_vector_sum_cancellation_below_linear_stack() -> None:
    """S3.2(b): i.i.d. uniform phase angles grow the vector sum as a random walk (~sqrt(N)), always
    strictly below the fully-stacked linear worst case (N). Averaged over many draws to make the
    statistical claim robust to a single noisy sample."""
    rng = np.random.default_rng(42)
    n = 200
    trials = 200
    magnitudes = []
    for _ in range(trials):
        angles = rng.uniform(0, 2 * math.pi, size=n)
        phasors = [cmath.rect(1.0, angle) for angle in angles]
        magnitude = abs(harmonic_vector_sum(phasors))
        assert magnitude < n * 1.0  # diverse phases never stack to the full linear worst case
        magnitudes.append(magnitude)
    mean_magnitude = sum(magnitudes) / trials
    # E[|sum of N unit phasors, iid uniform angle|] = sqrt(pi*N)/2 (Rayleigh random-walk result).
    expected_mean = math.sqrt(math.pi * n) / 2.0
    assert mean_magnitude == pytest.approx(expected_mean, rel=0.15)


def test_bank_thd_current_pct_single_hub_matches_its_own_ratio() -> None:
    fundamental_a = 10.0
    thd_target_pct = 4.0
    mag = fundamental_a * thd_target_pct / 100.0
    result = bank_thd_current_pct([{3: complex(mag, 0.0)}], [fundamental_a])
    assert result == pytest.approx(thd_target_pct, rel=1e-9)


def test_bank_thd_current_pct_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        bank_thd_current_pct([{3: 1 + 0j}], [1.0, 2.0])


def test_bank_thd_current_pct_rejects_empty_fleet() -> None:
    with pytest.raises(ValueError, match="at least one hub"):
        bank_thd_current_pct([], [])


@given(
    n=st.integers(min_value=1, max_value=25),
    per_unit_thd_pct=st.floats(min_value=0.1, max_value=10.0),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_bank_thd_never_exceeds_the_uniform_individual_thd(
    n: int, per_unit_thd_pct: float, seed: int
) -> None:
    """S3.2(b) property: for a homogeneous fleet (same per-unit |harmonic|/fundamental ratio), the
    vector-summed bank THD can never exceed the individual unit's THD, whatever the phase angles --
    by the triangle inequality the vector sum's magnitude is at most the linear (fully-stacked) sum,
    and diverse phases only ever pull it below that ceiling (the aggregated-THD-improves-with-
    diversity claim of S3.2(b))."""
    rng = np.random.default_rng(seed)
    fundamental_a = 10.0
    mag = fundamental_a * per_unit_thd_pct / 100.0
    order = 3
    angles = rng.uniform(0, 2 * math.pi, size=n)
    per_hub_harmonics = [{order: cmath.rect(mag, angle)} for angle in angles]
    per_hub_fundamental = [fundamental_a] * n
    bank_thd = bank_thd_current_pct(per_hub_harmonics, per_hub_fundamental)
    assert bank_thd <= per_unit_thd_pct + 1e-6


@given(value=st.floats(min_value=0.01, max_value=100_000.0, allow_nan=False, allow_infinity=False))
def test_per_phase_imbalance_zero_for_any_balanced_set(value: float) -> None:
    """S3.2(c)/S2 property: three equal phase currents (or voltages) always yield 0% imbalance."""
    assert per_phase_imbalance_pct({"A": value, "B": value, "C": value}) == pytest.approx(0.0, abs=1e-6)


def test_aggregate_kva_circle_sums_ratings_and_takes_tightest_pf() -> None:
    circle = aggregate_kva_circle([11.0, 11.0, 20.0], [0.90, 0.92, 0.95], [0.90, 0.88, 0.93])
    assert circle.kva_rating == pytest.approx(42.0)
    assert circle.pf_min_lagging == pytest.approx(0.95)  # tightest (highest) member limit
    assert circle.pf_min_leading == pytest.approx(0.93)


def test_aggregate_kva_circle_rejects_empty_fleet() -> None:
    with pytest.raises(ValueError, match="at least one member"):
        aggregate_kva_circle([], [], [])


def test_kva_point_feasible_inside_and_outside_circle() -> None:
    circle = KvaCircle(kva_rating=10.0, pf_min_lagging=0.9, pf_min_leading=0.9)
    assert kva_point_feasible(9.0, 0.0, circle) is True  # unity PF, within rating
    assert kva_point_feasible(9.0, 9.0, circle) is False  # exceeds kVA rating
    assert kva_point_feasible(1.0, 9.5, circle) is False  # within rating but PF too low


def test_inverter_quality_score_clipped_to_unit_interval() -> None:
    assert inverter_quality_score(0.0, 0.0, 0.0, 0.0) == pytest.approx(1.0)
    assert inverter_quality_score(10.0, 10.0, 10.0, 10.0) == 0.0
