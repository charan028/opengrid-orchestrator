"""Ed25519 sign/verify and JCS (RFC 8785) canonicalization for the command envelope.

Single owner per 02b S12 / interfaces/crypto.md. `guardian` signs, `safestop` signs its distinct
stop-only key the same way, `sim`/hub verifiers verify. `opengrid.trace.tracehash` imports
`canonicalize_json` from here rather than re-deriving JCS.
"""

from __future__ import annotations

import base64
import math
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: CORE-002: JCS/JSON key ordering is by UTF-16 code unit (RFC 8785); for any key whose characters are
#: all in the Basic Multilingual Plane, Python's code-point ordering already agrees with that (each
#: BMP code point is exactly one UTF-16 code unit, in the same relative order). A key containing a
#: non-BMP (astral) character breaks that equivalence: a supplementary code point sorts AFTER every
#: BMP code point by raw value, but its UTF-16 surrogate pair's high surrogate (0xD800-0xDBFF) sorts
#: BEFORE the BMP range 0xE000-0xFFFF. Rather than re-implement UTF-16 comparison, this module refuses
#: to canonicalize such a key -- MVP-S has no legitimate use for astral characters in a wire object key.
_NON_BMP_THRESHOLD = 0x10000


class SignatureError(Exception):
    """Raised when a signature fails to verify."""


class CanonicalizationError(Exception):
    """Raised when a value cannot be canonicalized per RFC 8785 (JCS)."""


def _canon_number(n: float | int) -> str:
    if isinstance(n, bool):  # bool is an int subclass; JSON has no bool-as-number here
        raise TypeError("booleans are not numbers")
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if math.isnan(n) or math.isinf(n):
            raise ValueError("JCS cannot encode NaN/Infinity")
        return _ecmascript_number_to_string(n)
    raise TypeError(f"unsupported number type: {type(n)!r}")


def _ecmascript_number_to_string(x: float) -> str:
    """CORE-001: ECMAScript `Number::toString` (radix 10), exactly -- JCS numbers are "serialized as
    per section 6.1 of [ECMA-262]" (RFC 8785 S3.2.2.3), and it disagrees with Python's `repr()` at
    several boundaries Python switches fixed/exponential notation at different magnitudes than V8
    does (e.g. `1e-6`/`1e-7`, `1e15`/`1e21`), and prints `-0.0` where JS prints `"0"`.

    Approach: `repr(x)` is Python's own shortest-round-trip decimal string for `x` (same requirement
    ECMAScript imposes: the digit string `s`/`k` must be the shortest that round-trips). Parsing it as
    an exact `Decimal` and stripping trailing zero digits (adjusting the exponent to compensate)
    recovers ECMA-262's `(s, n, k)` -- the minimal significant-digit string and decimal-point
    position -- independent of which fixed/exponential format Python chose to print. ECMA-262's
    formatting rules (6.1.6.1.20 steps 6-10) are then applied directly to `(s, n, k)`.
    """
    if x == 0.0:  # covers both +0.0 and -0.0: ECMAScript ToString(-0) is "0"
        return "0"
    sign = "-" if x < 0 else ""
    digits, exponent = _shortest_round_trip_digits(abs(x))
    k = len(digits)
    n = k + exponent  # value = int(digits) * 10**exponent == 0.digits-shifted * 10**n

    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    exp = n - 1
    mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{mantissa}e{'+' if exp >= 0 else '-'}{abs(exp)}"


def _shortest_round_trip_digits(x: float) -> tuple[str, int]:
    """`x` is a positive finite float. Returns `(digits, exponent)` with no leading/trailing zero in
    `digits` (except the single digit "0", never reached here since x != 0) such that
    `x == int(digits) * 10**exponent` exactly, and `digits` is the shortest such string (inherited
    from `repr(x)`, Python's own shortest-round-trip formatter)."""
    sign, digit_tuple, exponent = Decimal(repr(x)).as_tuple()
    assert sign == 0  # x is positive
    assert isinstance(exponent, int)  # never 'n'/'N'/'F' for a finite Decimal parsed from repr()
    digits = list(digit_tuple)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return "".join(str(d) for d in digits), exponent


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
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"JCS object keys must be strings, got {type(k)!r}")
            if any(ord(ch) >= _NON_BMP_THRESHOLD for ch in k):
                raise CanonicalizationError(
                    f"non-BMP character in object key {k!r}: code-point ordering would not match "
                    "RFC 8785's required UTF-16 code-unit ordering (CORE-002)"
                )
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
