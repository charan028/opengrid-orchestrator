"""Remote calibration math (07-delivery/06 S5.5, S6.7): bounded correction commands, drift
persistence/observation-window logic, calibration-outcome classification and rate limiting.

Pure functions -- the guardian's G-25 re-checks the SAME bound-clipping this module performs
(defense in depth, S6.7); there is no second implementation of "clip to bounds."
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from opengrid.core.pq.constants import (
    CALIBRATION_MIN_INTERVAL_S_DEFAULT,
    CALIBRATION_OUTCOME_REL_TOL_DEFAULT,
    DRIFT_MIN_PERSISTENT_FRACTION_DEFAULT,
    DRIFT_VARIANCE_MULTIPLIER_DEFAULT,
)
from opengrid.core.pq.types import CalibrationBounds, CalibrationOutcome, OffsetVector


def _clip(value: float, bound: float) -> float:
    if bound < 0.0:
        raise ValueError("a calibration bound must be >= 0")
    return max(-bound, min(bound, value))


def compute_correction(measured: OffsetVector, bounds: CalibrationBounds) -> OffsetVector:
    """S6.7: the correction command is the negative of the measured offset from the grid reference,
    clipped component-wise to `bounds` -- a correction is never unbounded (S5.5.4; G-25 re-checks the
    same bounds independently at signing time, defense in depth)."""
    return OffsetVector(
        freq_hz=_clip(-measured.freq_hz, bounds.max_freq_hz),
        voltage_pct=_clip(-measured.voltage_pct, bounds.max_voltage_pct),
        phase_deg=_clip(-measured.phase_deg, bounds.max_phase_deg),
    )


def apply_correction(measured: OffsetVector, correction: OffsetVector) -> OffsetVector:
    """The residual offset expected once a hub applies `correction` against `measured` -- used both to
    predict the post-calibration offset and, combined with real post-calibration measurements, to
    classify the outcome via `classify_calibration_outcome`."""
    return OffsetVector(
        freq_hz=measured.freq_hz + correction.freq_hz,
        voltage_pct=measured.voltage_pct + correction.voltage_pct,
        phase_deg=measured.phase_deg + correction.phase_deg,
    )


def _magnitude(offset: OffsetVector) -> float:
    return math.sqrt(offset.freq_hz**2 + offset.voltage_pct**2 + offset.phase_deg**2)


def classify_calibration_outcome(
    pre: OffsetVector,
    post: OffsetVector,
    *,
    corrected_tolerance: OffsetVector,
    no_change_rel_tol: float = CALIBRATION_OUTCOME_REL_TOL_DEFAULT,
) -> CalibrationOutcome:
    """S5.5.4: `CORRECTED` (post-calibration offset within `corrected_tolerance` on every axis),
    `WORSE_ROLLED_BACK` (post magnitude materially larger than pre -- triggers the automatic rollback),
    `IMPROVED` (materially smaller but not yet within tolerance -- stays `WATCH`), else `NO_CHANGE`."""
    if (
        abs(post.freq_hz) <= corrected_tolerance.freq_hz
        and abs(post.voltage_pct) <= corrected_tolerance.voltage_pct
        and abs(post.phase_deg) <= corrected_tolerance.phase_deg
    ):
        return CalibrationOutcome.CORRECTED
    pre_mag, post_mag = _magnitude(pre), _magnitude(post)
    if post_mag > pre_mag * (1.0 + no_change_rel_tol):
        return CalibrationOutcome.WORSE_ROLLED_BACK
    if post_mag < pre_mag * (1.0 - no_change_rel_tol):
        return CalibrationOutcome.IMPROVED
    return CalibrationOutcome.NO_CHANGE


def calibration_allowed(
    last_attempt_s: float | None,
    now_s: float,
    *,
    min_interval_s: float = CALIBRATION_MIN_INTERVAL_S_DEFAULT,
) -> bool:
    """S5.5.4 rate limiting: at most one calibration attempt per hub per rolling window (default 24h),
    to avoid chasing noise or interacting badly with the unit's own control loop."""
    if last_attempt_s is None:
        return True
    return now_s - last_attempt_s >= min_interval_s


def is_persistent_drift(
    observations: Sequence[bool],
    *,
    min_fraction: float = DRIFT_MIN_PERSISTENT_FRACTION_DEFAULT,
) -> bool:
    """S5.5.1: a candidate drift is flagged only once the deviation is present in at least
    `min_fraction` (default 90%) of summaries over the rolling observation window -- a single noisy
    sample or a fleet-wide transient (a grid event the whole bank rides through together) must never
    trip this on its own."""
    if not observations:
        return False
    return (sum(1 for observed in observations if observed) / len(observations)) >= min_fraction


def exceeds_watch_threshold(
    deviation: float,
    historical_std: float,
    floor: float,
    *,
    variance_multiplier: float = DRIFT_VARIANCE_MULTIPLIER_DEFAULT,
) -> bool:
    """S5.5.1: a WATCH-worthy drift exceeds `variance_multiplier`x (default 1.5x) the unit's own
    historical variance OR an absolute floor, whichever is larger -- never flags ordinary noise, even
    for a very stable (low-variance) unit, below the floor."""
    threshold = max(variance_multiplier * historical_std, floor)
    return abs(deviation) >= threshold
