from __future__ import annotations

import math

import numpy as np
import pytest

from opengrid.core.pq.waveform import (
    analyze_waveform,
    estimate_fundamental_hz,
    harmonic_spectrum,
    phase_angle_between_deg,
    phase_unbalance_pct,
    rms,
    thd_pct,
)

SAMPLE_RATE_HZ = 7_680.0  # 128 samples/cycle at 60 Hz, S6.4(a)
CAPTURE_CYCLES = 10
N_SAMPLES = 1_280  # 128 * 10


def _time_axis(n: int = N_SAMPLES, sample_rate_hz: float = SAMPLE_RATE_HZ) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / sample_rate_hz


def test_rms_of_pure_sine_is_amplitude_over_sqrt2() -> None:
    t = _time_axis()
    samples = 100.0 * np.cos(2 * np.pi * 60.0 * t)
    assert rms(samples) == pytest.approx(100.0 / math.sqrt(2), rel=1e-3)


def test_rms_rejects_empty_array() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        rms(np.array([], dtype=np.float64))


def test_estimate_fundamental_hz_recovers_offset_frequency() -> None:
    t = _time_axis()
    true_freq = 60.03  # a realistic PLL bias, S3.1's freq_offset_hz
    samples = 100.0 * np.cos(2 * np.pi * true_freq * t)
    estimated = estimate_fundamental_hz(samples, SAMPLE_RATE_HZ)
    assert estimated == pytest.approx(true_freq, abs=0.1)


def test_harmonic_spectrum_and_thd_recover_known_distortion() -> None:
    t = _time_axis()
    f0 = 60.0
    amp = 100.0
    h3_pct, h5_pct = 5.0, 3.0
    samples = (
        amp * np.cos(2 * np.pi * f0 * t)
        + amp * (h3_pct / 100.0) * np.cos(2 * np.pi * 3 * f0 * t + math.radians(40))
        + amp * (h5_pct / 100.0) * np.cos(2 * np.pi * 5 * f0 * t + math.radians(-20))
    )
    spectrum = harmonic_spectrum(samples, SAMPLE_RATE_HZ, f0)
    assert abs(spectrum.fundamental) == pytest.approx(amp, rel=1e-3)
    assert abs(spectrum.harmonics[3]) == pytest.approx(amp * h3_pct / 100.0, rel=1e-2)
    assert abs(spectrum.harmonics[5]) == pytest.approx(amp * h5_pct / 100.0, rel=1e-2)
    expected_thd = math.sqrt(h3_pct**2 + h5_pct**2)
    assert thd_pct(spectrum) == pytest.approx(expected_thd, rel=0.02)


def test_thd_pct_rejects_zero_fundamental() -> None:
    spectrum = harmonic_spectrum(np.zeros(N_SAMPLES), SAMPLE_RATE_HZ, 60.0)
    with pytest.raises(ValueError, match="nonzero fundamental"):
        thd_pct(spectrum)


def test_analyze_waveform_recovers_frequency_thd_and_rms_together() -> None:
    t = _time_axis()
    f0 = 59.97
    amp = 100.0
    samples = amp * np.cos(2 * np.pi * f0 * t) + amp * 0.04 * np.cos(2 * np.pi * 3 * f0 * t)
    analysis = analyze_waveform(samples, SAMPLE_RATE_HZ)
    assert analysis.fundamental_hz == pytest.approx(f0, abs=0.1)
    assert analysis.thd_pct == pytest.approx(4.0, rel=0.03)
    assert analysis.rms == pytest.approx(amp / math.sqrt(2), rel=0.02)


def test_phase_angle_between_deg_recovers_known_lag() -> None:
    t = _time_axis()
    f0 = 60.0
    lag_deg = 90.0
    phasor_a = harmonic_spectrum(np.cos(2 * np.pi * f0 * t), SAMPLE_RATE_HZ, f0).fundamental
    phasor_b = harmonic_spectrum(
        np.cos(2 * np.pi * f0 * t - math.radians(lag_deg)), SAMPLE_RATE_HZ, f0
    ).fundamental
    assert phase_angle_between_deg(phasor_a, phasor_b) == pytest.approx(lag_deg, abs=0.5)


def test_phase_unbalance_pct_zero_when_balanced() -> None:
    assert phase_unbalance_pct({"A": 10.0, "B": 10.0, "C": 10.0}) == pytest.approx(0.0, abs=1e-9)


def test_phase_unbalance_pct_matches_hand_computation() -> None:
    # mean = 10, max deviation = |13 - 10| = 3 -> 30%
    assert phase_unbalance_pct({"A": 13.0, "B": 9.0, "C": 8.0}) == pytest.approx(30.0, rel=1e-9)


def test_phase_unbalance_pct_zero_mean_is_zero_not_a_crash() -> None:
    assert phase_unbalance_pct({"A": 0.0, "B": 0.0, "C": 0.0}) == 0.0
