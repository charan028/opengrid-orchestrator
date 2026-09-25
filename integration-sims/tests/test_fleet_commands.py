"""Tests for ogsim.fleet.commands -- signature, epoch/seq, issued_at/expires_at."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.crypto import sign
from ogsim.fleet.commands import CommandVerdict, build_ack, evaluate_batch


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _batch(
    guardian_key: Ed25519PrivateKey,
    *,
    epoch: int = 1,
    seq: int = 1,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    hub_id: str = "hub-00001",
    unsigned: bool = False,
    wrong_key: bool = False,
) -> dict:
    now = datetime.now(UTC)
    issued_at = issued_at or now - timedelta(seconds=1)
    expires_at = expires_at or now + timedelta(seconds=30)
    batch = {
        "batch_id": str(uuid.uuid4()),
        "bank_id": "bank-000",
        "epoch": epoch,
        "seq": seq,
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "items": [{"hub_id": hub_id, "p_kw_setpoint": -3.2, "reason_code": "SELECTOR"}],
    }
    signing_key = Ed25519PrivateKey.generate() if wrong_key else guardian_key
    signing_fields = {
        k: batch[k] for k in ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")
    }
    batch["key_id"] = "guardian-test"
    batch["signature"] = "" if unsigned else sign(signing_key, signing_fields)
    return batch


def test_valid_batch_is_accepted(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, datetime.now(UTC))
    assert verdicts[0].accepted is True
    assert verdicts[0].reject_reason is None
    assert verdicts[0].requested_p_kw_setpoint == -3.2


def test_missing_signature_rejected_as_bad_signature(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key, unsigned=True)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, datetime.now(UTC))
    assert verdicts[0].accepted is False
    assert verdicts[0].reject_reason == "BAD_SIGNATURE"


def test_wrong_key_rejected_as_bad_signature(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key, wrong_key=True)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, datetime.now(UTC))
    assert verdicts[0].reject_reason == "BAD_SIGNATURE"


def test_stale_epoch_rejected(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key, epoch=5, seq=1)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {"hub-00001": (10, 999)}, datetime.now(UTC))
    assert verdicts[0].reject_reason == "STALE_EPOCH"


def test_stale_seq_within_same_epoch_rejected(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key, epoch=5, seq=3)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {"hub-00001": (5, 3)}, datetime.now(UTC))
    assert verdicts[0].reject_reason == "STALE_SEQ"


def test_higher_epoch_resets_seq_baseline(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key, epoch=6, seq=0)
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {"hub-00001": (5, 999)}, datetime.now(UTC))
    assert verdicts[0].accepted is True


def test_not_yet_valid_rejected_as_expired(guardian_key: Ed25519PrivateKey) -> None:
    now = datetime.now(UTC)
    batch = _batch(
        guardian_key, issued_at=now + timedelta(seconds=10), expires_at=now + timedelta(seconds=40)
    )
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, now)
    assert verdicts[0].reject_reason == "EXPIRED"


def test_past_expiry_rejected_as_expired(guardian_key: Ed25519PrivateKey) -> None:
    now = datetime.now(UTC)
    batch = _batch(
        guardian_key, issued_at=now - timedelta(seconds=40), expires_at=now - timedelta(seconds=10)
    )
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, now)
    assert verdicts[0].reject_reason == "EXPIRED"


def test_build_ack_for_accepted_verdict_matches_ack_schema_shape() -> None:
    verdict = CommandVerdict(
        hub_id="hub-00001", accepted=True, reject_reason=None, requested_p_kw_setpoint=-3.2
    )
    ack = build_ack(verdict, batch_id="b1", applied_p_kw=-2.5, now_epoch=1780000000.0)
    assert ack == {
        "hub_id": "hub-00001",
        "batch_id": "b1",
        "accepted": True,
        "applied_p_kw": -2.5,
        "reject_reason": None,
        "ts": "2026-05-28T20:26:40.000Z",
    }


def test_build_ack_for_rejected_verdict_has_no_applied_power() -> None:
    verdict = CommandVerdict(
        hub_id="hub-00002", accepted=False, reject_reason="STALE_SEQ", requested_p_kw_setpoint=None
    )
    # applied_p_kw is always None for a rejected verdict, even if the caller
    # (incorrectly) passed one in -- a rejected command was never applied.
    ack = build_ack(verdict, batch_id="b2", applied_p_kw=4.0, now_epoch=1780000000.0)
    assert ack["accepted"] is False
    assert ack["applied_p_kw"] is None
    assert ack["reject_reason"] == "STALE_SEQ"
    assert ack["hub_id"] == "hub-00002"
    assert ack["batch_id"] == "b2"


def test_precondition_ledger_version_passes_through_unconditionally(guardian_key: Ed25519PrivateKey) -> None:
    batch = _batch(guardian_key)
    batch["precondition"] = {"ledger_version": 42}
    # precondition is not part of the signed fields (crypto.md §2.1), so
    # adding it after signing must not change signature verification, and
    # the sim has no ledger to check it against.
    verdicts = evaluate_batch(batch, guardian_key.public_key(), {}, datetime.now(UTC))
    assert verdicts[0].accepted is True
