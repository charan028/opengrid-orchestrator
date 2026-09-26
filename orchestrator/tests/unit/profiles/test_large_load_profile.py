"""LARGE_LOAD service-profile definition: `config/service_profiles/large_load.toml`.

Spec: docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S4.b (the explicit
"LARGE_LOAD ... no PQ obligation beyond grid-code minimum" contrast with DATA_CENTER), S5.5.3;
02-architecture/03-decision-engine.md S2.6 ("LARGE_LOAD contracted stress events from the customer's
signal"); 07-delivery/14-additional-services.md.
"""

from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest

from opengrid.core.models import pq
from opengrid.settle.services_extra import large_load_curtailment_compliance_pct

_REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "large_load.toml"
INTERFACES_DIR = _REPO_ROOT / "interfaces"

REFERENCE_COMMITTED_KW = 2000.0
REFERENCE_SITE_ID = "large-load-crypto-01"


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


def _reference_row(committed_kw: float = REFERENCE_COMMITTED_KW) -> dict[str, Any]:
    profile = _load_profile()
    return _service_profile_row(
        profile,
        committed_kw=committed_kw,
        site_id=REFERENCE_SITE_ID,
        sustain_duration_s=1800.0,
        pq_envelope_id=str(uuid4()),
    )


# =========================================================================================================
# Activation-gate stage 1: schema validation
# =========================================================================================================


@pytest.mark.parametrize("committed_kw", [100.0, 2000.0, 15000.0])
def test_instantiated_service_profile_matches_schema_and_model(committed_kw: float) -> None:
    service_row = _reference_row(committed_kw)
    assert _schema_errors(service_row, _load_schema("contracts/service_profile.schema.json")) == []
    model = pq.ServiceProfile.model_validate(service_row)
    assert model.control_primitive == "EVENT_SCHEDULE_TRACKING"


def test_instantiated_pq_envelope_matches_schema_and_model() -> None:
    profile = _load_profile()
    envelope_row = {"pq_envelope_id": str(uuid4()), "customer_id": str(uuid4()), **profile["pq_envelope"]}
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
    assert profile["profile"]["profile_ref"] == f"large-load-profile@{profile['profile']['version']}"


def test_static_rule_every_billing_line_references_an_mv_output() -> None:
    lines = _load_profile()["settlement"]["lines"]
    assert {line["name"] for line in lines} == {"capacity_payment_x_performance"}
    assert all(line["mv_output"] for line in lines)


def test_static_rule_ramp_rule_reaches_committed_kw_within_response_time() -> None:
    for committed_kw in (50.0, 2000.0, 50000.0):
        row = _reference_row(committed_kw)
        reachable_kw = row["ramp_limit"] * row["response_time_s"] / 60.0
        assert reachable_kw >= committed_kw - 1e-9


# =========================================================================================================
# S4.b: the explicit DATA_CENTER contrast -- customer-signalled event tracking, grid-code minimum PQ
# =========================================================================================================


def test_control_law_tracks_a_customer_declared_load_following_schedule() -> None:
    template = _load_profile()["service_profile"]
    assert template["control_primitive"] == "EVENT_SCHEDULE_TRACKING"
    assert template["setpoint_source"] == "CUSTOMER_API"
    assert template["target_scope"] == "SITE_METER"


def test_no_pq_obligation_beyond_grid_code_minimum() -> None:
    """S4.b: 'LARGE_LOAD ... is a load-offset event profile with no PQ obligation beyond grid-code
    minimum' -- the explicit contrast with DATA_CENTER's tight envelope."""
    profile = _load_profile()
    assert set(profile["pq_envelope"]) == {"phase_config"}
    assert profile["eligibility"]["pq_aware_selection"] is False
    envelope = pq.PowerQualityEnvelope(pq_envelope_id=uuid4(), customer_id=uuid4(), **profile["pq_envelope"])
    fleet_default = pq.PowerQualityEnvelope(pq_envelope_id=uuid4(), customer_id=uuid4(), phase_config="3P")
    assert envelope.voltage_band_pct == fleet_default.voltage_band_pct
    assert envelope.thd_current_limit_pct == fleet_default.thd_current_limit_pct


def test_still_requires_category_iii_ride_through() -> None:
    """Grid-code minimum does not mean no ride-through requirement: the load must not trip during the
    grid event that likely triggered its own curtailment/ride-through call."""
    eligibility = _load_profile()["eligibility"]
    assert eligibility["required_ride_through_class"] == "CATEGORY_III"


def test_asset_state_eligibility_allows_degraded_like_grid_code_minimum_services() -> None:
    eligibility = _load_profile()["eligibility"]
    eligible = set(eligibility["eligible_asset_states"])
    excluded = set(eligibility["excluded_asset_states"])
    assert eligible == {"OK", "WATCH", "DEGRADED"}
    assert excluded == {"QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
    assert eligible | excluded == set(get_args(pq.AssetState))


def test_distinct_from_data_center_variant() -> None:
    profile = _load_profile()
    assert profile["profile"]["service_type"] == "LARGE_LOAD"
    assert profile["profile"]["variant"] != "BRIDGING"


# =========================================================================================================
# capacity_payment_x_performance settlement helper (opengrid.settle.services_extra)
# =========================================================================================================


def test_curtailment_compliance_pct_full_and_partial_and_no_schedule() -> None:
    assert large_load_curtailment_compliance_pct(Decimal("500"), Decimal("500")) == Decimal("1")
    assert large_load_curtailment_compliance_pct(Decimal("250"), Decimal("500")) == Decimal("0.5")
    assert large_load_curtailment_compliance_pct(Decimal("0"), Decimal("0")) is None
