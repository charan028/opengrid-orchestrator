"""Tests for validating `<root>/scenario/cmd` messages against the
architect's published `interfaces/mqtt/scenario_control.schema.json`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ogsim.control.schema_validation import ScenarioCmdValidationError, _load_schema, validate_scenario_cmd

VALID_MESSAGE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "target": {"kind": "bank", "ref": "BANK_01"},
    "type": "SCADA_BANK_OVERLOAD",
    "params": {},
    "start": "2026-09-26T18:00:00+00:00",
    "duration_s": 60,
}


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    _load_schema.cache_clear()
    yield
    _load_schema.cache_clear()


def test_the_real_published_schema_is_found():
    assert _load_schema() is not None


def test_a_well_formed_scada_message_passes():
    validate_scenario_cmd(VALID_MESSAGE)  # must not raise


def test_a_well_formed_fleet_message_passes():
    message = {**VALID_MESSAGE, "target": {"kind": "hub", "ref": "HUB_0142"}, "type": "FLEET_HUB_OFFLINE"}
    validate_scenario_cmd(message)  # must not raise


def test_missing_required_field_fails():
    message = {k: v for k, v in VALID_MESSAGE.items() if k != "start"}
    with pytest.raises(ScenarioCmdValidationError):
        validate_scenario_cmd(message)


def test_unknown_type_enum_value_fails():
    message = {**VALID_MESSAGE, "type": "not_a_real_wire_type"}
    with pytest.raises(ScenarioCmdValidationError):
        validate_scenario_cmd(message)


def test_target_must_be_an_object_not_a_bare_string():
    message = {**VALID_MESSAGE, "target": "BANK_01"}
    with pytest.raises(ScenarioCmdValidationError):
        validate_scenario_cmd(message)


def test_unknown_target_kind_fails():
    message = {**VALID_MESSAGE, "target": {"kind": "planet", "ref": "BANK_01"}}
    with pytest.raises(ScenarioCmdValidationError):
        validate_scenario_cmd(message)


def test_additional_top_level_properties_are_rejected():
    message = {**VALID_MESSAGE, "unexpected_field": True}
    with pytest.raises(ScenarioCmdValidationError):
        validate_scenario_cmd(message)


def test_validation_is_a_no_op_when_the_schema_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "ogsim.control.schema_validation.SCENARIO_CONTROL_SCHEMA_PATH", tmp_path / "missing.schema.json"
    )
    _load_schema.cache_clear()
    validate_scenario_cmd({"nonsense": True})  # must not raise - degrades safely if the file is absent
