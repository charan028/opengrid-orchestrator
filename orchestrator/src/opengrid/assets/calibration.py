"""Remote-calibration command builder (07-delivery/06-service-profiles-and-power-quality.md S5.5.4,
S6.7). Builds the UNSIGNED candidate content of a `CalibrationCommand` -- the guardian's `check_g25_
calibration_safety` (`opengrid.guardian.pq_checks`) is the independent check that gates signing it
(S6.7); this module never signs anything itself (BUILD.md S4's sole-signer rule, K3).

All correction/bound math is `opengrid.core.pq.calibration` -- this module only assembles the wire-shaped
candidate and calibration_id/epoch/seq/lease bookkeeping around it (no duplicated clipping logic, S6.7's
"there is no second implementation of 'clip to bounds'").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from opengrid.core.pq import CalibrationBounds, OffsetVector, calibration_allowed, compute_correction


@dataclass(frozen=True, slots=True)
class CalibrationReference:
    """S6.7's `CalibrationCommand.reference`: the grid-synchronized reference the correction is computed
    against (S5.5.4's "reference phase angle, reference frequency, nominal amplitude")."""

    phase_deg: float
    freq_hz: float
    amplitude_v: float
    sync_source: str  # "ptp" | "gps" | "ntp_disciplined", per calibration_command.schema.json


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    """The unsigned content of a `CalibrationCommand` (`opengrid.core.models.pq.CalibrationCommand`
    minus `key_id`/`signature`) plus the `ProposedCalibrationCommand` guardian's G-25 needs -- callers
    hand this to the guardian for signing (mirrors the engine -> guardian `ProposedBatch` hand-off)."""

    calibration_id: UUID
    hub_id: str
    epoch: int | None
    """`None` until the guardian assigns the real per-hub (epoch, seq) on `og.calibration_command`
    (migration 0016) when it signs; the ladder never invents one (#30)."""
    seq: int | None
    issued_at: datetime
    expires_at: datetime
    reference: CalibrationReference
    correction: OffsetVector
    bounds: CalibrationBounds


def build_calibration_candidate(
    *,
    hub_id: str,
    measured_offset: OffsetVector,
    bounds: CalibrationBounds,
    reference: CalibrationReference,
    issued_at: datetime,
    lease_ttl_s: float,
    calibration_id: UUID | None = None,
    epoch: int | None = None,
    seq: int | None = None,
) -> CalibrationCandidate:
    """S5.5.4: the correction is the negative of the measured offset, clipped to `bounds` (`opengrid.
    core.pq.compute_correction` -- the SAME clipping G-25 re-checks defense-in-depth). `lease_ttl_s`
    mirrors the command-batch lease pattern (K6): a calibration command not applied before `expires_at`
    is stale and must be rejected/expired by the hub, never held open indefinitely."""
    correction = compute_correction(measured_offset, bounds)
    return CalibrationCandidate(
        calibration_id=calibration_id or uuid4(),
        hub_id=hub_id,
        epoch=epoch,
        seq=seq,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=lease_ttl_s),
        reference=reference,
        correction=correction,
        bounds=bounds,
    )


def calibration_due(last_attempt_epoch_s: float | None, now_epoch_s: float, min_interval_s: float) -> bool:
    """S5.5.4 rate limiting, re-exported at the module the ladder calls (the primary check; G-25 is the
    independent re-check of the exact same `opengrid.core.pq.calibration_allowed` function -- one
    implementation, two independent callers, per the K2/K14 "primary + independent" pattern)."""
    return calibration_allowed(last_attempt_epoch_s, now_epoch_s, min_interval_s=min_interval_s)
