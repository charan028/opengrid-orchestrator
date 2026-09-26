"""FFT-based waveform analysis: RMS, fundamental frequency, harmonic phasors, THD, phase angle and
per-phase unbalance (07-delivery/06 S3.1, S6.1, S6.4(a), S6.5 step 1).

Pure functions operating on already-sampled arrays (a hub's raw waveform capture, S6.4(a)) -- no I/O,
no MQTT/DB access. Edge devices compute their own summary in the field; these are the SAME formulas
used to (a) independently recompute a summary in the background audit job (S6.5 step 2) and
(b) analyze synthetic/simulated waveforms in tests.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

from opengrid.core.pq.constants import (
    DEFAULT_FREQ_SEARCH_BAND_HZ,
    HARMONIC_ORDER_MAX,
    HARMONIC_ORDER_MIN,
    NOMINAL_FREQ_HZ,
)
from opengrid.core.pq.types import HarmonicSpectrum, WaveformAnalysis

FloatArray = npt.NDArray[np.float64]


def rms(samples: FloatArray) -> float:
    """Root-mean-square of a sampled waveform (V or A), S6.1's `v_rms`/`i_rms`."""
    if samples.size == 0:
        raise ValueError("rms() requires at least one sample")
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def estimate_fundamental_hz(
    samples: FloatArray,
    sample_rate_hz: float,
    *,
    nominal_hz: float = NOMINAL_FREQ_HZ,
    search_band_hz: float = DEFAULT_FREQ_SEARCH_BAND_HZ,
) -> float:
    """Estimate the fundamental frequency (Hz) near `nominal_hz` from an FFT magnitude peak, refined
    by quadratic (parabolic) interpolation between the peak bin and its neighbors -- S6.1's `freq_hz`.
    A Hann window is applied only for THIS peak search (not for the harmonic phasors below) to reduce
    spectral leakage when locating the peak.
    """
    n = samples.size
    if n < 4:
        raise ValueError("estimate_fundamental_hz() requires at least 4 samples")
    windowed = samples * np.hanning(n)
    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    band = (freqs >= nominal_hz - search_band_hz) & (freqs <= nominal_hz + search_band_hz)
    candidates = np.nonzero(band)[0]
    if candidates.size == 0:
        raise ValueError("no FFT bins found within the search band around nominal_hz")
    magnitudes = np.abs(spectrum)
    peak_idx = int(candidates[np.argmax(magnitudes[candidates])])
    if 0 < peak_idx < magnitudes.size - 1:
        alpha, beta, gamma = (
            float(magnitudes[peak_idx - 1]),
            float(magnitudes[peak_idx]),
            float(magnitudes[peak_idx + 1]),
        )
        denom = alpha - 2.0 * beta + gamma
        offset = 0.5 * (alpha - gamma) / denom if denom != 0.0 else 0.0
        bin_hz = sample_rate_hz / n
        return float(freqs[peak_idx] + offset * bin_hz)
    return float(freqs[peak_idx])


def _phasor_at_frequency(samples: FloatArray, sample_rate_hz: float, freq_hz: float) -> complex:
    """Single-frequency Fourier coefficient (a Goertzel-equivalent direct DFT term) at an arbitrary,
    possibly non-FFT-grid frequency -- needed because a real inverter's fundamental sits at
    `nominal_hz + freq_offset_hz`, off the FFT bin grid for a finite capture window. Exact (up to
    numerical precision) when the window spans an integer number of cycles of `freq_hz`, which S6.4(a)'s
    10-cycle capture window guarantees for the fundamental and every one of its integer harmonics.
    """
    n = samples.size
    t = np.arange(n, dtype=np.float64) / sample_rate_hz
    basis = np.exp(-2j * np.pi * freq_hz * t)
    return complex(2.0 / n * np.sum(samples * basis))


