"""Local pure-Python types for `opengrid.core.pq` (07-delivery/06 S3, S5, S6.4, S6.7).

These are typed dataclasses/enums, not pydantic models -- WP-A owns the pydantic wire/row models in
`opengrid.core.models`. Mapping for WP-A (so its models can convert to/from these without a second
implementation of the same shape):

- `PqEnvelopeLimits`            <-> the numeric-limit subset of the `og.pq_envelope` row (S1.5, S2)
- `HarmonicSpectrum`/`WaveformAnalysis` <-> a hub's edge-computed `pq`/`harmonics` summary fields
                                            (S6.1, S6.4(a)); `og.pq_waveform_summary`
- `PqMeasurement`                <-> a bank/phase aggregate reading fed to the monitoring loop
                                     (S5.4, S6.5 step 4)
- `KvaCircle`                    <-> the aggregate P/Q capability circle used at admission (S3.2(d))
- `OffsetVector`/`CalibrationBounds` <-> `CalibrationCommand.reference`/`.correction`/`.bounds` (S6.7)
- `ComplianceState`/`HysteresisState` <-> the per-dimension WARN/BREACH state kept per contract (S5.4)
- `CalibrationOutcome`           <-> `calibration_attempt.outcome` (S5.5.4)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ComplianceState(StrEnum):
    """K14/G-21..G-23 per-dimension verdict, and the S5.4 monitoring-loop state (07-delivery/06)."""

    NOMINAL = "NOMINAL"
    WARN = "WARN"
    BREACH = "BREACH"


class CalibrationOutcome(StrEnum):
    """S5.5.4's remote-calibration verification outcome."""

    CORRECTED = "CORRECTED"
    IMPROVED = "IMPROVED"
    NO_CHANGE = "NO_CHANGE"
    WORSE_ROLLED_BACK = "WORSE_ROLLED_BACK"


@dataclass(frozen=True, slots=True)
class HarmonicSpectrum:
    """Fundamental phasor plus harmonic-order phasors (S6.4(a)'s harmonics block before quantization
    to int16 magnitude/angle pairs), in the sampled waveform's own unit (V or A)."""

    fundamental: complex
    harmonics: dict[int, complex]


@dataclass(frozen=True, slots=True)
class WaveformAnalysis:
    """One hub/phase edge-summary computation (S6.1's `pq` object, S6.4(a), S6.5 step 1)."""

    rms: float
    fundamental_hz: float
    thd_pct: float
    spectrum: HarmonicSpectrum


@dataclass(frozen=True, slots=True)
class PqEnvelopeLimits:
    """The numeric-limit subset of `og.pq_envelope` (S1.5/S2) consumed by the compliance checks."""

    max_phase_imbalance_pct: float
    voltage_band_pct: float
    freq_tolerance_hz: float
    pf_min: float
    thd_voltage_limit_pct: float
    thd_current_limit_pct: float
    current_limit_a: float | None = None


@dataclass(frozen=True, slots=True)
class PqMeasurement:
    """One aggregated reading fed to `evaluate_envelope` (S5.4/S6.5 step 4)."""

    imbalance_pct: float
    voltage_deviation_pct: float
    freq_deviation_hz: float
    pf: float
    thd_voltage_pct: float
    thd_current_pct: float
    current_a: float | None = None


@dataclass(frozen=True, slots=True)
class KvaCircle:
    """Aggregate P/Q capability circle (S3.2(d)): total kVA rating and the tightest member PF limits
    in each direction."""

    kva_rating: float
    pf_min_lagging: float
    pf_min_leading: float


@dataclass(frozen=True, slots=True)
class OffsetVector:
    """A freq/voltage/phase offset OR correction -- S6.7's `reference`/`correction`/`bounds` shape."""

    freq_hz: float
    voltage_pct: float
    phase_deg: float


@dataclass(frozen=True, slots=True)
class CalibrationBounds:
    """S6.7's per-inverter-model/firmware-family maximum correction magnitudes (never unbounded)."""

    max_freq_hz: float
    max_voltage_pct: float
    max_phase_deg: float


@dataclass(frozen=True, slots=True)
class HysteresisConfig:
    """S5.4's WARN/BREACH/recovery ratios and dwell windows. Ratios are fractions of the envelope
    limit; dwell windows are seconds. Defaults match S5.4's own defaults (70%/100%/60%, 60 s dwell)."""

    warn_ratio: float = 0.70
    breach_ratio: float = 1.0
    recovery_ratio: float = 0.60
    warn_dwell_s: float = 60.0
    breach_dwell_s: float = 60.0
    recovery_dwell_s: float = 60.0


@dataclass(frozen=True, slots=True)
class HysteresisState:
    """S5.4's per-dimension monitoring state: the committed `verdict`, plus a `candidate` verdict
    being dwelled toward (and the clock reading it started at), so a single noisy sample can never
    flip the committed verdict (chatter prevention, mirroring the $5/MWh price-response dwell)."""

    verdict: ComplianceState = ComplianceState.NOMINAL
    candidate: ComplianceState | None = None
    candidate_since_s: float | None = None
