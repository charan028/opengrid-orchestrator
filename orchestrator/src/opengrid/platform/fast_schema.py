"""A precompiled accept-check for the high-volume MQTT schemas (r3.4.4 PERF-OPT; telemetry: ~750 msg/s at 7,500
hubs). `jsonschema`'s `Validator.validate` costs ~250 us per telemetry message on the engine's event loop; the
predicate compiled here answers "valid?" in a few microseconds.

It is a pre-check, never the authority for a rejection: `platform.mqtt.validate_payload` accepts on its `True`
and runs the reference `jsonschema` validator on anything else (`False`, or any exception), so every rejection --
and its error message -- is still `jsonschema`'s own. The one direction that matters, "accepts only what
`jsonschema` accepts", is proven by the differential test `tests/unit/platform/test_fast_schema.py` (every schema
fixture plus a seeded 10k-case fuzz per schema, both directions must agree).

Built at import time from the schema file itself (no generated source, no hand-written per-schema code), and only
for what it can mirror exactly -- `compile_schema` returns `None` (the caller keeps `jsonschema` alone) for:
- any validator other than draft 2020-12 with its stock type checker, or one that asserts formats;
- any keyword outside `type`, `properties`, `required`, `additionalProperties` (a boolean), `minLength`,
  `maxLength`, `minimum`, `maximum` and a string-only `enum`, besides pure annotations (`$schema`, `$id`, `title`,
  `description`, `$comment`, `examples`, `default`, and `format`, which draft 2020-12 treats as an annotation
  unless a format checker is configured -- the reference validator here has none).

Each keyword mirrors `jsonschema._keywords` (4.x) expression for expression: it applies only to instances of its
own type, `minimum` fails on `instance < minimum` (so NaN passes, exactly as there), `enum` compares with `==`,
`integer` includes integral floats, and `bool` is never a number.
"""

from __future__ import annotations

import numbers
from collections.abc import Callable, Mapping
from typing import Any

import jsonschema

Check = Callable[[Any], bool]

#: Keywords that never change validity in draft 2020-12 without a format checker.
_ANNOTATIONS = frozenset(
    {"$schema", "$id", "title", "description", "$comment", "examples", "default", "format"}
)
_SUPPORTED = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
    }
)


class _UnsupportedError(Exception):
    """The schema uses something this compiler does not mirror exactly."""


def _is_number(x: Any) -> bool:
    return not isinstance(x, bool) and isinstance(x, numbers.Number)


def _is_integer(x: Any) -> bool:
    if isinstance(x, bool):
        return False
    return isinstance(x, int) or (isinstance(x, float) and x.is_integer())


_TYPE_PREDICATES: dict[str, Check] = {
    "string": lambda x: isinstance(x, str),
    "number": _is_number,
    "integer": _is_integer,
    "object": lambda x: isinstance(x, dict),
    "array": lambda x: isinstance(x, list),
    "boolean": lambda x: isinstance(x, bool),
    "null": lambda x: x is None,
}


def compile_schema(schema: Mapping[str, Any], validator: jsonschema.protocols.Validator) -> Check | None:
    """The accept predicate for `schema` as `validator` (its reference `jsonschema` validator) judges it, or `None`
    when it cannot be mirrored exactly (see the module docstring)."""
    if type(validator) is not jsonschema.Draft202012Validator:
        return None
    if validator.format_checker is not None:
        return None
    if validator.TYPE_CHECKER is not jsonschema.Draft202012Validator.TYPE_CHECKER:
        return None
    try:
        return _compile(schema)
    except _UnsupportedError:
        return None


def _type_check(names: list[str]) -> Check:
    """`type`: plain `isinstance` for the classes that map one-to-one, the number rules otherwise."""
    simple = tuple(
        {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}[n]
        for n in names
        if n in ("string", "object", "array", "boolean", "null")
    )
    number, integer = "number" in names, "integer" in names

    def _type(x: Any) -> bool:
        if isinstance(x, simple):
            return True
        if number and _is_number(x):
            return True
        return integer and _is_integer(x)

    return _type


def _compile(schema: Any) -> Check:
    if not isinstance(schema, dict):
        raise _UnsupportedError("boolean or non-object subschema")
    unknown = set(schema) - _SUPPORTED - _ANNOTATIONS
    if unknown:
        raise _UnsupportedError(f"keywords {sorted(unknown)}")

    type_check: Check | None = None
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not names or any(n not in _TYPE_PREDICATES for n in names):
            raise _UnsupportedError("type")
        type_check = _type_check(names)

    enum: tuple[str, ...] | None = None
    if "enum" in schema:
        members = schema["enum"]
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise _UnsupportedError("enum")
        enum = tuple(members)

    min_len = _non_negative_int(schema["minLength"]) if "minLength" in schema else None
    max_len = _non_negative_int(schema["maxLength"]) if "maxLength" in schema else None
    low = _bound(schema["minimum"]) if "minimum" in schema else None
    high = _bound(schema["maximum"]) if "maximum" in schema else None

    required: tuple[str, ...] = ()
    if "required" in schema:
        if not isinstance(schema["required"], list) or not all(
            isinstance(r, str) for r in schema["required"]
        ):
            raise _UnsupportedError("required")
        required = tuple(schema["required"])

    items: tuple[tuple[str, Check], ...] = ()
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise _UnsupportedError("properties")
        items = tuple((name, _compile(sub)) for name, sub in schema["properties"].items())

    allowed: frozenset[str] | None = None
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            raise _UnsupportedError("additionalProperties subschema")
        if not extra:
            allowed = frozenset(schema.get("properties", {}))
    object_rules = bool(required or items or allowed is not None)

    def _check(x: Any) -> bool:
        if type_check is not None and not type_check(x):
            return False
        if enum is not None:
            for each in enum:  # jsonschema: `equal(each, x)`, which is `each == x` for a str member
                if each == x:
                    break
            else:
                return False
        if isinstance(x, str):
            if min_len is not None and len(x) < min_len:
                return False
            if max_len is not None and len(x) > max_len:
                return False
        elif isinstance(x, dict):
            if object_rules:
                for name in required:
                    if name not in x:
                        return False
                if allowed is not None:
                    for key in x:
                        if key not in allowed:
                            return False
                for name, sub in items:
                    if name in x and not sub(x[name]):
                        return False
        elif (low is not None or high is not None) and _is_number(x):
            if low is not None and x < low:
                return False
            if high is not None and x > high:
                return False
        return True

    return _check


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _UnsupportedError("length bound")
    return value


def _bound(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _UnsupportedError("numeric bound")
    return value
