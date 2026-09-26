"""Regression guard (post-deploy defect: injecting calibration_drift_correctable/hardware, and
every other PQ anomaly type, returned HTTP 500 from og-sim-control). Root cause: the catalogue
(`ogsim.control.catalogue`) had the entries, but `interfaces/mqtt/scenario_control.schema.json`'s
`type` enum -- which `ogsim.control.mqtt_pub.publish_scenario_cmd` validates every scada/fleet
injection against before publishing -- did not list their `wire_type` values, so validation
raised and the FastAPI route returned an unhandled 500.

This test asserts every catalogue entry with a wire_type (scada/fleet -- market entries never go
over MQTT and have no wire_type) is present in the schema's enum, so this class of bug cannot
reappear silently."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ogsim.control import catalogue
from ogsim.control.schema_validation import validate_scenario_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CONTROL_SCHEMA_PATH = REPO_ROOT / "interfaces" / "mqtt" / "scenario_control.schema.json"


def _schema_type_enum() -> set[str]:
    with SCENARIO_CONTROL_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    return set(schema["properties"]["type"]["enum"])


def test_every_scada_and_fleet_catalogue_wire_type_is_in_the_schema_enum():
    enum_values = _schema_type_enum()
    missing = [
        (entry.id, entry.wire_type)
        for entry in catalogue.CATALOGUE
        if entry.owner in ("scada", "fleet") and entry.wire_type not in enum_values
    ]
    assert missing == [], f"catalogue entries missing from scenario_control.schema.json: {missing}"


def test_calibration_drift_wire_types_specifically_are_covered():
    """The exact defect report: calibration-drift injection returned 500."""
    enum_values = _schema_type_enum()
    assert "FLEET_CALIBRATION_DRIFT_CORRECTABLE" in enum_values
    assert "FLEET_CALIBRATION_DRIFT_HARDWARE" in enum_values


def test_calibration_drift_correctable_scenario_cmd_now_validates_without_raising():
    """The exact call site that used to raise `ScenarioCmdValidationError` (and, uncaught
    in `app.py`'s /api/inject route, surfaced as an HTTP 500) before the schema fix."""
    entry = catalogue.BY_ID["calibration_drift_correctable"]
    message = {
        "id": str(uuid.uuid4()),
        "target": {"kind": "hub", "ref": "hub-00000"},
        "type": entry.wire_type,
        "params": {"target_freq_offset_hz": 0.2, "target_voltage_offset_pct": 2.0},
        "start": datetime.now(UTC).isoformat(),
        "duration_s": 60,
    }
    validate_scenario_cmd(message)  # must not raise


def test_every_pq_catalogue_wire_type_is_covered():
    """The other PQ types named in the defect report."""
    enum_values = _schema_type_enum()
    for anomaly_id in (
        "frequency_drift",
        "harmonic_injection",
        "phase_imbalance_injection",
        "replace_inverter",
        "site_sag_swell",
    ):
        assert catalogue.BY_ID[anomaly_id].wire_type in enum_values
