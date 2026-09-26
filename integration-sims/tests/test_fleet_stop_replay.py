"""K8 BLOCKER regression: the hub tracked stops by scope only, so a replayed old signed RELEASE lifted a
newer stop, and on reconnect the retained RELEASE and a newer ENGAGE could arrive in either order and
leave the scope released. Stop state is now per stop_id with an issued_at backstop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.crypto import sign
from ogsim.fleet.__main__ import handle_stop_message
from ogsim.fleet.stop import StopRegistry

_SIGNED = ("stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref")
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _Engine:
    """Just enough of FleetEngine for `handle_stop_message`: the real verify + registry path."""

    def __init__(self) -> None:
        self.stops = StopRegistry()

    def handle_stop_event(self, event, safestop_public_key, guardian_public_key) -> bool:
        from ogsim.fleet.stop import verify_stop_event

        if verify_stop_event(event, safestop_public_key, guardian_public_key) is not None:
            return False
        return self.stops.apply_verified_event(event)


@pytest.fixture
def keys() -> tuple[Ed25519PrivateKey, Ed25519PrivateKey]:
    return Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()  # (safestop, guardian)


def _event(
    key: Ed25519PrivateKey, action: str, stop_id: str, at: datetime, *, scope_id: str = "bank-001"
) -> dict:
    event = {
        "stop_id": stop_id,
        "scope": "bank",
        "scope_id": scope_id,
        "action": action,
        "reason": "test",
        "issued_by": "operator:alice" if action == "RELEASE" else "SAFESTOP_AUTO",
        "issued_at": at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "approver_ref": "operator:bob" if action == "RELEASE" else None,
        "key_id": "k",
    }
    event["signature"] = sign(key, {k: event[k] for k in _SIGNED})
    return event


def _deliver(engine: _Engine, keys, *events: dict) -> None:
    safestop, guardian = keys
    for event in events:
        handle_stop_message(
            engine,
            f"og/v1/stop/bank/bank-001/{event['stop_id']}",
            event,
            safestop.public_key(),
            guardian.public_key(),
        )


def _stopped(engine: _Engine) -> bool:
    return engine.stops.is_stopped("LZ_NORTH", "bank-001")


def test_a_replayed_old_release_never_lifts_a_newer_stop(keys):
    safestop, guardian = keys
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    engine = _Engine()
    r1 = _event(guardian, "RELEASE", s1, T0 + timedelta(minutes=5))
    _deliver(engine, keys, _event(safestop, "ENGAGE", s1, T0), r1)
    assert not _stopped(engine)

    _deliver(engine, keys, _event(safestop, "ENGAGE", s2, T0 + timedelta(hours=1)))
    _deliver(engine, keys, r1)  # the attacker replays the old, validly signed RELEASE

    assert _stopped(engine)


@pytest.mark.parametrize("order", ["release_first", "engage_first"])
def test_reconnect_with_retained_old_release_and_newer_engage_in_either_order_stays_stopped(keys, order):
    safestop, guardian = keys
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    r1 = _event(guardian, "RELEASE", s1, T0 + timedelta(minutes=5))
    e2 = _event(safestop, "ENGAGE", s2, T0 + timedelta(hours=1))
    engine = _Engine()  # a fresh hub: it never saw S1's ENGAGE

    _deliver(engine, keys, *((r1, e2) if order == "release_first" else (e2, r1)))

    assert _stopped(engine)


def test_the_scope_stays_stopped_while_any_engage_is_outstanding(keys):
    safestop, guardian = keys
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    engine = _Engine()
    _deliver(
        engine,
        keys,
        _event(safestop, "ENGAGE", s1, T0),
        _event(safestop, "ENGAGE", s2, T0 + timedelta(minutes=1)),
    )
    _deliver(engine, keys, _event(guardian, "RELEASE", s1, T0 + timedelta(minutes=2)))
    assert _stopped(engine)
    _deliver(engine, keys, _event(guardian, "RELEASE", s2, T0 + timedelta(minutes=3)))
    assert not _stopped(engine)


def test_a_release_older_than_the_newest_engage_is_ignored(keys):
    safestop, guardian = keys
    s1 = str(uuid.uuid4())
    engine = _Engine()
    _deliver(engine, keys, _event(safestop, "ENGAGE", s1, T0 + timedelta(hours=1)))
    _deliver(engine, keys, _event(guardian, "RELEASE", s1, T0))  # issued before the ENGAGE it names
    assert _stopped(engine)


def test_an_engage_arriving_after_its_own_release_is_ignored(keys):
    safestop, guardian = keys
    s1 = str(uuid.uuid4())
    engine = _Engine()
    _deliver(
        engine,
        keys,
        _event(guardian, "RELEASE", s1, T0 + timedelta(minutes=5)),
        _event(safestop, "ENGAGE", s1, T0),
    )
    assert not _stopped(engine)


def test_a_release_of_an_unknown_stop_changes_nothing(keys):
    safestop, guardian = keys
    engine = _Engine()
    _deliver(engine, keys, _event(safestop, "ENGAGE", "s-known", T0))
    _deliver(engine, keys, _event(guardian, "RELEASE", "s-unknown", T0 + timedelta(minutes=1)))
    assert _stopped(engine)
