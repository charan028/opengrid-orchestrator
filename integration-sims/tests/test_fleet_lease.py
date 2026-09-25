"""Tests for ogsim.fleet.lease -- expiry -> brief hold -> local autonomy."""

from __future__ import annotations

from ogsim.fleet.lease import HoldTracker, lease_expiry_from_message


def test_active_lease_is_not_holding_or_autonomous() -> None:
    tracker = HoldTracker(hold_after_expiry_s=5.0)
    state = tracker.state_for("hub-1", expires_at=100.0, now=50.0)
    assert state.holding is False
    assert state.local_autonomy is False


def test_no_lease_is_immediately_local_autonomy() -> None:
    tracker = HoldTracker(hold_after_expiry_s=5.0)
    state = tracker.state_for("hub-1", expires_at=0.0, now=50.0)
    assert state.local_autonomy is True


def test_just_expired_lease_holds_before_autonomy() -> None:
    tracker = HoldTracker(hold_after_expiry_s=5.0)
    state = tracker.state_for("hub-1", expires_at=100.0, now=101.0)
    assert state.holding is True
    assert state.local_autonomy is False


def test_lease_falls_to_autonomy_after_hold_grace_elapses() -> None:
    tracker = HoldTracker(hold_after_expiry_s=5.0)
    tracker.state_for("hub-1", expires_at=100.0, now=101.0)  # starts holding
    state = tracker.state_for("hub-1", expires_at=100.0, now=106.0)
    assert state.holding is False
    assert state.local_autonomy is True


def test_renewal_before_grace_elapses_clears_hold() -> None:
    tracker = HoldTracker(hold_after_expiry_s=5.0)
    tracker.state_for("hub-1", expires_at=100.0, now=101.0)  # starts holding
    tracker.renew("hub-1")
    state = tracker.state_for("hub-1", expires_at=200.0, now=150.0)
    assert state.holding is False
    assert state.local_autonomy is False


def test_lease_expiry_from_message_parses_rfc3339() -> None:
    seconds = lease_expiry_from_message("2026-09-26T18:00:30.000Z")
    assert seconds > 0
