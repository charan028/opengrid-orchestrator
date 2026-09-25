"""Tests for ogsim.fleet.stop -- retained stop scopes, ramp-to-zero, and
late-join/reconnect honoring an already-retained stop."""

from __future__ import annotations

from ogsim.fleet.stop import StopRegistry, ramp_toward_zero


def test_fleet_scope_stops_every_zone_and_bank() -> None:
    registry = StopRegistry()
    registry.engage("fleet", None)
    assert registry.is_stopped("LZ_NORTH", "bank-000") is True
    assert registry.is_stopped("LZ_WEST", "bank-039") is True


def test_zone_scope_stops_only_that_zone() -> None:
    registry = StopRegistry()
    registry.engage("zone", "LZ_NORTH")
    assert registry.is_stopped("LZ_NORTH", "bank-000") is True
    assert registry.is_stopped("LZ_SOUTH", "bank-001") is False


def test_bank_scope_stops_only_that_bank() -> None:
    registry = StopRegistry()
    registry.engage("bank", "bank-005")
    assert registry.is_stopped("LZ_NORTH", "bank-005") is True
    assert registry.is_stopped("LZ_NORTH", "bank-006") is False


def test_release_clears_the_stop() -> None:
    registry = StopRegistry()
    registry.engage("zone", "LZ_NORTH")
    registry.release("zone", "LZ_NORTH")
    assert registry.is_stopped("LZ_NORTH", "bank-000") is False


def test_late_join_sees_an_already_retained_stop() -> None:
    # Simulates a hub task created AFTER the stop was already engaged
    # (a reconnect): the registry is queried fresh, so no separate "did I
    # process this event" state can be missed.
    registry = StopRegistry()
    registry.engage("bank", "bank-007")
    late_joining_hub_zone, late_joining_hub_bank = "LZ_HOUSTON", "bank-007"
    assert registry.is_stopped(late_joining_hub_zone, late_joining_hub_bank) is True


def test_ramp_toward_zero_steps_down_and_stops_at_zero() -> None:
    p = 5.0
    for _ in range(10):
        p = ramp_toward_zero(p, dt_s=2.0, ramp_time_s=4.0, p_kw_limit=5.0)
    assert p == 0.0


def test_ramp_toward_zero_never_overshoots_sign() -> None:
    p = ramp_toward_zero(1.0, dt_s=2.0, ramp_time_s=4.0, p_kw_limit=5.0)
    assert p >= 0.0


def test_ramp_toward_zero_handles_negative_setpoint() -> None:
    p = -5.0
    for _ in range(10):
        p = ramp_toward_zero(p, dt_s=2.0, ramp_time_s=4.0, p_kw_limit=5.0)
    assert p == 0.0
