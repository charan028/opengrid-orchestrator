"""S5.5.4/S6.7 calibration-candidate builder -- correction clipping (delegated to `opengrid.core.pq`,
never re-implemented) and lease/rate-limit bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from opengrid.assets.calibration import (
    CalibrationReference,
    build_calibration_candidate,
    calibration_due,
)
from opengrid.core.pq import CalibrationBounds, OffsetVector

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
REFERENCE = CalibrationReference(phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ptp")
BOUNDS = CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.0)


def test_build_candidate_clips_correction_to_bounds():
    measured = OffsetVector(freq_hz=0.2, voltage_pct=-2.0, phase_deg=5.0)
    candidate = build_calibration_candidate(
        hub_id="hub-1",
        measured_offset=measured,
        bounds=BOUNDS,
        reference=REFERENCE,
        epoch=1,
        seq=1,
        issued_at=NOW,
        lease_ttl_s=30.0,
    )
    assert candidate.correction.freq_hz == -0.05  # clipped, negative of measured, within max_freq_hz
    assert candidate.correction.voltage_pct == 1.0  # clipped, negative of -2.0 clipped to +1.0
    assert candidate.correction.phase_deg == -2.0  # clipped, negative of 5.0 clipped to -2.0
    assert candidate.expires_at == NOW + timedelta(seconds=30.0)
    assert candidate.hub_id == "hub-1"
    assert isinstance(candidate.calibration_id, UUID)


def test_build_candidate_uses_provided_calibration_id():
    fixed_id = UUID("00000000-0000-0000-0000-000000000001")
    candidate = build_calibration_candidate(
        hub_id="hub-1",
        measured_offset=OffsetVector(freq_hz=0.0, voltage_pct=0.0, phase_deg=0.0),
        bounds=BOUNDS,
        reference=REFERENCE,
        epoch=1,
        seq=1,
        issued_at=NOW,
        lease_ttl_s=30.0,
        calibration_id=fixed_id,
    )
    assert candidate.calibration_id == fixed_id


def test_calibration_due_respects_rate_limit():
    now_s = NOW.timestamp()
    assert calibration_due(None, now_s, 86_400.0) is True
    assert calibration_due(now_s - 3600.0, now_s, 86_400.0) is False
    assert calibration_due(now_s - 86_400.0, now_s, 86_400.0) is True
