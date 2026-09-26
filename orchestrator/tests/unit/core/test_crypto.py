import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core.crypto import (
    CanonicalizationError,
    canonicalize_json,
    generate_keypair,
    sha256_hex_of_json,
    sign_payload,
    verify_payload,
)


def test_jcs_simple_object_matches_rfc8785_shape():
    assert canonicalize_json({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


def test_jcs_sorts_keys_regardless_of_input_order():
    assert canonicalize_json({"b": 2, "a": 1}) == canonicalize_json({"a": 1, "b": 2})


def test_jcs_nested_and_arrays():
    obj = {"z": [1, 2, {"y": "x"}], "a": None, "flag": True}
    out = canonicalize_json(obj)
    assert out == b'{"a":null,"flag":true,"z":[1,2,{"y":"x"}]}'


def test_jcs_unicode_string():
    assert canonicalize_json({"s": "café"}) == '{"s":"café"}'.encode()


def test_jcs_rejects_bool_as_number_path_never_reached_from_canon_value():
    # bool is routed to true/false in _canon_value before _canon_number ever sees it.
    assert canonicalize_json(True) == b"true"
    assert canonicalize_json(False) == b"false"


def test_jcs_rejects_nan_and_infinity():
    with pytest.raises(ValueError, match="NaN/Infinity"):
        canonicalize_json(math.nan)
    with pytest.raises(ValueError, match="NaN/Infinity"):
        canonicalize_json(math.inf)


def test_jcs_non_integral_float_uses_repr():
    assert canonicalize_json(1.5) == b"1.5"


def test_jcs_rejects_unsupported_type():
    with pytest.raises(TypeError, match="unsupported JCS value type"):
        canonicalize_json(object())


def test_jcs_escapes_control_and_special_characters():
    raw = 'a"b\\c\bd\fe\nf\rg\th\x01i'
    expected = b'"a\\"b\\\\c\\bd\\fe\\nf\\rg\\th\\u0001i"'
    assert canonicalize_json(raw) == expected


def test_jcs_list_of_mixed_types():
    assert canonicalize_json([1, "a", None, True, 2.0]) == b'[1,"a",null,true,2]'


def test_sign_and_verify_roundtrip():
    seed, pub = generate_keypair()
    payload = {"batch_id": "abc", "epoch": 1, "seq": 2}
    sig = sign_payload(seed, payload)
    assert verify_payload(pub, payload, sig)


def test_verify_fails_on_tampered_payload():
    seed, pub = generate_keypair()
    payload = {"epoch": 1}
    sig = sign_payload(seed, payload)
    assert not verify_payload(pub, {"epoch": 2}, sig)


def test_verify_fails_on_wrong_key():
    seed, _pub = generate_keypair()
    _seed2, pub2 = generate_keypair()
    payload = {"epoch": 1}
    sig = sign_payload(seed, payload)
    assert not verify_payload(pub2, payload, sig)


def test_verify_fails_gracefully_on_malformed_signature():
    _seed, pub = generate_keypair()
    assert not verify_payload(pub, {"a": 1}, "not-base64!!")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # CORE-001: ECMAScript Number::toString boundaries, where Python's repr() picks a different
        # fixed/exponential threshold than V8 (JS switches to exponential below 1e-6 and at/above
        # 1e21; Python's repr() switches around 1e-5 and 1e16-1e17) -- pinned against hand-computed
        # ECMA-262 6.1.6.1.20 output, independent of this module's own implementation.
        (1e-7, "1e-7"),
        (1e-6, "0.000001"),
        (1e-5, "0.00001"),
        (0.1, "0.1"),
        (1e15, "1000000000000000"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (-0.0, "0"),
        (0.0, "0"),
        (-1e-7, "-1e-7"),
        (123.456, "123.456"),
    ],
)
def test_ecmascript_number_canonicalization_boundaries(value, expected):
    assert canonicalize_json(value) == expected.encode()


def test_canonicalize_json_rejects_non_bmp_object_key():
    # U+1F600 (an astral/non-BMP code point) as an object key: code-point sort order would not match
    # RFC 8785's required UTF-16-code-unit order (CORE-002), so this is refused rather than silently
    # mis-ordered.
    with pytest.raises(CanonicalizationError, match="non-BMP"):
        canonicalize_json({"\U0001f600": 1})


def test_canonicalize_json_accepts_bmp_keys_up_to_ffff():
    # U+FFFF is the highest BMP code point -- must still be accepted.
    assert canonicalize_json({"￿": 1}) == '{"￿":1}'.encode()


def test_canonicalize_json_cross_check_against_ogsim_jcs_fixture():
    """CORE-001: cross-check against `ogsim.common.jcs`'s output for a realistic command-batch-shaped
    payload (interfaces/crypto.md S2.1), WITHOUT importing `ogsim` (BUILD.md S1: `opengrid` never
    imports `ogsim`). The expected bytes below were derived by hand from `ogsim`'s own
    `_encode`/`_encode_number` (integers as `str(value)`; a non-integral float as `repr(x)`; `-0.0`/
    `0.0` as `"0"`) for values within the range `ogsim`'s docstring itself guarantees exactness for
    (hub/bank ids, epochs/seqs, small setpoint floats) -- both implementations must agree byte-for-byte
    on this shape, since it is exactly the wire traffic `interfaces/crypto.md` governs for both sides.
    """
    payload = {
        "batch_id": "0190f7b0-aaaa-bbbb-cccc-000000000001",
        "bank_id": "bank-07",
        "epoch": 42,
        "seq": 1183,
        "issued_at": "2026-09-26T18:00:02.104Z",
        "expires_at": "2026-09-26T18:00:12.104Z",
        "items": [{"hub_id": "hub-0007", "p_kw_setpoint": -3.2, "reason_code": "SELECTOR"}],
    }
    expected = (
        b'{"bank_id":"bank-07","batch_id":"0190f7b0-aaaa-bbbb-cccc-000000000001",'
        b'"epoch":42,"expires_at":"2026-09-26T18:00:12.104Z","issued_at":"2026-09-26T18:00:02.104Z",'
        b'"items":[{"hub_id":"hub-0007","p_kw_setpoint":-3.2,"reason_code":"SELECTOR"}],"seq":1183}'
    )
    assert canonicalize_json(payload) == expected


def test_sha256_hex_of_json_is_deterministic():
    assert sha256_hex_of_json({"a": 1, "b": 2}) == sha256_hex_of_json({"b": 2, "a": 1})


_BMP_KEY_TEXT = st.text(
    alphabet=st.characters(max_codepoint=0xFFFF, blacklist_categories=("Cs",)), min_size=1, max_size=5
)


# No deadline: Ed25519 signing is CPU-bound and the full suite runs loaded; this is a correctness property.
@settings(deadline=None)
@given(st.dictionaries(_BMP_KEY_TEXT, st.integers(min_value=-1000, max_value=1000)))
def test_sign_verify_property(payload):
    # CORE-002: keys are restricted to the Basic Multilingual Plane (surrogates excluded) -- a
    # non-BMP key is a `CanonicalizationError`, covered separately below, not a valid payload here.
    seed, pub = generate_keypair()
    sig = sign_payload(seed, payload)
    assert verify_payload(pub, payload, sig)
