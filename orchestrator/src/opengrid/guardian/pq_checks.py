"""Guardian PQ checks G-21..G-25 (00-invariants.md K14; 07-delivery/06-service-profiles-and-power-
quality.md S5.3, S5.5.3, S6.7) -- new file, additive to `opengrid.guardian.checks` (WP-C; that module is
not edited here, see this package's README for the exact `GuardianService` wiring).

Every check is a pure function over plain values, reusing `CheckOutcome` from `opengrid.guardian.checks`
(no second verdict shape) and every PQ formula/threshold from `opengrid.core.pq` (no re-implementation of
`evaluate_envelope`, `calibration_allowed`, etc. -- BUILD.md S1's dupcheck rule). `opengrid.guardian.
pq_ports` supplies this module's own independently-read inputs, exactly like `checks.py`/`ports.py`.
"""

from __future__ import annotations

from opengrid.core.pq import (
    CalibrationBounds,
    ComplianceState,
    PqEnvelopeLimits,
    PqMeasurement,
    calibration_allowed,
    evaluate_envelope,
)
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.pq_ports import AssetState, ProposedCalibrationCommand

_BOUNDS_TOL = 1e-9

#: S5.5.3 table: excluded from ALL grid-facing dispatch, for every profile except HOME (which G-24 never
#: gates -- "the homeowner's own reserve and backup service is never gated on grid-facing asset state").
_ALWAYS_EXCLUDED_STATES: frozenset[AssetState] = frozenset(
    {"QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
)

#: S2's `ride_through_class` labels, ranked low-to-high per IEEE 1547-2018 Table 15's Category I/II/III
#: (higher category = broader sag/swell ride-through the inverter must not trip on). Kept local to this
#: module rather than `opengrid.core.pq` -- it is a simple ordinal label map, not PQ math; move it to
#: `core.pq.constants` if another owner (e.g. the allocator's S5.5.3 eligibility filter) needs the same
#: ranking, so there is still exactly one copy (BUILD.md S1).
RIDE_THROUGH_RANK: dict[str, int] = {"CATEGORY_I": 1, "CATEGORY_II": 2, "CATEGORY_III": 3}


def _envelope_verdicts(measurement: PqMeasurement, limits: PqEnvelopeLimits) -> dict[str, ComplianceState]:
    return evaluate_envelope(measurement, limits)


def check_g21_phase_imbalance(
    bank_id: str, measurement: PqMeasurement, limits: PqEnvelopeLimits, *, is_stale: bool = False
) -> CheckOutcome:
    """K14/G-21: per-phase imbalance (S3.2c) against the tightest active `max_phase_imbalance_pct` among
    obligations served behind `bank_id`. VETO (item-level shape, like G-01/G-02/G-04) if the independently
    re-derived aggregate breaches -- reuses `core.pq.evaluate_envelope`, never re-implements the
    imbalance-vs-limit comparison."""
    verdict = _envelope_verdicts(measurement, limits)["imbalance_pct"]
    if verdict != ComplianceState.BREACH:
        return CheckOutcome.passed("G-21", hub_id=bank_id)
    reason = "PQ_PHASE_IMBALANCE_STALE_FALLBACK_EXCEEDED" if is_stale else "PQ_PHASE_IMBALANCE_EXCEEDED"
    return CheckOutcome("G-21", False, reason, bank_id)


def check_g22_thd(
    bank_id: str, measurement: PqMeasurement, limits: PqEnvelopeLimits, *, is_stale: bool = False
) -> CheckOutcome:
    """K14/G-22: vector-summed THD_V/THD_I (S3.2b) against the tightest active `thd_voltage_limit_pct`/
    `thd_current_limit_pct`. `is_stale=True` (S4.a's fallback) means the caller already substituted the
    conservative worst-case-phase-alignment estimate for `measurement`'s THD fields -- this function does
    not know or care which pipeline produced the number, it only re-derives the same PASS/BREACH verdict
    `evaluate_envelope` gives the allocator's own self-check."""
    verdicts = _envelope_verdicts(measurement, limits)
    breached = (
        verdicts["thd_voltage_pct"] == ComplianceState.BREACH
        or verdicts["thd_current_pct"] == ComplianceState.BREACH
    )
    if not breached:
        return CheckOutcome.passed("G-22", hub_id=bank_id)
    reason = "PQ_THD_STALE_FALLBACK_EXCEEDED" if is_stale else "PQ_THD_EXCEEDED"
    return CheckOutcome("G-22", False, reason, bank_id)


def check_g23_freq_voltage_deviation(
    bank_id: str, measurement: PqMeasurement, limits: PqEnvelopeLimits, *, is_stale: bool = False
) -> CheckOutcome:
    """K14/G-23: aggregate frequency/voltage offset (S3.2a) against the tightest active `freq_tolerance_
    hz`/`voltage_band_pct`. Complementary to, not duplicating, the existing grid-frequency-event freeze
    (R26 of `03 S8.6`) -- this checks the inverter fleet's OWN contribution to deviation."""
    verdicts = _envelope_verdicts(measurement, limits)
    breached = (
        verdicts["voltage_deviation_pct"] == ComplianceState.BREACH
        or verdicts["freq_deviation_hz"] == ComplianceState.BREACH
    )
    if not breached:
        return CheckOutcome.passed("G-23", hub_id=bank_id)
    reason = (
        "PQ_FREQ_VOLTAGE_DEVIATION_STALE_FALLBACK_EXCEEDED"
        if is_stale
        else "PQ_FREQ_VOLTAGE_DEVIATION_EXCEEDED"
    )
    return CheckOutcome("G-23", False, reason, bank_id)


def check_g24_asset_conformance(
    hub_id: str,
    *,
    asset_state: AssetState,
    hub_ride_through_class: str,
    envelope_ride_through_class: str,
    pq_sensitive: bool,
    is_home: bool = False,
) -> CheckOutcome:
    """K14/G-24: ride-through conformance (S2/S3.1) plus asset-state eligibility (S5.5.3's table),
    extended per S5.5.3's own note ("independently by guardian check G-24 ... extended to read
    `asset_state`"). `is_home=True` skips the whole check (S5.5.3: HOME is never gated on grid-facing
    asset state). `pq_sensitive=True` marks a `DATA_CENTER`/`PIPELINE_AC`-style non-default-envelope
    obligation, which excludes `DEGRADED` immediately; a grid-code-minimum obligation (e.g.
    `ERCOT_ENERGY`) may still use a `DEGRADED` hub -- that hub's contribution is separately re-verified
    every cycle by G-21..G-23 against the grid-code-minimum envelope, not by this check."""
    if is_home:
        return CheckOutcome.passed("G-24", hub_id=hub_id)
    if asset_state in _ALWAYS_EXCLUDED_STATES:
        return CheckOutcome("G-24", False, f"PQ_ASSET_STATE_{asset_state}_EXCLUDED", hub_id)
    if pq_sensitive and asset_state == "DEGRADED":
        return CheckOutcome("G-24", False, "PQ_ASSET_STATE_DEGRADED_EXCLUDED_PQ_SENSITIVE", hub_id)

    hub_rank = RIDE_THROUGH_RANK.get(hub_ride_through_class)
    envelope_rank = RIDE_THROUGH_RANK.get(envelope_ride_through_class)
    if hub_rank is None or envelope_rank is None:
        return CheckOutcome("G-24", False, "PQ_RIDE_THROUGH_CLASS_UNKNOWN", hub_id)
    if hub_rank < envelope_rank:
        return CheckOutcome("G-24", False, "PQ_RIDE_THROUGH_CLASS_INSUFFICIENT", hub_id)
    return CheckOutcome.passed("G-24", hub_id=hub_id)


def _bounds_exceeded(bounds: CalibrationBounds, firmware_max: CalibrationBounds) -> bool:
    return (
        bounds.max_freq_hz > firmware_max.max_freq_hz + _BOUNDS_TOL
        or bounds.max_voltage_pct > firmware_max.max_voltage_pct + _BOUNDS_TOL
        or bounds.max_phase_deg > firmware_max.max_phase_deg + _BOUNDS_TOL
    )


def _correction_exceeds_bounds(command: ProposedCalibrationCommand) -> bool:
    correction, bounds = command.correction, command.bounds
    return (
        abs(correction.freq_hz) > bounds.max_freq_hz + _BOUNDS_TOL
        or abs(correction.voltage_pct) > bounds.max_voltage_pct + _BOUNDS_TOL
        or abs(correction.phase_deg) > bounds.max_phase_deg + _BOUNDS_TOL
    )


def check_g25_calibration_safety(
    command: ProposedCalibrationCommand,
    *,
    firmware_max_bounds: CalibrationBounds,
    last_attempt_epoch_s: float | None,
    now_epoch_s: float,
    min_interval_s: float,
    hub_has_active_sensitive_grant: bool,
) -> CheckOutcome:
    """K14/G-25 (S6.7): refuse to sign a `CalibrationCommand` unless (i) its own `bounds` are within the
    inverter model/firmware family's configured maximum (defense in depth against a bad candidate) AND
    the correction magnitudes are within `bounds`, (ii) the per-hub rate limit is not exceeded
    (`core.pq.calibration_allowed`, S5.5.4 default 1/24h), and (iii) the ledger shows no active committed
    grant for a non-default-envelope obligation on the target hub (S5.4 step 2 must have already
    substituted any PQ-sensitive delivery off it). Fail-safe: refuse to sign (hold) -- the ladder's
    recalibration step is skipped and drift proceeds toward escalation on its own timeline (S6.7), never
    forced through."""
    if hub_has_active_sensitive_grant:
        return CheckOutcome("G-25", False, "PQ_CALIBRATION_ACTIVE_SENSITIVE_GRANT", command.hub_id)
    if not calibration_allowed(last_attempt_epoch_s, now_epoch_s, min_interval_s=min_interval_s):
        return CheckOutcome("G-25", False, "PQ_CALIBRATION_RATE_LIMIT_EXCEEDED", command.hub_id)
    if _bounds_exceeded(command.bounds, firmware_max_bounds):
        return CheckOutcome("G-25", False, "PQ_CALIBRATION_BOUNDS_EXCEED_FIRMWARE_LIMIT", command.hub_id)
    if _correction_exceeds_bounds(command):
        return CheckOutcome("G-25", False, "PQ_CALIBRATION_CORRECTION_EXCEEDS_BOUNDS", command.hub_id)
    return CheckOutcome.passed("G-25", hub_id=command.hub_id)
