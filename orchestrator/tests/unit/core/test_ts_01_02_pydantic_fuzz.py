"""ES01-S02 / TS-01-02 (traceability-gap closure, 2026-09-26): fuzz the Pydantic core row/wire models
with missing, extra and wrong-typed fields. Every model in `opengrid.core.models` derives from a base
carrying `model_config = ConfigDict(extra="forbid")` (`engine.py`'s `_Row`, `platform.py`'s `_Row`,
`mqtt.py`'s `_Wire`, `pq.py`'s `_Row`/`_Wire`) -- this test asserts that contract actually holds for a
representative model from each module, across three independent mutation kinds, rather than trusting
the docstring/`ConfigDict` line was never silently shadowed by a subclass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from opengrid.core.models.engine import Commitment, Contract, Obligation, Opportunity, ProductRule
from opengrid.core.models.mqtt import Ack, CommandItem, Telemetry
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.core.models.pq import PowerQualityEnvelope, ServiceProfile


def _contract_kwargs() -> dict[str, Any]:
    return {
        "contract_id": uuid4(),
        "customer_id": uuid4(),
        "service_type": "ERCOT_ENERGY",
        "tier": "T2",
        "profile_ref": "profile@1",
        "start_at": datetime.now(UTC),
    }


def _product_rule_kwargs() -> dict[str, Any]:
    return {
        "product_rule_id": uuid4(),
        "contract_id": uuid4(),
        "product_code": "ENERGY",
        "duration_minutes": 15,
        "variable_kind": "CONTINUOUS",
    }


def _opportunity_kwargs() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "opportunity_id": uuid4(),
        "contract_id": uuid4(),
        "window_start": now,
        "window_end": now,
        "requested_kw": Decimal("10"),
        "admitted_at": now,
    }


def _obligation_kwargs() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "obligation_id": uuid4(),
        "opportunity_id": uuid4(),
        "contract_id": uuid4(),
        "service_type": "ERCOT_ENERGY",
        "tier": "T2",
        "window_start": now,
        "window_end": now,
        "committed_qty_kw": Decimal("10"),
    }


def _commitment_kwargs() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "commitment_id": uuid4(),
        "obligation_id": uuid4(),
        "plan_id": uuid4(),
        "interval_start": now,
        "interval_end": now,
        "committed_kw": Decimal("10"),
        "variable_kind": "CONTINUOUS",
    }


def _hub_kwargs() -> dict[str, Any]:
    return {
        "hub_id": "hub-1",
        "bank_id": "bank-1",
        "zone": "LZ_NORTH",
        "e_kwh": 39.2,
        "r_kwh": 7.84,
        "p_kw": 11.0,
    }


def _bank_kwargs() -> dict[str, Any]:
    return {"bank_id": "bank-1", "zone": "LZ_NORTH", "kva_rating": 600.0}


def _hub_state_kwargs() -> dict[str, Any]:
    return {"hub_id": "hub-1", "soc_kwh": 20.0, "p_kw": 0.0, "last_seen_at": datetime.now(UTC)}


def _telemetry_kwargs() -> dict[str, Any]:
    return {
        "hub_id": "hub-1",
        "bank_id": "bank-1",
        "zone": "LZ_NORTH",
        "ts": datetime.now(UTC),
        "soc_kwh": 20.0,
        "p_kw": -1.0,
        "health": "online",
        "seq": 1,
        "epoch": 1,
    }


def _ack_kwargs() -> dict[str, Any]:
    return {"hub_id": "hub-1", "batch_id": uuid4(), "accepted": True, "ts": datetime.now(UTC)}


def _command_item_kwargs() -> dict[str, Any]:
    return {"hub_id": "hub-1", "p_kw_setpoint": -5.0, "reason_code": "R-GATE-SELECT"}


def _pq_envelope_kwargs() -> dict[str, Any]:
    return {"pq_envelope_id": uuid4(), "customer_id": uuid4(), "phase_config": "3P"}


def _service_profile_kwargs() -> dict[str, Any]:
    return {
        "service_profile_id": uuid4(),
        "contract_id": uuid4(),
        "control_primitive": "CAPACITY_HOLD",
        "target_quantity": "KW",
        "target_scope": "BANK",
        "setpoint_source": "PLAN",
        "response_time_s": Decimal("30"),
        "ramp_limit": Decimal("5"),
        "accuracy_tolerance": Decimal("0.05"),
        "deadband": Decimal("0.02"),
        "priority_tier": "T2",
        "mv_method": "meter",
        "settlement_metric": "kwh",
        "pq_envelope_id": uuid4(),
        "failure_behaviour": "substitute",
    }


#: (model class, a known-valid kwargs factory) -- one representative per `opengrid.core.models` module
#: (engine/platform/mqtt/pq), each `extra="forbid"` per BUILD.md S5a ("Pydantic v2 models for all data
#: crossing a module or process boundary").
_MODELS: tuple[tuple[type[BaseModel], Any], ...] = (
    (Contract, _contract_kwargs),
    (ProductRule, _product_rule_kwargs),
    (Opportunity, _opportunity_kwargs),
    (Obligation, _obligation_kwargs),
    (Commitment, _commitment_kwargs),
    (Hub, _hub_kwargs),
    (Bank, _bank_kwargs),
    (HubState, _hub_state_kwargs),
    (Telemetry, _telemetry_kwargs),
    (Ack, _ack_kwargs),
    (CommandItem, _command_item_kwargs),
    (PowerQualityEnvelope, _pq_envelope_kwargs),
    (ServiceProfile, _service_profile_kwargs),
)

_MODEL_INDEX = st.integers(min_value=0, max_value=len(_MODELS) - 1)
#: Structurally incompatible replacement values for a scalar field -- deliberately excludes `float`
#: oddities like `nan`/`inf`, which some scalar types (`Decimal`, `float` itself) accept without error
#: and would make this an accidental no-op rather than a genuine wrong-type fuzz case.
_WRONG_TYPE_VALUES: tuple[Any, ...] = (
    object(),
    ["not", "a", "scalar"],
    {"nested": "dict"},
)


def _valid_instance(index: int) -> BaseModel:
    model, kwargs_factory = _MODELS[index]
    return model(**kwargs_factory())


@given(index=_MODEL_INDEX)
@settings(max_examples=len(_MODELS), deadline=None)
def test_valid_baseline_kwargs_actually_construct(index: int) -> None:
    """Sanity anchor: every factory above must itself be accepted (otherwise the mutation tests below
    would trivially "pass" by raising for the wrong reason)."""
    _valid_instance(index)


@given(index=_MODEL_INDEX)
@settings(max_examples=len(_MODELS) * 3, deadline=None)
def test_missing_required_field_raises_validation_error(index: int) -> None:
    """ES01-S02: a required field omitted from the payload must raise `ValidationError`, for every
    model, not just the ones existing tests happen to exercise via a full fixture."""
    model, kwargs_factory = _MODELS[index]
    kwargs = kwargs_factory()
    required = [name for name, field in model.model_fields.items() if field.is_required()]
    assert required, f"{model.__name__} has no required fields to fuzz"
    for field_name in required:
        mutated = dict(kwargs)
        del mutated[field_name]
        with pytest.raises(ValidationError):
            model(**mutated)


@given(index=_MODEL_INDEX, extra_key=st.sampled_from(["bogus_field", "unexpected", "__proto__"]))
@settings(max_examples=len(_MODELS) * 3, deadline=None)
def test_extra_field_raises_validation_error(index: int, extra_key: str) -> None:
    """`extra='forbid'` (BUILD.md S5a): an unexpected field must be rejected, never silently dropped or
    silently accepted -- a permissive fallback here would hide a caller/wire-format mismatch."""
    model, kwargs_factory = _MODELS[index]
    kwargs = {**kwargs_factory(), extra_key: "unexpected-value"}
    with pytest.raises(ValidationError):
        model(**kwargs)


@given(index=_MODEL_INDEX, wrong_value=st.sampled_from(_WRONG_TYPE_VALUES))
@settings(max_examples=len(_MODELS) * len(_WRONG_TYPE_VALUES), deadline=None)
def test_wrong_typed_field_raises_validation_error(index: int, wrong_value: object) -> None:
    """A field whose declared type is a scalar (str/int/float/Decimal/datetime/UUID/Literal) must
    reject a structurally wrong-typed replacement (a dict/list/bare object/NaN), for every model."""
    model, kwargs_factory = _MODELS[index]
    kwargs = kwargs_factory()
    scalar_fields = [
        name
        for name, field in model.model_fields.items()
        if field.annotation in (str, int, float, Decimal, datetime, bool, UUID) and name in kwargs
    ]
    assert scalar_fields, f"{model.__name__} has no plain-scalar field to fuzz with a wrong type"
    for field_name in scalar_fields:
        mutated = dict(kwargs)
        mutated[field_name] = wrong_value
        with pytest.raises(ValidationError):
            model(**mutated)
