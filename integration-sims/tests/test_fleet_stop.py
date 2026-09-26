"""Tests for ogsim.fleet.stop -- retained stop scopes, ramp-to-zero,
late-join/reconnect honoring an already-retained stop, and crypto.md §2.3
StopEvent signature verification (safestop signs ENGAGE, guardian signs
RELEASE)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.crypto import sign
from ogsim.fleet.stop import StopRegistry, ramp_toward_zero, verify_stop_event


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


@pytest.fixture
def safestop_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _stop_event(signing_key: Ed25519PrivateKey, *, action: str, key_id: str = "test-key") -> dict:
    event = {
        "stop_id": str(uuid.uuid4()),
        "scope": "bank",
        "scope_id": "bank-000",
        "action": action,
        "reason": "test",
        "issued_by": "operator-1" if action == "RELEASE" else "SAFESTOP_AUTO",
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "approver_ref": "approver-2" if action == "RELEASE" else None,
    }
    fields = ("stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref")
    signing_fields = {k: event[k] for k in fields if k in event}
    event["key_id"] = key_id
    event["signature"] = sign(signing_key, signing_fields)
    return event


def test_engage_signed_by_safestop_key_is_accepted(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    event = _stop_event(safestop_key, action="ENGAGE", key_id="safestop-1")
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) is None


def test_engage_signed_by_guardian_key_is_rejected(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    # crypto.md §2.3: only the safestop key may sign action="ENGAGE".
    event = _stop_event(guardian_key, action="ENGAGE", key_id="guardian-1")
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "BAD_SIGNATURE"


def test_release_signed_by_guardian_key_is_accepted(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    event = _stop_event(guardian_key, action="RELEASE", key_id="guardian-1")
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) is None


def test_release_signed_by_safestop_key_is_rejected(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    # crypto.md §2.3: a verifier rejects a RELEASE signed by the safestop key.
    event = _stop_event(safestop_key, action="RELEASE", key_id="safestop-1")
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "BAD_SIGNATURE"


def test_bad_signature_is_rejected(safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey) -> None:
    event = _stop_event(safestop_key, action="ENGAGE")
    event["signature"] = "not-a-valid-signature"
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "BAD_SIGNATURE"


def test_missing_signature_is_rejected(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    event = _stop_event(safestop_key, action="ENGAGE")
    del event["signature"]
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "BAD_SIGNATURE"


@pytest.mark.parametrize("approver", [None, "", "operator-1", " OPERATOR-1 "])
def test_release_without_a_distinct_second_approver_is_rejected(
    safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey, approver: str | None
) -> None:
    """K8 Tier 2: even a guardian-signed RELEASE needs two people (requester operator-1 + approver)."""
    event = _stop_event(guardian_key, action="RELEASE", key_id="guardian-1")
    event["approver_ref"] = approver
    fields = ("stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref")
    event["signature"] = sign(guardian_key, {k: event[k] for k in fields})
    assert (
        verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "NOT_TIER2_APPROVED"
    )


def test_unknown_action_is_rejected(safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey) -> None:
    event = _stop_event(guardian_key, action="RELEASE")
    event["action"] = "PAUSE"
    assert verify_stop_event(event, safestop_key.public_key(), guardian_key.public_key()) == "UNKNOWN_ACTION"
