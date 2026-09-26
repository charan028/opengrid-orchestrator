"""MOBILE_STORAGE service-profile definition: `config/service_profiles/mobile_storage.toml`.

Spec: docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S1.3-1.5, S2, S5.5.3;
02-architecture/03-decision-engine.md S2.5-2.7; 07-delivery/14-additional-services.md.

Same shape as `test_data_center_profile.py`, plus a check specific to MOBILE_STORAGE: `site_id` (and the
M&V feedback point built from it) must be free to change between two deployments of the same template
without touching the template itself.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest

from opengrid.core.models import pq
from opengrid.settle.services_extra import mobile_deployment_availability_pct

_REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "mobile_storage.toml"
HOME_STATIONS_PATH = (
    _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "mobile_storage_home_stations.toml"
)
INTERFACES_DIR = _REPO_ROOT / "interfaces"

REFERENCE_COMMITTED_KW = 500.0


def _load_profile() -> dict[str, Any]:
    with PROFILE_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _load_schema(relative_path: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((INTERFACES_DIR / relative_path).read_text(encoding="utf-8"))
    return schema


def _schema_errors(instance: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return [f"{list(err.path)}: {err.message}" for err in validator.iter_errors(instance)]


def _service_profile_row(
    profile: dict[str, Any],
    *,
    committed_kw: float,
    site_id: str,
    sustain_duration_s: float,
    pq_envelope_id: str,
) -> dict[str, Any]:
    template = profile["service_profile"]
    rules = profile["instantiation"]
    return {
        "service_profile_id": str(uuid4()),
        "contract_id": str(uuid4()),
        "version": profile["profile"]["version"],
        "control_primitive": template["control_primitive"],
        "target_quantity": template["target_quantity"],
        "target_scope": template["target_scope"],
        "setpoint_source": template["setpoint_source"],
        "feedback_signal_ref": template["feedback_signal_ref"].format(site_id=site_id),
        "response_time_s": template["response_time_s"],
        "ramp_limit": committed_kw * rules["ramp_limit_per_committed_kw_per_min"],
        "ramp_limit_unit": template["ramp_limit_unit"],
        "sustain_duration_s": sustain_duration_s,
        "accuracy_tolerance": committed_kw * rules["accuracy_tolerance_fraction_of_committed"],
        "deadband": committed_kw * rules["deadband_fraction_of_committed"],
        "priority_tier": template["priority_tier"],
        "mv_method": template["mv_method"],
        "settlement_metric": template["settlement_metric"],
        "pq_envelope_id": pq_envelope_id,
        "failure_behaviour": template["failure_behaviour"],
    }


def _reference_row(site_id: str, committed_kw: float = REFERENCE_COMMITTED_KW) -> dict[str, Any]:
    profile = _load_profile()
    return _service_profile_row(
        profile,
        committed_kw=committed_kw,
        site_id=site_id,
        sustain_duration_s=14400.0,
        pq_envelope_id=str(uuid4()),
    )


# =========================================================================================================
# Activation-gate stage 1: schema validation
# =========================================================================================================


@pytest.mark.parametrize("committed_kw", [50.0, 500.0, 2000.0])
def test_instantiated_service_profile_matches_schema_and_model(committed_kw: float) -> None:
    service_row = _reference_row("trailer-deploy-001", committed_kw)
    assert _schema_errors(service_row, _load_schema("contracts/service_profile.schema.json")) == []
    model = pq.ServiceProfile.model_validate(service_row)
    assert model.control_primitive == "EVENT_SCHEDULE_TRACKING"


def test_instantiated_pq_envelope_matches_schema_and_model() -> None:
    profile = _load_profile()
    envelope_row = {
        "pq_envelope_id": str(uuid4()),
        "customer_id": str(uuid4()),
        **profile["pq_envelope"],
    }
    assert _schema_errors(envelope_row, _load_schema("contracts/pq_envelope.schema.json")) == []
    assert pq.PowerQualityEnvelope.model_validate(envelope_row).phase_config == "3P"


def test_template_carries_no_row_ids_or_unknown_fields() -> None:
    profile = _load_profile()
    service_fields = set(_load_schema("contracts/service_profile.schema.json")["properties"])
    envelope_fields = set(_load_schema("contracts/pq_envelope.schema.json")["properties"])
    assert set(profile["service_profile"]) <= service_fields - {
        "service_profile_id",
        "contract_id",
        "pq_envelope_id",
    }
    assert set(profile["pq_envelope"]) <= envelope_fields - {"pq_envelope_id", "customer_id"}


# =========================================================================================================
# Activation-gate stage 2: static rules
# =========================================================================================================


def test_static_rule_profile_tier_matches_contract_tier() -> None:
    profile = _load_profile()
    assert profile["service_profile"]["priority_tier"] == profile["profile"]["tier"]
    assert profile["profile"]["profile_ref"] == f"mobile-storage-profile@{profile['profile']['version']}"


def test_static_rule_every_billing_line_references_an_mv_output() -> None:
    lines = _load_profile()["settlement"]["lines"]
    assert {line["name"] for line in lines} == {
        "deployment_capacity_payment",
        "deployment_availability_pct",
    }
    assert all(line["mv_output"] for line in lines)


def test_static_rule_ramp_rule_reaches_committed_kw_within_response_time() -> None:
    for committed_kw in (50.0, 500.0, 5000.0):
        row = _reference_row("trailer-deploy-001", committed_kw)
        reachable_kw = row["ramp_limit"] * row["response_time_s"] / 60.0
        assert reachable_kw >= committed_kw - 1e-9


# =========================================================================================================
# Deployment relocation: the profile is location-free; only the instantiated row's feedback signal moves
# =========================================================================================================


def test_feedback_signal_ref_changes_with_the_deployment_site_but_template_does_not() -> None:
    row_a = _reference_row("site-warehouse-01")
    row_b = _reference_row("site-drilling-pad-07")
    assert row_a["feedback_signal_ref"] == "site_meter:site-warehouse-01:p_kw"
    assert row_b["feedback_signal_ref"] == "site_meter:site-drilling-pad-07:p_kw"
    # Everything else about the two deployments' service profile is identical (same template).
    assert {
        k: v
        for k, v in row_a.items()
        if k not in {"service_profile_id", "contract_id", "feedback_signal_ref", "pq_envelope_id"}
    } == {
        k: v
        for k, v in row_b.items()
        if k not in {"service_profile_id", "contract_id", "feedback_signal_ref", "pq_envelope_id"}
    }


def test_template_feedback_signal_ref_is_not_bound_to_a_site() -> None:
    template = _load_profile()["service_profile"]
    assert "{site_id}" in template["feedback_signal_ref"]


def test_asset_state_eligibility_covers_every_state() -> None:
    eligibility = _load_profile()["eligibility"]
    eligible = set(eligibility["eligible_asset_states"])
    excluded = set(eligibility["excluded_asset_states"])
    assert eligible == {"OK", "WATCH"}
    assert excluded == {"DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
    assert eligible | excluded == set(get_args(pq.AssetState))


def test_delivered_to_site_meter() -> None:
    template = _load_profile()["service_profile"]
    assert template["target_scope"] == "SITE_METER"
    assert template["mv_method"] == "AMI_INTERVAL"


# =========================================================================================================
# deployment_availability_pct settlement helper (opengrid.settle.services_extra)
# =========================================================================================================


def test_deployment_availability_pct_full_and_partial_window() -> None:
    from decimal import Decimal

    assert mobile_deployment_availability_pct(240, 240) == Decimal("1")
    assert mobile_deployment_availability_pct(120, 240) == Decimal("0.5")


# =========================================================================================================
# D-31 (2026-09-26, docs/orchestrator/07-delivery/11-decision-log.md): a mobile unit is never charged
# from the fleet, only at its registered home station, priced at that station's tariff.
# =========================================================================================================


def test_d31_never_chargeable_from_fleet() -> None:
    profile = _load_profile()
    assert profile["home_station"]["chargeable_from_fleet"] is False
    assert profile["control"]["charge_source"] == "HOME_STATION_ONLY"


def test_d31_home_station_registry_is_referenced_and_exists() -> None:
    home_station = _load_profile()["home_station"]
    registry_relpath = home_station["registry"]
    assert registry_relpath == "config/service_profiles/mobile_storage_home_stations.toml"
    assert HOME_STATIONS_PATH.exists()


def test_d31_charging_priced_at_the_home_station_not_the_deployment_site() -> None:
    home_station = _load_profile()["home_station"]
    assert home_station["charging_priced_at"] == "HOME_STATION_TARIFF"


def test_d31_home_station_registry_parses_and_has_required_fields() -> None:
    with HOME_STATIONS_PATH.open("rb") as handle:
        registry = tomllib.load(handle)
    stations = registry["home_station"]
    assert len(stations) >= 1
    required = {"home_station_id", "zone", "lat", "lon", "charger_kw"}
    for station in stations:
        assert required <= set(station)
    station_ids = {s["home_station_id"] for s in stations}
    assert len(station_ids) == len(stations)  # no duplicate home_station_id


def test_d31_home_station_assignments_reference_a_defined_station() -> None:
    with HOME_STATIONS_PATH.open("rb") as handle:
        registry = tomllib.load(handle)
    station_ids = {s["home_station_id"] for s in registry["home_station"]}
    assignments = registry["assignment"]
    assert len(assignments) >= 1
    for assignment in assignments:
        assert assignment["home_station_id"] in station_ids
        assert assignment["bank_id"]  # og.bank.bank_id / og.hub.hub_id -- what OPTIMIZER keys is_mobile on


def test_d31_optimizer_can_join_bank_id_to_a_zone_via_home_station_id() -> None:
    """The exact join OPTIMIZER's `load_banks`/`bank_market_terms` needs (2026-09-26 R3.1 message to
    OPTIMIZER): assignment.bank_id -> assignment.home_station_id -> home_station.zone. Every assigned
    bank_id must resolve to exactly one zone this way."""
    with HOME_STATIONS_PATH.open("rb") as handle:
        registry = tomllib.load(handle)
    zone_by_station = {s["home_station_id"]: s["zone"] for s in registry["home_station"]}
    zone_by_bank_id = {}
    for assignment in registry["assignment"]:
        zone_by_bank_id[assignment["bank_id"]] = zone_by_station[assignment["home_station_id"]]
    assert zone_by_bank_id == {"trailer-mb-01": "LZ_AEN"}
