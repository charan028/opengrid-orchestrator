"""opengrid.core.pq -- power-quality math (07-delivery/06-service-profiles-and-power-quality.md S3,
S5, S6.4-S6.5): waveform analysis, cross-inverter aggregation, envelope compliance/hysteresis and
remote-calibration math. Pure functions, no I/O (BUILD.md S1) -- the allocator's pre-delivery
planning cross-check and post-assignment self-check, the guardian's independent G-21..G-25, the
waveform-ingestion service's background audit job, and the `ogsim` test harness all call these
instead of re-deriving any formula (single implementation per 02b S12's rule, enforced by
`orchestrator/tools/dupcheck.py`).
"""

from __future__ import annotations

from opengrid.core.pq.aggregation import (
    aggregate_kva_circle,
    aggregate_offset_std,
    bank_thd_current_pct,
    harmonic_vector_sum,
    inverter_quality_score,
    kva_point_feasible,
    per_phase_imbalance_pct,
)
from opengrid.core.pq.calibration import (
    apply_correction,
    calibration_allowed,
    classify_calibration_outcome,
    compute_correction,
    exceeds_watch_threshold,
    is_persistent_drift,
)
from opengrid.core.pq.constants import DEFAULT_FIRMWARE_CALIBRATION_BOUNDS, RIDE_THROUGH_RANK
from opengrid.core.pq.envelope import (
    compliance_ratios,
    evaluate_envelope,
    step_hysteresis,
    worst_verdict,
)
from opengrid.core.pq.types import (
    CalibrationBounds,
    CalibrationOutcome,
    ComplianceState,
    HarmonicSpectrum,
    HysteresisConfig,
    HysteresisState,
    KvaCircle,
    OffsetVector,
    PqEnvelopeLimits,
    PqMeasurement,
    WaveformAnalysis,
)
from opengrid.core.pq.waveform import (
    analyze_waveform,
    estimate_fundamental_hz,
    harmonic_spectrum,
    phase_angle_between_deg,
    phase_unbalance_pct,
    rms,
    thd_pct,
)

__all__ = [
    "DEFAULT_FIRMWARE_CALIBRATION_BOUNDS",
    "RIDE_THROUGH_RANK",
    "CalibrationBounds",
    "CalibrationOutcome",
    "ComplianceState",
    "HarmonicSpectrum",
    "HysteresisConfig",
    "HysteresisState",
    "KvaCircle",
    "OffsetVector",
    "PqEnvelopeLimits",
    "PqMeasurement",
    "WaveformAnalysis",
    "aggregate_kva_circle",
    "aggregate_offset_std",
    "analyze_waveform",
    "apply_correction",
    "bank_thd_current_pct",
    "calibration_allowed",
    "classify_calibration_outcome",
    "compliance_ratios",
    "compute_correction",
    "estimate_fundamental_hz",
    "evaluate_envelope",
    "exceeds_watch_threshold",
    "harmonic_spectrum",
    "harmonic_vector_sum",
    "inverter_quality_score",
    "is_persistent_drift",
    "kva_point_feasible",
    "per_phase_imbalance_pct",
    "phase_angle_between_deg",
    "phase_unbalance_pct",
    "rms",
    "step_hysteresis",
    "thd_pct",
    "worst_verdict",
]
