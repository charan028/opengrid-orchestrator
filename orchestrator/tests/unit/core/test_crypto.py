import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.crypto import (
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


def test_sha256_hex_of_json_is_deterministic():
    assert sha256_hex_of_json({"a": 1, "b": 2}) == sha256_hex_of_json({"b": 2, "a": 1})


@given(st.dictionaries(st.text(min_size=1, max_size=5), st.integers(min_value=-1000, max_value=1000)))
def test_sign_verify_property(payload):
    seed, pub = generate_keypair()
    sig = sign_payload(seed, payload)
    assert verify_payload(pub, payload, sig)
