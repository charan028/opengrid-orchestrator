"""Tests for ogsim.common.crypto: Ed25519 sign/verify against real keypairs
(cryptography's Ed25519PrivateKey/Ed25519PublicKey, never hardcoded keys)."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.crypto import (
    b64url_nopad_decode,
    b64url_nopad_encode,
    load_public_key,
    sign,
    verify_signature,
)

SIGNING_FIELDS = {
    "batch_id": "0190f7b0-0000-0000-0000-000000000000",
    "bank_id": "bank-07",
    "epoch": 42,
    "seq": 1183,
    "issued_at": "2026-09-26T18:00:02.104Z",
    "expires_at": "2026-09-26T18:00:12.104Z",
    "items": [{"hub_id": "hub-0007", "p_kw_setpoint": -3.2, "reason_code": "SELECTOR"}],
}


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    return private_key, public_bytes.hex()


def test_valid_signature_verifies(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    private_key, pub_hex = keypair
    signature = sign(private_key, SIGNING_FIELDS)
    public_key = load_public_key(pub_hex)
    assert verify_signature(public_key, SIGNING_FIELDS, signature) is True


def test_tampered_field_fails_verification(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    private_key, pub_hex = keypair
    signature = sign(private_key, SIGNING_FIELDS)
    public_key = load_public_key(pub_hex)
    tampered = {**SIGNING_FIELDS, "epoch": 43}
    assert verify_signature(public_key, tampered, signature) is False


def test_wrong_key_fails_verification(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    private_key, _ = keypair
    signature = sign(private_key, SIGNING_FIELDS)
    other_public = load_public_key(Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex())
    assert verify_signature(other_public, SIGNING_FIELDS, signature) is False


def test_missing_signature_fails_verification(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    _, pub_hex = keypair
    public_key = load_public_key(pub_hex)
    assert verify_signature(public_key, SIGNING_FIELDS, None) is False
    assert verify_signature(public_key, SIGNING_FIELDS, "") is False


def test_malformed_base64_fails_verification(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    _, pub_hex = keypair
    public_key = load_public_key(pub_hex)
    assert verify_signature(public_key, SIGNING_FIELDS, "not-valid-base64!!") is False


def test_load_public_key_accepts_base64_too(keypair: tuple[Ed25519PrivateKey, str]) -> None:
    private_key, pub_hex = keypair
    raw = bytes.fromhex(pub_hex)
    b64_key = base64.b64encode(raw).decode("ascii")
    public_key = load_public_key(b64_key)
    signature = sign(private_key, SIGNING_FIELDS)
    assert verify_signature(public_key, SIGNING_FIELDS, signature) is True


def test_b64url_nopad_round_trip() -> None:
    data = b"\x00\x01\xff\xfe\xfd" * 5
    encoded = b64url_nopad_encode(data)
    assert "=" not in encoded
    assert b64url_nopad_decode(encoded) == data


def test_load_public_key_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        load_public_key("aa" * 10)
