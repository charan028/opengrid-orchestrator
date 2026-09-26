from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.pq.calibration import (
    apply_correction,
    calibration_allowed,
    classify_calibration_outcome,
    compute_correction,
    exceeds_watch_threshold,
    is_persistent_drift,
)
from opengrid.core.pq.types import CalibrationBounds, CalibrationOutcome, OffsetVector

TIGHT_TOLERANCE = OffsetVector(freq_hz=0.005, voltage_pct=0.2, phase_deg=1.0)


@given(
    freq=st.floats(min_value=-5.0, max_value=5.0),
    volt=st.floats(min_value=-50.0, max_value=50.0),
    phase=st.floats(min_value=-180.0, max_value=180.0),
    max_freq=st.floats(min_value=1e-3, max_value=5.0),
    max_volt=st.floats(min_value=1e-3, max_value=50.0),
    max_phase=st.floats(min_value=1e-3, max_value=180.0),
)
def test_compute_correction_always_within_bounds(
    freq: float, volt: float, phase: float, max_freq: float, max_volt: float, max_phase: float
) -> None:
    """Property required by the task: a correction never exceeds its configured bounds (S6.7, G-25)."""
    measured = OffsetVector(freq, volt, phase)
    bounds = CalibrationBounds(max_freq, max_volt, max_phase)
    correction = compute_correction(measured, bounds)
    assert abs(correction.freq_hz) <= max_freq + 1e-9
    assert abs(correction.voltage_pct) <= max_volt + 1e-9
    assert abs(correction.phase_deg) <= max_phase + 1e-9


def test_compute_correction_fully_cancels_offset_when_within_bounds() -> None:
    measured = OffsetVector(freq_hz=0.02, voltage_pct=1.0, phase_deg=5.0)
    bounds = CalibrationBounds(max_freq_hz=1.0, max_voltage_pct=10.0, max_phase_deg=20.0)
    correction = compute_correction(measured, bounds)
    residual = apply_correction(measured, correction)
    assert residual.freq_hz == pytest.approx(0.0, abs=1e-9)
    assert residual.voltage_pct == pytest.approx(0.0, abs=1e-9)
    assert residual.phase_deg == pytest.approx(0.0, abs=1e-9)


def test_compute_correction_partial_when_bound_is_tighter_than_offset() -> None:
    measured = OffsetVector(freq_hz=1.0, voltage_pct=0.0, phase_deg=0.0)
    bounds = CalibrationBounds(max_freq_hz=0.1, max_voltage_pct=10.0, max_phase_deg=20.0)
    correction = compute_correction(measured, bounds)
    assert correction.freq_hz == pytest.approx(-0.1)
    residual = apply_correction(measured, correction)
    assert residual.freq_hz == pytest.approx(0.9)  # improved, but not corrected -- bound-limited


def test_compute_correction_rejects_negative_bound() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        compute_correction(OffsetVector(0.0, 0.0, 0.0), CalibrationBounds(-1.0, 1.0, 1.0))


def test_classify_calibration_outcome_corrected() -> None:
    pre = OffsetVector(freq_hz=0.05, voltage_pct=2.0, phase_deg=5.0)
    post = OffsetVector(freq_hz=0.001, voltage_pct=0.05, phase_deg=0.1)
    assert (
        classify_calibration_outcome(pre, post, corrected_tolerance=TIGHT_TOLERANCE)
        == CalibrationOutcome.CORRECTED
    )


def test_classify_calibration_outcome_improved() -> None:
    pre = OffsetVector(freq_hz=0.10, voltage_pct=4.0, phase_deg=10.0)
    post = OffsetVector(freq_hz=0.05, voltage_pct=2.0, phase_deg=5.0)
    assert (
        classify_calibration_outcome(pre, post, corrected_tolerance=TIGHT_TOLERANCE)
        == CalibrationOutcome.IMPROVED
    )


def test_classify_calibration_outcome_no_change() -> None:
    pre = OffsetVector(freq_hz=0.05, voltage_pct=2.0, phase_deg=5.0)
    post = OffsetVector(freq_hz=0.051, voltage_pct=2.01, phase_deg=5.02)
    assert (
        classify_calibration_outcome(pre, post, corrected_tolerance=TIGHT_TOLERANCE)
        == CalibrationOutcome.NO_CHANGE
    )


def test_classify_calibration_outcome_worse_rolled_back() -> None:
    pre = OffsetVector(freq_hz=0.05, voltage_pct=2.0, phase_deg=5.0)
    post = OffsetVector(freq_hz=0.20, voltage_pct=8.0, phase_deg=20.0)
    assert (
        classify_calibration_outcome(pre, post, corrected_tolerance=TIGHT_TOLERANCE)
        == CalibrationOutcome.WORSE_ROLLED_BACK
    )


def test_calibration_allowed_first_attempt_always_allowed() -> None:
    assert calibration_allowed(None, now_s=1_000.0) is True


def test_calibration_allowed_rate_limited_within_24h() -> None:
    assert calibration_allowed(last_attempt_s=0.0, now_s=3_600.0) is False
    assert calibration_allowed(last_attempt_s=0.0, now_s=86_400.0) is True


def test_is_persistent_drift_requires_min_fraction() -> None:
    assert is_persistent_drift([True] * 9 + [False], min_fraction=0.90) is True
    assert is_persistent_drift([True] * 8 + [False] * 2, min_fraction=0.90) is False
    assert is_persistent_drift([], min_fraction=0.90) is False


@given(st.lists(st.booleans(), max_size=200))
def test_is_persistent_drift_never_exceeds_one_or_below_zero_fraction(observations: list[bool]) -> None:
    # Sanity/robustness property: the function is total over any boolean sequence, never raises.
    result = is_persistent_drift(observations)
    assert isinstance(result, bool)


def test_exceeds_watch_threshold_uses_the_larger_of_variance_and_floor() -> None:
    # Low historical variance: the floor dominates.
    assert exceeds_watch_threshold(deviation=0.03, historical_std=0.001, floor=0.02) is True
    assert exceeds_watch_threshold(deviation=0.01, historical_std=0.001, floor=0.02) is False
    # High historical variance: the 1.5x-variance threshold dominates.
    assert exceeds_watch_threshold(deviation=0.5, historical_std=0.5, floor=0.02) is False
    assert exceeds_watch_threshold(deviation=1.0, historical_std=0.5, floor=0.02) is True
