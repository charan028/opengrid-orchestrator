"""ogsim.common.jcs -- RFC 8785 JSON Canonicalization Scheme (JCS).

Implements interfaces/crypto.md §1's canonicalization rule: sorted object
keys, compact separators (no insignificant whitespace), ECMAScript-style
number formatting. Ed25519 signatures in this system are always computed
over these bytes, never over a plain `json.dumps(sort_keys=True)` (which
does not match ECMAScript number-to-string, e.g. `1.0` -> `"1.0"` instead
of JCS's `"1"`).

Key ordering: JCS sorts keys by UTF-16 code unit. Python's default string
comparison sorts by Unicode code point, which is identical to UTF-16 code
unit order for every character in the Basic Multilingual Plane. The
message shapes this module canonicalizes (hub/bank ids, enum strings,
UUIDs, RFC3339 timestamps) never contain characters outside the BMP, so
plain `sorted()` is sufficient here; this is called out rather than
silently assumed.

Number formatting: integers are printed as-is. Floats use `repr()`, which
produces the shortest decimal string that round-trips to the same double
-- the same guarantee ECMAScript's Number::toString relies on -- after
first checking for an exact integral value (JCS/JS print `2.0` as `2`).
This is not a byte-for-byte certified JCS float formatter for every
possible double (e.g. very large magnitudes needing exponential form),
but it is exact for the numeric shapes actually used on this wire:
p_kw setpoints, epochs/seqs (ints), and small config-range floats.
"""

from __future__ import annotations

import json
import math
from typing import Any


def canonicalize(value: Any) -> str:
    """Serializes `value` to its JCS string form."""
    return _encode(value)


def canonicalize_bytes(value: Any) -> bytes:
    """Serializes `value` to its JCS UTF-8 bytes, ready to sign/verify."""
    return canonicalize(value).encode("utf-8")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # must precede int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_encode(v)}" for k, v in items) + "}"
    raise TypeError(f"unsupported type for JCS encoding: {type(value)!r}")


def _encode_number(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise ValueError("JCS cannot encode NaN or Infinity")
    if x == 0.0:
        return "0"
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    return repr(x)
