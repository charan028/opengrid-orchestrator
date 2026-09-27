"""r3.4.4 PERF-OPT: the precompiled accept-check (`platform.fast_schema`) agrees with the reference `jsonschema`
validator on every payload -- both directions: it accepts exactly what `jsonschema` accepts and rejects exactly what
it rejects. Any divergence, including a stricter one, fails.

For every `interfaces/mqtt/*.schema.json` the compiler supports (the ones `validate_payload` pre-checks, and any
other it could): a hand-written corpus (telemetry: every optional field, nulls, pq, and each rejection rule), then
a seeded differential fuzz of `FUZZ_CASES` mutated payloads per schema -- type swaps, missing and extra keys, bool
vs number, int vs float, boundary, NaN/infinite and huge numbers, unicode/control characters, garbage date-times,
enum near-misses, nulls, deep nesting and non-object documents. `validate_payload` itself must raise the same
error message as the reference on every rejected case.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from opengrid.platform import fast_schema, mqtt
from opengrid.platform.fast_schema import compile_schema

FUZZ_CASES = 10_000
SEED = 20260927
SCHEMA_DIR = mqtt.INTERFACES_MQTT_DIR


def _schemas() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.schema.json"))


def _reference(path: Path) -> tuple[dict[str, Any], jsonschema.protocols.Validator]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    return schema, cls(schema)


def _compiled(path: Path) -> Any:
    _schema, validator = _reference(path)
    return compile_schema(validator)


SUPPORTED = [p for p in _schemas() if _compiled(p) is not None]


def _verdict(check: Any, payload: Any) -> bool:
    try:
        return bool(check(payload))
    except Exception:  # a crash is never an accept
        return False


# ----------------------------------------------------------------------------------------- generators
_ODD_STRINGS = ["", "a", " ", "\x00", "\x1f\x7f", "‮​", "онлайн", "🔋⚡", "a" * 5_000, "null", "0", "1e3"]
_ODD_NUMBERS: list[Any] = [
    0,
    -0.0,
    1,
    -1,
    0.5,
    -0.5,
    1.0,
    5.0,
    1e-300,
    -1e-300,
    1e308,
    -1e308,
    math.inf,
    -math.inf,
    math.nan,
    10**400,
    -(10**400),
    2**63,
    -(2**63),
    90,
    90.0000001,
    -90,
    -90.0000001,
    180,
    -180.0000001,
    1 - 1e-16,
]
_ODD_VALUES: list[Any] = [None, True, False, [], [1], {}, {"a": 1}, *_ODD_NUMBERS, *_ODD_STRINGS]


def _deep(depth: int) -> Any:
    value: Any = 1
    for i in range(depth):
        value = [value] if i % 2 else {"k": value}
    return value


def _valid(rng: random.Random, schema: dict[str, Any]) -> Any:
    types = schema.get("type", "object")
    types = types if isinstance(types, list) else [types]
    kind = rng.choice([t for t in types if t != "null"] or types) if rng.random() < 0.9 else rng.choice(types)
    if "enum" in schema:
        return rng.choice(schema["enum"])
    low, high = schema.get("minimum", -1_000), schema.get("maximum", 1_000)
    if kind == "null":
        return None
    if kind == "boolean":
        return rng.random() < 0.5
    if kind == "integer":
        return rng.randint(math.ceil(low), max(math.ceil(low), math.floor(min(high, low + 10**6))))
    if kind == "number":
        return rng.uniform(low, min(high, low + 1e4))
    if kind == "string":
        if schema.get("format") == "date-time":
            return "2026-09-27T03:00:00Z"
        n = rng.randint(
            schema.get("minLength", 0), min(schema.get("maxLength", 12), schema.get("minLength", 0) + 12)
        )
        return "".join(rng.choice("abcXYZ-_09é") for _ in range(n))
    if kind == "array":
        return []
    props = schema.get("properties", {})
    required = schema.get("required", [])
    out: dict[str, Any] = {}
    for name, sub in props.items():
        if name in required or rng.random() < 0.5:
            out[name] = _valid(rng, sub)
    return out


def _mutate(rng: random.Random, schema: dict[str, Any], payload: Any) -> Any:
    if not isinstance(payload, dict) or rng.random() < 0.02:
        return rng.choice([[], [payload], "x", None, 1, 1.5, True, _deep(rng.randint(1, 60))])
    out = dict(payload)
    props = schema.get("properties", {})
    op = rng.randrange(8)
    if op == 0 and out:
        out.pop(rng.choice(sorted(out)))
    elif op == 1:
        name = rng.choice(["extra", "Hub_id", "", "hub_id ", "\u0000", "pq_", "ts2", *sorted(props)])
        out[name] = rng.choice(_ODD_VALUES)
    elif op == 2 and props:
        out[rng.choice(sorted(props))] = rng.choice(_ODD_VALUES)
    elif op == 3 and props:
        name = rng.choice(sorted(props))
        sub = props[name]
        # Either bound of a field (C1: a field with a minimum AND a maximum has both exercised).
        bounds = [sub[key] for key in ("minimum", "maximum") if key in sub]
        bound = rng.choice(bounds) if bounds else None
        if bound is not None:
            eps = rng.choice([0, 1e-12, 1e-9, 1, 0.5])
            out[name] = rng.choice([bound, bound - eps, bound + eps, float(bound), int(bound)])
        elif "enum" in sub:
            each = rng.choice(sub["enum"])
            out[name] = rng.choice(
                [each.upper(), each + " ", " " + each, each[:-1], each, each.encode().decode()]
            )
        else:
            out[name] = rng.choice(_ODD_VALUES)
    elif op == 4 and props:
        name = rng.choice(sorted(props))
        sub = props[name]
        if sub.get("format") == "date-time":
            out[name] = rng.choice(["", "yesterday", "2026-13-40T99:99:99Z", "\x00", "2026-09-27", 17])
        elif isinstance(out.get(name), (int, float)) and not isinstance(out.get(name), bool):
            value = out[name]
            finite = isinstance(value, int) or math.isfinite(value)
            as_float = float(value) if not isinstance(value, int) or abs(value) < 2**1000 else value
            out[name] = rng.choice([as_float, int(value) if finite else value, str(value), True])
        else:
            out[name] = rng.choice(_ODD_STRINGS)
    elif op == 5 and props:
        name = rng.choice(sorted(props))
        sub = props[name]
        if isinstance(sub.get("properties"), dict):
            out[name] = _mutate(rng, sub, _valid(rng, {**sub, "type": "object"}))
        else:
            out[name] = _deep(rng.randint(1, 80))
    elif op == 6:
        out = _valid(rng, schema)
    else:
        for name in sorted(props):
            if rng.random() < 0.3:
                out[name] = None
    return out


def _fuzz(schema: dict[str, Any]) -> Iterator[Any]:
    rng = random.Random(SEED)  # noqa: S311 -- seeded fuzz input, not security
    for _ in range(FUZZ_CASES):
        payload = _valid(rng, schema)
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            payload = _mutate(rng, schema, payload)
        yield payload


# --------------------------------------------------------------------------------- telemetry corpus
_TEL = {
    "hub_id": "hub-00001",
    "bank_id": "bank-001",
    "zone": "LZ_NORTH",
    "ts": "2026-09-27T03:00:00+00:00",
    "soc_kwh": 20.5,
    "p_kw": -3.2,
    "health": "online",
    "seq": 7,
    "epoch": 1,
}
_FULL = {
    **_TEL,
    "fault_code": None,
    "home_load_kw": 1.2,
    "pv_kw": 0,
    "meter_kw": -2.0,
    "cell_temp_c": 31.5,
    "p_dis_max_kw": 11,
    "p_ch_max_kw": 11.0,
    "peak_power_budget_kws": 0,
    "lat": 32.7,
    "lon": -96.8,
    "charge_pv_kw": 0.0,
    "charge_grid_kw": 0.0,
    "pq": {
        "v_rms": 240.1,
        "i_rms": 3.1,
        "q_kvar": 0.2,
        "freq_hz": 60.0,
        "pf": -1,
        "thd_v_pct": 1.1,
        "phase": "AB",
    },
}
TELEMETRY_CORPUS: list[Any] = [
    _TEL,
    _FULL,
    {**_FULL, **{k: None for k in _FULL if k not in _TEL}},
    {**_TEL, "health": "fault", "fault_code": "BMS_FAULT"},
    {**_TEL, "seq": 0, "epoch": 0, "soc_kwh": 0},
    {**_TEL, "seq": 5.0},  # an integral float is an integer (draft 2020-12)
    {**_TEL, "soc_kwh": math.nan},  # NaN is not < 0: jsonschema accepts it
    {**_TEL, "p_kw": math.inf},
    {**_TEL, "ts": "not a date"},  # format is an annotation: accepted
    {**_TEL, "pq": {}},
    {**_TEL, "lat": 90, "lon": -180},
    # rejections
    {k: v for k, v in _TEL.items() if k != "epoch"},
    {**_TEL, "health": "Online"},
    {**_TEL, "hub_id": ""},
    {**_TEL, "soc_kwh": -0.001},
    {**_TEL, "seq": 5.5},
    {**_TEL, "seq": True},
    {**_TEL, "p_kw": "1"},
    {**_TEL, "p_kw": None},
    {**_TEL, "unknown": 1},
    {**_TEL, "lat": 90.5},
    {**_TEL, "pq": {"pf": 1.01}},
    {**_TEL, "pq": {"phase": "D"}},
    {**_TEL, "pq": {"extra": 1}},
    {**_TEL, "pq": []},
    {**_TEL, "fault_code": 3},
    [],
    None,
    "telemetry",
]


# ------------------------------------------------------------------------------------------ tests
def test_telemetry_is_compiled_and_prechecked() -> None:
    assert SCHEMA_DIR / "telemetry.schema.json" in SUPPORTED
    assert mqtt._fast_check_for("telemetry") is not None


@pytest.mark.parametrize("payload", TELEMETRY_CORPUS, ids=range(len(TELEMETRY_CORPUS)))
def test_the_telemetry_corpus_is_judged_alike(payload: Any) -> None:
    _schema, validator = _reference(SCHEMA_DIR / "telemetry.schema.json")
    check = _compiled(SCHEMA_DIR / "telemetry.schema.json")
    assert _verdict(check, payload) == validator.is_valid(payload)
    _same_validate_payload_outcome("telemetry", validator, payload)


@pytest.mark.parametrize("path", SUPPORTED, ids=lambda p: p.name)
def test_a_seeded_differential_fuzz_agrees_both_ways(path: Path) -> None:
    schema, validator = _reference(path)
    check = _compiled(path)
    kind = next((k for k, f in mqtt._SCHEMA_BY_KIND.items() if f == path.name), None)
    accepted = rejected = 0
    for payload in _fuzz(schema):
        expected = validator.is_valid(payload)
        assert _verdict(check, payload) == expected, repr(payload)[:500]
        accepted += expected
        rejected += not expected
        if kind in mqtt.FAST_CHECK_KINDS and not expected:
            _same_validate_payload_outcome(kind, validator, payload)
    # Not vacuous: both verdicts are well represented.
    assert accepted > FUZZ_CASES // 10 and rejected > FUZZ_CASES // 10, (accepted, rejected)


def _same_validate_payload_outcome(kind: str, validator: Any, payload: Any) -> None:
    """`validate_payload` (fast pre-check + reference) raises exactly what the reference alone raises."""
    try:
        validator.validate(instance=payload)
        expected = None
    except jsonschema.ValidationError as exc:
        expected = f"{kind}: {exc.message}"
    try:
        mqtt.validate_payload(kind, payload)
        got = None
    except mqtt.SchemaValidationError as exc:
        got = str(exc)
    assert got == expected


def test_a_crashing_precheck_is_never_an_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(payload: Any) -> bool:
        raise RuntimeError("bug")

    monkeypatch.setattr(mqtt, "_fast_check_for", lambda kind: _boom)
    with pytest.raises(mqtt.SchemaValidationError):
        mqtt.validate_payload("telemetry", {**_TEL, "health": "bogus"})
    mqtt.validate_payload("telemetry", _TEL)  # the reference still accepts a valid one


def test_unsupported_schemas_and_validators_are_not_compiled() -> None:
    base = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    v = jsonschema.Draft202012Validator
    assert compile_schema(v({**base, "pattern": "x"})) is None
    assert compile_schema(v({**base, "additionalProperties": {"type": "string"}})) is None
    assert compile_schema(v({**base, "enum": [1, 2]})) is None
    assert compile_schema(v(base, format_checker=v.FORMAT_CHECKER)) is None
    assert compile_schema(jsonschema.Draft7Validator(base)) is None
    nested_dialect = {**base, "properties": {"a": {"$schema": "http://json-schema.org/draft-07/schema#"}}}
    assert compile_schema(v(nested_dialect)) is None
    assert compile_schema(v({**base, "properties": {"a": {"$id": "urn:x", "type": "string"}}})) is None
    assert compile_schema(v(base)) is not None


# ------------------------------------------------------------------------------------ planted bugs (C2)
#: Deliberate bugs in `fast_schema` (source text -> buggy text). Each must make the differential check diverge
#: from `jsonschema` somewhere -- otherwise the equivalence tests above could not catch that regression.
PLANTED_BUGS = [
    ("x < low:", "x <= low:"),
    ("x > high:", "x > high + 1e-9:"),  # SAFETY C1: accepts just above a maximum
    (
        "return not isinstance(x, bool) and isinstance(x, numbers.Number)",
        "return isinstance(x, numbers.Number)",
    ),
    ("or (isinstance(x, float) and x.is_integer())", ""),
    ("len(x) < min_len:", "len(x) <= min_len:"),
    ("if key not in allowed:", "if key not in allowed and False:"),
    ("if each == x:", "if each == str(x).lower():"),
    ("if each == x:", "if isinstance(x, str) and each.startswith(x):"),  # enum prefix match
    ("if name not in x:", "if name not in x and False:"),
    ("if name in x and not sub(x[name]):", "if name in x and not sub(x[name]) and x[name] is not None:"),
    ("if number and _is_number(x):", "if number and isinstance(x, (int, float)):"),
    ("if isinstance(x, simple):", "if x is None or isinstance(x, simple):"),  # null accepted for any type
]
PLANTED_FUZZ_CASES = 3_000


def _load_mutant(tmp_path: Path, old: str, new: str) -> Any:
    source = Path(fast_schema.__file__).read_text(encoding="utf-8")
    assert source.count(old) >= 1, old
    path = tmp_path / "fast_schema_mutant.py"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fast_schema_mutant", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diverges(mutant: Any) -> bool:
    """Whether the mutant disagrees with jsonschema on the corpus or a (shorter) seeded fuzz of each schema."""
    for path in SUPPORTED:
        schema, validator = _reference(path)
        check = mutant.compile_schema(validator)
        if check is None:
            continue
        cases: list[Any] = list(TELEMETRY_CORPUS) if path.name == "telemetry.schema.json" else []
        cases.extend(itertools.islice(_fuzz(schema), PLANTED_FUZZ_CASES))
        if any(_verdict(check, payload) != validator.is_valid(payload) for payload in cases):
            return True
    return False


@pytest.mark.parametrize(("old", "new"), PLANTED_BUGS, ids=[new for _, new in PLANTED_BUGS])
def test_every_planted_bug_is_caught(tmp_path: Path, old: str, new: str) -> None:
    assert _diverges(_load_mutant(tmp_path, old, new))


def test_the_unmutated_compiler_passes_the_same_check(tmp_path: Path) -> None:
    assert not _diverges(_load_mutant(tmp_path, "x < low:", "x < low:"))