def harmonic_spectrum(
    samples: FloatArray,
    sample_rate_hz: float,
    fundamental_hz: float,
    *,
    min_order: int = HARMONIC_ORDER_MIN,
    max_order: int = HARMONIC_ORDER_MAX,
) -> HarmonicSpectrum:
    """Fundamental phasor and harmonic phasors for orders `min_order..max_order` (S6.4(a): orders
    2-50), each a complex phasor in the sampled waveform's own unit; angle is relative to the t=0
    sample (callers align t=0 to the sync reference for S6.4(a)'s `phase_angle_deg`)."""
    fundamental = _phasor_at_frequency(samples, sample_rate_hz, fundamental_hz)
    harmonics = {
        order: _phasor_at_frequency(samples, sample_rate_hz, fundamental_hz * order)
        for order in range(min_order, max_order + 1)
    }
    return HarmonicSpectrum(fundamental=fundamental, harmonics=harmonics)


def thd_pct(spectrum: HarmonicSpectrum) -> float:
    """Total harmonic distortion, % of the fundamental (IEEE 519 style):
    sqrt(sum(|I_k|^2 for k in harmonics)) / |I_1| * 100 -- S6.1's `thd_v_pct`/`thd_i_pct`."""
    fundamental_mag = abs(spectrum.fundamental)
    if fundamental_mag <= 0.0:
        raise ValueError("thd_pct() requires a nonzero fundamental")
    sum_sq = sum(abs(phasor) ** 2 for phasor in spectrum.harmonics.values())
    return float(np.sqrt(sum_sq) / fundamental_mag * 100.0)


def analyze_waveform(
    samples: FloatArray,
    sample_rate_hz: float,
    *,
    nominal_hz: float = NOMINAL_FREQ_HZ,
    search_band_hz: float = DEFAULT_FREQ_SEARCH_BAND_HZ,
    min_order: int = HARMONIC_ORDER_MIN,
    max_order: int = HARMONIC_ORDER_MAX,
) -> WaveformAnalysis:
    """One-shot edge-summary computation: RMS, fundamental frequency, harmonic spectrum and THD from
    a raw waveform capture (S6.4(a)'s edge computation; reused unchanged by the background audit job,
    S6.5 step 2, to independently recompute the same summary from the same raw samples)."""
    fundamental_hz = estimate_fundamental_hz(
        samples, sample_rate_hz, nominal_hz=nominal_hz, search_band_hz=search_band_hz
    )
    spectrum = harmonic_spectrum(
        samples, sample_rate_hz, fundamental_hz, min_order=min_order, max_order=max_order
    )
    return WaveformAnalysis(
        rms=rms(samples),
        fundamental_hz=fundamental_hz,
        thd_pct=thd_pct(spectrum),
        spectrum=spectrum,
    )


def phase_angle_between_deg(phasor_a: complex, phasor_b: complex) -> float:
    """Angle (deg, in (-180, 180]) of `phasor_a` relative to `phasor_b` -- S3.1's cross-inverter phase
    comparison and S6.4(a)'s `phase_angle_deg` relative to a reference phasor. Positive means `b` lags
    `a` by that many degrees."""
    delta = float(np.angle(phasor_a, deg=True) - np.angle(phasor_b, deg=True))
    return (delta + 180.0) % 360.0 - 180.0


def phase_unbalance_pct(phase_values: Mapping[str, float]) -> float:
    """NEMA MG-1 / IEEE 1159 style current or voltage imbalance: 100 * max_phi(|X_phi - mean|) / mean
    (S2's `max_phase_imbalance_pct` definition; S3.2(c) applies this to per-phase currents)."""
    if not phase_values:
        raise ValueError("phase_unbalance_pct() requires at least one phase")
    values = np.array(list(phase_values.values()), dtype=np.float64)
    mean = float(np.mean(values))
    if mean <= 0.0:
        return 0.0
    max_dev = float(np.max(np.abs(values - mean)))
    return 100.0 * max_dev / mean
