"""S5.5.2's asset-health lifecycle -- every documented transition, plus fail-closed on undefined
(state, event) pairs (07-delivery/06-service-profiles-and-power-quality.md)."""

from __future__ import annotations

import pytest

from opengrid.assets.state_machine import DriftEvent, InvalidAssetTransitionError, next_asset_state


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        ("OK", DriftEvent.PERSISTENT_DRIFT_DETECTED, "WATCH"),
        ("WATCH", DriftEvent.WATCH_CLEARED, "OK"),
        ("WATCH", DriftEvent.CALIBRATION_CORRECTED, "OK"),
        ("WATCH", DriftEvent.CALIBRATION_IMPROVED, "WATCH"),
        ("WATCH", DriftEvent.CALIBRATION_NO_CHANGE, "DEGRADED"),
        ("WATCH", DriftEvent.CALIBRATION_WORSE_ROLLED_BACK, "QUARANTINED"),
        ("WATCH", DriftEvent.RECURRENCE_WITHIN_WINDOW, "DEGRADED"),
        ("DEGRADED", DriftEvent.QUARANTINE_ESCALATED, "QUARANTINED"),
        ("DEGRADED", DriftEvent.REPLACEMENT_SCHEDULED, "AWAITING_REPLACEMENT"),
        ("QUARANTINED", DriftEvent.REPLACEMENT_SCHEDULED, "AWAITING_REPLACEMENT"),
        ("AWAITING_REPLACEMENT", DriftEvent.INVERTER_REPLACED, "RECOMMISSIONING"),
        ("RECOMMISSIONING", DriftEvent.RECOMMISSIONING_VERIFIED, "OK"),
        ("RECOMMISSIONING", DriftEvent.RECOMMISSIONING_FAILED, "RECOMMISSIONING"),
    ],
)
def test_documented_transitions(state, event, expected):
    assert next_asset_state(state, event) == expected


@pytest.mark.parametrize(
    ("state", "event"),
    [
        ("OK", DriftEvent.CALIBRATION_CORRECTED),
        ("QUARANTINED", DriftEvent.PERSISTENT_DRIFT_DETECTED),
        ("OK", DriftEvent.INVERTER_REPLACED),
        ("AWAITING_REPLACEMENT", DriftEvent.RECOMMISSIONING_VERIFIED),
    ],
)
def test_undefined_transition_fails_closed(state, event):
    """TS-17b-adjacent: an undefined transition is refused, not guessed at -- a hub already excluded
    (e.g. QUARANTINED) getting a fresh PERSISTENT_DRIFT_DETECTED signal is a caller bug, not a no-op."""
    with pytest.raises(InvalidAssetTransitionError):
        next_asset_state(state, event)


def test_ts17a_full_uncorrectable_escalation_sequence():
    """TS-17a: calibration_drift_hardware -> NO_CHANGE -> DEGRADED -> work order -> REPLACE_INVERTER ->
    RECOMMISSIONING -> passing verification -> OK, driven purely through the state machine."""
    state = "OK"
    state = next_asset_state(state, DriftEvent.PERSISTENT_DRIFT_DETECTED)
    assert state == "WATCH"
    state = next_asset_state(state, DriftEvent.CALIBRATION_NO_CHANGE)
    assert state == "DEGRADED"
    state = next_asset_state(state, DriftEvent.REPLACEMENT_SCHEDULED)
    assert state == "AWAITING_REPLACEMENT"
    state = next_asset_state(state, DriftEvent.INVERTER_REPLACED)
    assert state == "RECOMMISSIONING"
    state = next_asset_state(state, DriftEvent.RECOMMISSIONING_VERIFIED)
    assert state == "OK"


def test_ts16a_corrected_returns_to_ok():
    """TS-16a: calibration_drift_correctable -> CalibrationCommand -> verified CORRECTED -> back to OK,
    no maintenance work order opened (the state machine never enters DEGRADED on this path)."""
    state = next_asset_state("OK", DriftEvent.PERSISTENT_DRIFT_DETECTED)
    state = next_asset_state(state, DriftEvent.CALIBRATION_CORRECTED)
    assert state == "OK"
