"""Tests for ogsim.fleet.firmware -- the hub side of a guardian-signed FIRMWARE_UPDATE (R3.1).

Covers: signature/lease/(epoch, seq) verification, the update timeline and device-info republish, the
failure kinds (transient before install, terminal after, hash mismatch, hardware incompatibility), rollback,
and that every status message validates against interfaces/mqtt/firmware_status.schema.json.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.crypto import sign
from ogsim.fleet.firmware import FirmwareManager, FirmwareSimConfig, verify_firmware_signature

MQTT_DIR = Path(__file__).resolve().parents[2] / "interfaces" / "mqtt"
SHA_NEW = "a" * 64
T0 = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
NOW = T0.timestamp()


def _validator(name: str) -> jsonschema.protocols.Validator:
    schema = json.loads((MQTT_DIR / name).read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


STATUS = _validator("firmware_status.schema.json")
COMMAND = _validator("firmware_command.schema.json")


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _command(
    key: Ed25519PrivateKey,
    *,
    hub_id: str = "hub-00001",
    target: str = "1.5.0",
    sha256: str = SHA_NEW,
    revision: str = "revB",
    epoch: int = 1,
    seq: int = 1,
    action: str = "UPDATE",
    issued_at: datetime = T0 - timedelta(seconds=1),
    lease_s: float = 120.0,
) -> dict:
    command = {
        "command_id": str(uuid.uuid4()),
        "campaign_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "hub_id": hub_id,
        "action": action,
        "target_version": target,
        "from_version": "1.4.2",
        "sha256": sha256,
        "hardware_revision": revision,
        "attempt": 1,
        "epoch": epoch,
        "seq": seq,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued_at + timedelta(seconds=lease_s)).isoformat().replace("+00:00", "Z"),
        "key_id": "guardian-test",
    }
    command["signature"] = sign(key, {k: v for k, v in command.items() if k != "key_id"})
    assert not list(COMMAND.iter_errors(command))
    return command


def _manager(**cfg: object) -> FirmwareManager:
    config = FirmwareSimConfig(duration_min_s=30.0, duration_max_s=30.0, **cfg)  # type: ignore[arg-type]
    return FirmwareManager(["hub-00001", "hub-00002"], config, rng=np.random.default_rng(7))


def _run(manager: FirmwareManager, until_s: float) -> list[dict]:
    messages = []
    for t in range(1, int(until_s) + 1):
        messages.extend(manager.tick(NOW + t))
    return messages


def test_signature_round_trip_and_tamper(key):
    command = _command(key)
    assert verify_firmware_signature(command, key.public_key())
    tampered = dict(command, target_version="9.9.9")
    assert not verify_firmware_signature(tampered, key.public_key())


def test_successful_update_timeline_and_device_info(key):
    manager = _manager()
    ack = manager.handle_command(_command(key), key.public_key(), NOW)
    assert ack["state"] == "ACCEPTED" and ack["reason"] is None
    assert manager.is_updating("hub-00001")
    messages = _run(manager, 31)
    states = [m["state"] for m in messages]
    assert states == ["DOWNLOADING", "INSTALLING", "REBOOTING", "DONE"]
    assert messages[-1]["firmware_version"] == "1.5.0"
    assert not manager.is_updating("hub-00001")
    assert manager.device_info_fields("hub-00001")["firmware_version"] == "1.5.0"
    assert manager.take_device_info_changes() == ["hub-00001"]
    assert manager.take_device_info_changes() == []
    stamps = [m["ts"] for m in [ack, *messages]]
    assert stamps == sorted(stamps)
    for message in [ack, *messages]:
        assert not list(STATUS.iter_errors(message)), message


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"forged": True}, "BAD_SIGNATURE"),
        ({"issued_at": T0 - timedelta(hours=1)}, "EXPIRED"),
    ],
)
def test_rejections_never_start_an_update(key, mutation, reason):
    manager = _manager()
    if mutation.get("forged"):
        command = _command(Ed25519PrivateKey.generate())
    else:
        command = _command(key, issued_at=mutation["issued_at"])
    status = manager.handle_command(command, key.public_key(), NOW)
    assert (status["state"], status["reason"]) == ("REJECTED", reason)
    assert not manager.is_updating("hub-00001")
    assert not list(STATUS.iter_errors(status))


def test_replayed_or_stale_seq_is_rejected(key):
    manager = _manager()
    first = _command(key, seq=5)
    assert manager.handle_command(first, key.public_key(), NOW)["state"] == "ACCEPTED"
    _run(manager, 31)
    assert manager.handle_command(first, key.public_key(), NOW + 40)["reason"] == "STALE_SEQ"
    assert manager.handle_command(_command(key, seq=4), key.public_key(), NOW + 40)["reason"] == "STALE_SEQ"


def test_second_command_while_updating_is_rejected(key):
    manager = _manager()
    manager.handle_command(_command(key, seq=1), key.public_key(), NOW)
    status = manager.handle_command(_command(key, seq=2), key.public_key(), NOW + 1)
    assert status["reason"] == "ALREADY_UPDATING"


def test_hash_mismatch_and_hardware_incompatible_are_terminal_refusals(key):
    manager = _manager(images={"1.5.0": "b" * 64})
    status = manager.handle_command(_command(key, sha256=SHA_NEW), key.public_key(), NOW)
    assert (status["state"], status["reason"]) == ("FAILED", "HASH_MISMATCH")
    status = manager.handle_command(_command(key, seq=2, revision="revZ"), key.public_key(), NOW)
    assert (status["state"], status["reason"]) == ("FAILED", "HARDWARE_INCOMPATIBLE")
    assert manager.version("hub-00001") == "1.4.2"


def test_install_failure_reverts_to_previous_version(key):
    manager = _manager(failure_rate=1.0, failure_kinds=("INSTALL_ERROR",))
    manager.handle_command(_command(key), key.public_key(), NOW)
    messages = _run(manager, 31)
    assert messages[-1]["state"] == "FAILED" and messages[-1]["reason"] == "INSTALL_ERROR"
    assert messages[-1]["firmware_version"] == "1.4.2"
    assert manager.version("hub-00001") == "1.4.2"
    assert manager.take_device_info_changes() == ["hub-00001"]


def test_download_error_fails_before_install(key):
    manager = _manager(failure_rate=1.0, failure_kinds=("DOWNLOAD_ERROR",))
    manager.handle_command(_command(key), key.public_key(), NOW)
    messages = _run(manager, 31)
    assert [m["state"] for m in messages] == ["DOWNLOADING", "FAILED"]
    assert messages[-1]["reason"] == "DOWNLOAD_ERROR"


def test_failure_rate_is_roughly_honoured():
    key = Ed25519PrivateKey.generate()
    hubs = [f"hub-{i:05d}" for i in range(200)]
    manager = FirmwareManager(
        hubs,
        FirmwareSimConfig(duration_min_s=1, duration_max_s=1, failure_rate=0.1),
        rng=np.random.default_rng(3),
    )
    for hub in hubs:
        manager.handle_command(_command(key, hub_id=hub), key.public_key(), NOW)
    final = manager.tick(NOW + 2)
    failed = sum(1 for m in final if m["state"] == "FAILED")
    assert 8 <= failed <= 35


def test_rollback_installs_the_previous_version(key):
    manager = _manager()
    manager.handle_command(_command(key, seq=1), key.public_key(), NOW)
    _run(manager, 31)
    assert manager.version("hub-00001") == "1.5.0"
    rollback = _command(key, seq=2, target="1.4.2", action="ROLLBACK", issued_at=T0 + timedelta(seconds=40))
    assert manager.handle_command(rollback, key.public_key(), NOW + 41)["state"] == "ACCEPTED"
    for t in range(42, 80):
        manager.tick(NOW + t)
    assert manager.version("hub-00001") == "1.4.2"


def test_unknown_hub_is_rejected(key):
    status = _manager().handle_command(_command(key, hub_id="hub-99999"), key.public_key(), NOW)
    assert (status["state"], status["reason"]) == ("REJECTED", "UNKNOWN_HUB")
