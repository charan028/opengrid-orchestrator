"""Tests for ogsim.common.jcs, including crypto.md §4's reference vector."""

from __future__ import annotations

import pytest

from ogsim.common.jcs import canonicalize, canonicalize_bytes


def test_reference_vector_from_crypto_md() -> None:
    assert canonicalize({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_keys_are_sorted_regardless_of_input_order() -> None:
    assert canonicalize({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_nested_objects_and_lists() -> None:
    value = {"z": [1, 2, {"y": "x"}], "a": None}
    assert canonicalize(value) == '{"a":null,"z":[1,2,{"y":"x"}]}'


def test_booleans_are_not_encoded_as_integers() -> None:
    assert canonicalize({"a": True, "b": False}) == '{"a":true,"b":false}'


def test_integral_float_prints_without_trailing_zero() -> None:
    # JCS/ECMAScript number formatting: 2.0 -> "2", not "2.0".
    assert canonicalize(2.0) == "2"


def test_non_integral_float_round_trips() -> None:
    assert canonicalize(-3.2) == "-3.2"


def test_negative_zero_prints_as_zero() -> None:
    assert canonicalize(-0.0) == "0"


def test_string_escaping() -> None:
    assert canonicalize('a"b') == '"a\\"b"'


def test_canonicalize_bytes_is_utf8() -> None:
    assert canonicalize_bytes({"a": 1}) == b'{"a":1}'


def test_nan_rejected() -> None:
    with pytest.raises(ValueError, match="NaN"):
        canonicalize(float("nan"))


def test_unsupported_type_rejected() -> None:
    with pytest.raises(TypeError):
        canonicalize(object())
