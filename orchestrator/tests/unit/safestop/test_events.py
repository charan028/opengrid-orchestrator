from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.core.crypto import generate_keypair, private_key_from_seed, verify_payload
from opengrid.safestop.events import (
    RAMP_WINDOW_S,
    InvalidScopeReferenceError,
    build_engage_event,
    ramp_window_s,
    stop_topic_suffix,
    to_wire_scope,
)


def test_ramp_window_seconds_match_spec_02a_s6_5():
    assert ramp_window_s("BANK") == 30
    assert ramp_window_s("ZONE") == 60
    assert ramp_window_s("FLEET") == 120
    assert set(RAMP_WINDOW_S) == {"BANK", "ZONE", "FLEET"}


def test_to_wire_scope_fleet_has_null_scope_id():
    wire_scope, scope_id = to_wire_scope("FLEET", "")
    assert wire_scope == "fleet"
    assert scope_id is None


@pytest.mark.parametrize(("scope", "wire"), [("ZONE", "zone"), ("BANK", "bank")])
def test_to_wire_scope_zone_bank_require_scope_ref(scope, wire):
    wire_scope, scope_id = to_wire_scope(scope, "bank-07")
    assert wire_scope == wire
    assert scope_id == "bank-07"


@pytest.mark.parametrize("scope", ["ZONE", "BANK"])
def test_to_wire_scope_rejects_empty_ref_for_zone_bank(scope):
    with pytest.raises(InvalidScopeReferenceError):
        to_wire_scope(scope, "")


def test_stop_topic_suffix_fleet():
    from uuid import uuid4

    stop_id = uuid4()
    suffix = stop_topic_suffix("FLEET", "", stop_id)
    assert suffix == f"stop/fleet/{stop_id}"


def test_stop_topic_suffix_bank_includes_ref():
    from uuid import uuid4

    stop_id = uuid4()
    suffix = stop_topic_suffix("BANK", "bank-07", stop_id)
    assert suffix == f"stop/bank/bank-07/{stop_id}"


def test_build_engage_event_is_verifiably_signed():
    seed, pub = generate_keypair()
    event = build_engage_event(
        scope="ZONE",
        scope_ref="LZ_NORTH",
        reason="chaos drill",
        initiator_ref="operator:alice",
        key_id="safestop-test",
        seed=seed,
        issued_at=datetime(2026, 9, 26, 18, 0, 2, tzinfo=UTC),
    )
    assert event.action == "ENGAGE"
    assert event.scope == "zone"
    assert event.scope_id == "LZ_NORTH"
    assert event.key_id == "safestop-test"
    assert verify_payload(pub, event.signing_payload(), event.signature)


def test_build_engage_event_signature_excludes_key_id_and_signature_fields():
    seed, _pub = generate_keypair()
    event = build_engage_event(
        scope="BANK",
        scope_ref="bank-07",
        reason="overload",
        initiator_ref="operator:bob",
        key_id="safestop-test",
        seed=seed,
    )
    signing_payload = event.signing_payload()
    assert "key_id" not in signing_payload
    assert "signature" not in signing_payload


def test_build_engage_event_wrong_key_fails_verification():
    seed, _pub = generate_keypair()
    _other_seed, other_pub = generate_keypair()
    event = build_engage_event(
        scope="FLEET",
        scope_ref="",
        reason="test",
        initiator_ref="operator:carol",
        key_id="safestop-test",
        seed=seed,
    )
    assert not verify_payload(other_pub, event.signing_payload(), event.signature)


def test_build_engage_event_uses_own_private_key_derivation():
    seed, pub = generate_keypair()
    derived_pub = private_key_from_seed(seed).public_key().public_bytes_raw()
    assert derived_pub == pub
