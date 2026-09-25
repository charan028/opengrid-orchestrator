"""Ed25519 sign/verify and JCS (RFC 8785) canonicalization for the command envelope.

Single owner per 02b S12 / interfaces/crypto.md. `guardian` signs, `safestop` signs its distinct
stop-only key the same way, `sim`/hub verifiers verify. `opengrid.trace.tracehash` imports
`canonicalize_json` from here rather than re-deriving JCS.
"""

from __future__ import annotations

import base64
import math
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class SignatureError(Exception):
    """Raised when a signature fails to verify."""


def _canon_number(n: float | int) -> str:
    if isinstance(n, bool):  # bool is an int subclass; JSON has no bool-as-number here
        raise TypeError("booleans are not numbers")
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if math.isnan(n) or math.isinf(n):
            raise ValueError("JCS cannot encode NaN/Infinity")
        if n == int(n) and abs(n) < 1e15:
            return str(int(n))
        # ECMAScript-compatible shortest round-trip representation
        return repr(n)
    raise TypeError(f"unsupported number type: {type(n)!r}")


def _canon_string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _canon_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _canon_number(value)
    if isinstance(value, str):
        return _canon_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canon_value(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        body = ",".join(f"{_canon_string(k)}:{_canon_value(v)}" for k, v in items)
        return "{" + body + "}"
    raise TypeError(f"unsupported JCS value type: {type(value)!r}")


def canonicalize_json(obj: Any) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme, UTF-8 encoded bytes. `obj` must already be built from
    JSON-safe primitives (str/int/float/bool/None/list/dict) -- callers (pydantic models) use
    `.model_dump(mode="json")` before calling this.
    """
    return _canon_value(obj).encode("utf-8")


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_seed_32_bytes, public_key_32_bytes)."""
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes_raw()
    pub = priv.public_key().public_bytes_raw()
    return seed, pub


def private_key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_from_bytes(pub: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(pub)


def sign_payload(seed: bytes, payload: dict[str, Any]) -> str:
    """Sign the JCS bytes of `payload` (which must already exclude signature/key_id fields).
    Returns base64url, no padding.
    """
    priv = private_key_from_seed(seed)
    signing_input = canonicalize_json(payload)
    sig = priv.sign(signing_input)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def verify_payload(pub: bytes, payload: dict[str, Any], signature_b64url: str) -> bool:
    """Verify `signature_b64url` over the JCS bytes of `payload`. Returns False (never raises) on any
    malformed input or signature mismatch -- callers treat False as BAD_SIGNATURE.
    """
    try:
        padded = signature_b64url + "=" * (-len(signature_b64url) % 4)
        sig = base64.urlsafe_b64decode(padded)
        signing_input = canonicalize_json(payload)
        public_key_from_bytes(pub).verify(sig, signing_input)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_hex_of_json(obj: Any) -> str:
    return sha256_hex(canonicalize_json(obj))
