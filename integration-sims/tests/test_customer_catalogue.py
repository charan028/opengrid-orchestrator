"""Catalogue coverage: every CUSTOMER_* anomaly BUILD.md requires is registered in
ogsim.control.catalogue with owner="customer" and a wire_type the scenario_control schema
accepts, and ogsim.customer.runtime actually understands every one of them."""

from __future__ import annotations

import json
from pathlib import Path

from ogsim.common.scenario import WIRE_TYPE_TO_CATALOGUE_ID
from ogsim.control import catalogue
from ogsim.customer.anomalies import CUSTOMER_ANOMALY_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CONTROL_SCHEMA_PATH = REPO_ROOT / "interfaces" / "mqtt" / "scenario_control.schema.json"


def _schema_type_enum() -> set[str]:
    with SCENARIO_CONTROL_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    return set(schema["properties"]["type"]["enum"])


REQUIRED_CUSTOMER_TYPES = {
    "load_step_datacenter",
    "pipeline_current_surge",
    "request_burst",
    "malformed_request",
    "late_cancellation",
    "invoice_dispute",
    "site_meter_stale",
}


def test_catalogue_covers_every_required_customer_type():
    assert REQUIRED_CUSTOMER_TYPES <= catalogue.CUSTOMER_TYPES


def test_customer_anomaly_manager_covers_the_same_set_as_the_catalogue():
    assert CUSTOMER_ANOMALY_TYPES == catalogue.CUSTOMER_TYPES


def test_every_customer_catalogue_entry_has_a_wire_type_and_description():
    for entry in catalogue.CATALOGUE:
        if entry.owner != "customer":
            continue
        assert entry.wire_type
        assert entry.description


def test_every_customer_wire_type_is_in_the_scenario_control_schema_enum():
    enum_values = _schema_type_enum()
    missing = [
        (entry.id, entry.wire_type)
        for entry in catalogue.CATALOGUE
        if entry.owner == "customer" and entry.wire_type not in enum_values
    ]
    assert missing == [], f"customer catalogue entries missing from scenario_control.schema.json: {missing}"


def test_every_customer_wire_type_round_trips_through_the_scenario_mapping():
    customer_entries = [e for e in catalogue.CATALOGUE if e.owner == "customer"]
    for entry in customer_entries:
        assert WIRE_TYPE_TO_CATALOGUE_ID[entry.wire_type] == entry.id


def test_infer_wire_target_kind_for_customer_site_and_corridor_targets():
    load_step = catalogue.BY_ID["load_step_datacenter"]
    assert catalogue.infer_wire_target_kind(load_step, "site-01") == "site"
    surge = catalogue.BY_ID["pipeline_current_surge"]
    assert catalogue.infer_wire_target_kind(surge, "corridor-01") == "corridor"
