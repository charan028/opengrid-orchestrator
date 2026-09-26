"""K8 / crypto.md §2.3 at the fleet's MQTT dispatch: stop state changes ONLY through a signature-verified
StopEvent. Regression: any payload that parsed as JSON-empty ({}, null, [], 0, false) on a stop topic
released that scope with no signature check, so anyone able to publish on stop/fleet/# could lift a fleet
stop."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import sign
from ogsim.fleet.__main__ import _dispatch_message
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="ogtest/unit")
_SIGNED = ("stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref")


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


@pytest.fixture
def safestop_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _event(
    key: Ed25519PrivateKey | None,
    *,
    action: str,
    scope: str = "fleet",
    scope_id: str | None = None,
    stop_id: str | None = None,
) -> dict:
    event: dict[str, Any] = {
        "stop_id": stop_id or str(uuid.uuid4()),
        "scope": scope,
        "scope_id": scope_id,
        "action": action,
        "reason": "test",
        "issued_by": "operator-1" if action == "RELEASE" else "SAFESTOP_AUTO",
        "issued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "approver_ref": "approver-2" if action == "RELEASE" else None,
        "key_id": "test-key",
    }
    if key is not None:
        event["signature"] = sign(key, {k: event[k] for k in _SIGNED})
    return event


async def _dispatch(engine: FleetEngine, topic: str, payload: bytes, safestop, guardian) -> None:
    await _dispatch_message(engine, None, topic, payload, guardian.public_key(), safestop.public_key())  # type: ignore[arg-type]


async def _engaged_fleet(engine: FleetEngine, safestop, guardian) -> str:
    engage = _event(safestop, action="ENGAGE")
    await _dispatch(
        engine, f"ogtest/unit/stop/fleet/{engage['stop_id']}", json.dumps(engage).encode(), safestop, guardian
    )
    assert engine.stops.fleet_stopped
    return str(engage["stop_id"])


@pytest.mark.parametrize("payload", [b"{}", b"null", b"[]", b"0", b"false", b"", b'""'])
async def test_empty_or_non_event_payloads_never_release_a_stop(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey, payload: bytes
) -> None:
    await _engaged_fleet(engine, safestop_key, guardian_key)

    await _dispatch(engine, "ogtest/unit/stop/fleet/any-id", payload, safestop_key, guardian_key)

    assert engine.stops.fleet_stopped


@pytest.mark.parametrize("payload", [b"[1]", b"5", b'"RELEASE"', b"true"])
async def test_non_object_payloads_are_ignored_without_crashing_the_consumer(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey, payload: bytes
) -> None:
    await _engaged_fleet(engine, safestop_key, guardian_key)
    await _dispatch(engine, "ogtest/unit/stop/fleet/any-id", payload, safestop_key, guardian_key)
    assert engine.stops.fleet_stopped


async def test_unsigned_release_is_rejected(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    stop_id = await _engaged_fleet(engine, safestop_key, guardian_key)
    release = _event(None, action="RELEASE", stop_id=stop_id)
    await _dispatch(
        engine, "ogtest/unit/stop/fleet/x", json.dumps(release).encode(), safestop_key, guardian_key
    )
    assert engine.stops.fleet_stopped


@pytest.mark.parametrize("forger", ["safestop", "attacker"])
async def test_forged_release_is_rejected(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey, forger: str
) -> None:
    stop_id = await _engaged_fleet(engine, safestop_key, guardian_key)
    key = safestop_key if forger == "safestop" else Ed25519PrivateKey.generate()
    release = _event(key, action="RELEASE", stop_id=stop_id)
    await _dispatch(
        engine, "ogtest/unit/stop/fleet/x", json.dumps(release).encode(), safestop_key, guardian_key
    )
    assert engine.stops.fleet_stopped


async def test_guardian_signed_release_lifts_the_stop(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    stop_id = await _engaged_fleet(engine, safestop_key, guardian_key)
    release = _event(guardian_key, action="RELEASE", stop_id=stop_id)
    await _dispatch(
        engine, "ogtest/unit/stop/fleet/x", json.dumps(release).encode(), safestop_key, guardian_key
    )
    assert not engine.stops.fleet_stopped


async def test_the_signed_scope_governs_not_the_topic(
    engine: FleetEngine, safestop_key: Ed25519PrivateKey, guardian_key: Ed25519PrivateKey
) -> None:
    """A guardian-signed release of bank-000 published on the fleet topic releases only bank-000."""
    await _engaged_fleet(engine, safestop_key, guardian_key)
    engine.stops.engage("bank", "bank-000", stop_id="bank-stop-1")
    release = _event(guardian_key, action="RELEASE", scope="bank", scope_id="bank-000", stop_id="bank-stop-1")
    await _dispatch(
        engine, "ogtest/unit/stop/fleet/x", json.dumps(release).encode(), safestop_key, guardian_key
    )
    assert engine.stops.fleet_stopped
    assert "bank-000" not in engine.stops.banks_stopped
