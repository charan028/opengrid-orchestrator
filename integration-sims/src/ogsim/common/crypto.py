"""ogsim.common.crypto -- Ed25519 verification per interfaces/crypto.md.

Verifies command batches and stop events signed by the guardian/safestop
keys (crypto.md §2.1, §2.3): JCS-canonicalize the signed fields (excluding
`signature`/`key_id`), base64url-nopad decode `signature`, Ed25519-verify.

Public key encoding: this module accepts either raw hex (64 hex chars) or
base64 (standard alphabet, padded or not) for the 32-byte guardian public
key file, auto-detected by length/alphabet. Hex is preferred for new dev
config because it has no padding ambiguity; base64 is accepted so a key
exported by the orchestrator's own tooling need not be re-encoded.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ogsim.common.jcs import canonicalize_bytes

ED25519_PUBLIC_KEY_LEN = 32


def b64url_nopad_decode(text: str) -> bytes:
    """Decodes base64url with no padding, per crypto.md §1 `signature` encoding."""
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def b64url_nopad_encode(data: bytes) -> str:
    """Encodes bytes as base64url with no padding (crypto.md §1)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_public_key(key_text: str) -> Ed25519PublicKey:
    """Parses a 32-byte Ed25519 public key from hex or base64 config text."""
    raw = _decode_key_text(key_text.strip())
    if len(raw) != ED25519_PUBLIC_KEY_LEN:
        raise ValueError(f"guardian public key must decode to {ED25519_PUBLIC_KEY_LEN} bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def _decode_key_text(text: str) -> bytes:
    if len(text) == ED25519_PUBLIC_KEY_LEN * 2:
        try:
            return binascii.unhexlify(text)
        except binascii.Error:
            pass
    padded = text + "=" * (-len(text) % 4)
    return base64.b64decode(padded)


def verify_signature(public_key: Ed25519PublicKey, signing_fields: dict, signature_field: str | None) -> bool:
    """Verifies `signature_field` (base64url-nopad) over JCS(signing_fields).

    `signing_fields` must already have `signature`/`key_id` excluded. A
    missing/malformed signature verifies as False, never raises -- callers
    map that to the wire's BAD_SIGNATURE reject_reason.
    """
    if not signature_field:
        return False
    try:
        signature = b64url_nopad_decode(signature_field)
    except (binascii.Error, ValueError):
        return False
    signing_input = canonicalize_bytes(signing_fields)
    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature:
        return False
    return True


def sign(private_key: Ed25519PrivateKey, signing_fields: dict) -> str:
    """Test/self-test helper (fleet's tampered_unsigned_command anomaly and
    unit tests use this with a throwaway keypair, never the real guardian
    private key): signs JCS(signing_fields), returns base64url-nopad."""
    signing_input = canonicalize_bytes(signing_fields)
    return b64url_nopad_encode(private_key.sign(signing_input))
