"""Named PQ constants with units (07-delivery/06 S3.1, S5.1, S5.4, S5.5, S6.4(a)).

No magic numbers elsewhere in `opengrid.core.pq` (BUILD.md S5a) -- every threshold and default used by
more than one function, or that a caller may reasonably want to override, is named here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from opengrid.core.pq.types import CalibrationBounds

# --- S6.4(a) waveform capture parameters -----------------------------------------------------------
NOMINAL_FREQ_HZ = 60.0
SAMPLES_PER_CYCLE = 128
CAPTURE_CYCLES = 10
HARMONIC_ORDER_MIN = 2
HARMONIC_ORDER_MAX = 50
DEFAULT_FREQ_SEARCH_BAND_HZ = 5.0  # +/- Hz around nominal_hz searched for the fundamental FFT peak

# --- S5.1 inverter quality score reference scales (denominators, not physical limits) ---------------
QUALITY_SCORE_FREQ_REF_HZ = 0.05
QUALITY_SCORE_VOLTAGE_REF_PCT = 2.0
QUALITY_SCORE_THD_REF_PCT = 5.0
QUALITY_SCORE_PHASE_REF_DEG = 10.0
QUALITY_SCORE_EQUAL_WEIGHT = 0.25

# --- S5.2 eligibility filter default ------------------------------------------------------------
ELIGIBILITY_K_SIGMA_DEFAULT = 2.0

# --- S5.4 continuous-monitoring hysteresis defaults --------------------------------------------------
WARN_RATIO_DEFAULT = 0.70
BREACH_RATIO_DEFAULT = 1.0
RECOVERY_RATIO_DEFAULT = 0.60
WARN_DWELL_S_DEFAULT = 60.0
BREACH_DWELL_S_DEFAULT = 60.0
RECOVERY_DWELL_S_DEFAULT = 60.0

# --- S5.5 asset-health / calibration defaults --------------------------------------------------------
CALIBRATION_MIN_INTERVAL_S_DEFAULT = 86_400.0  # at most 1 attempt / hub / rolling 24h
DRIFT_OBSERVATION_WINDOW_S_DEFAULT = 15 * 60.0  # rolling 15-minute window
DRIFT_MIN_PERSISTENT_FRACTION_DEFAULT = 0.90  # present in >= 90% of summaries over the window
DRIFT_VARIANCE_MULTIPLIER_DEFAULT = 1.5  # 1.5x the unit's own historical variance
DRIFT_FLOOR_FREQ_HZ = 0.02
DRIFT_FLOOR_VOLTAGE_PCT = 1.0
DRIFT_FLOOR_THD_PCT = 1.5
DRIFT_FLOOR_PHASE_DEG = 3.0
CALIBRATION_RECURRENCE_WINDOW_DAYS_DEFAULT = 14  # recurrence within this many days re-escalates
CALIBRATION_OUTCOME_REL_TOL_DEFAULT = 0.05  # NO_CHANGE vs IMPROVED/WORSE_ROLLED_BACK dead zone

# --- S6.7 firmware-family maximum calibration correction (G-25 defense in depth) --------------------
# One fixed, conservative bound for every inverter until a per-firmware-family bounds table exists. The
# ladder (`opengrid.assets`) builds candidates within it; the guardian's G-25 re-checks against it.
CALIBRATION_MAX_FREQ_HZ_DEFAULT = 0.10  # Hz: beyond this the reference, not the inverter, is suspect
CALIBRATION_MAX_VOLTAGE_PCT_DEFAULT = 2.0  # % of nominal: tightest typical data-center voltage band
CALIBRATION_MAX_PHASE_DEG_DEFAULT = 5.0  # deg: above the 3 deg WATCH floor, still a trim
DEFAULT_FIRMWARE_CALIBRATION_BOUNDS: Final = CalibrationBounds(
    max_freq_hz=CALIBRATION_MAX_FREQ_HZ_DEFAULT,
    max_voltage_pct=CALIBRATION_MAX_VOLTAGE_PCT_DEFAULT,
    max_phase_deg=CALIBRATION_MAX_PHASE_DEG_DEFAULT,
)

# --- S2 / S5.5.3 ride-through classes -------------------------------------------------------------
#: `ride_through_class` labels ranked low-to-high per IEEE 1547-2018 Table 15 (a higher category rides
#: through a broader sag/swell). One copy, used by the allocator's eligibility filter and guardian G-24.
RIDE_THROUGH_RANK: Final = MappingProxyType({"CATEGORY_I": 1, "CATEGORY_II": 2, "CATEGORY_III": 3})
