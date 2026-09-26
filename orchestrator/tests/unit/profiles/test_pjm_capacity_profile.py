"""PJM_CAPACITY service-profile definition: `config/service_profiles/pjm_capacity.toml`.

Spec: docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S1.3-1.5 (ServiceProfile
fields), S2 (PowerQualityEnvelope), S4.c (grid-code-minimum pattern), S5.5.3 (asset-state eligibility);
02-architecture/03-decision-engine.md S2.6/S2.7; 07-delivery/14-additional-services.md. PJM stays fully
simulated (owner decision 2026-09-26) -- there is no real PJM feed to validate against.

Same shape as `test_data_center_profile.py`: instantiate the template for a reference contract and check
the result against the interfaces/ JSON Schemas and the core pydantic models (activation-gate stages 1-2).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest

from opengrid.core.models import pq

_REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "pjm_capacity.toml"
INTERFACES_DIR = _REPO_ROOT / "interfaces"

REFERENCE_COMMITTED_KW = 5000.0
REFERENCE_SITE_ID = "pjm-zone-aep-01"
REFERENCE_EMERGENCY_HOUR_S = 3600.0


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
    # PJM_CAPACITY has no feedback_signal_ref (setpoint_source = ISO_INSTRUCTION, not
    # MEASURED_FEEDBACK -- see the toml's comment); `site_id` is accepted for a uniform helper
    # signature across the three new profile test modules but is unused when there is no template
    # to fill.
    _ = site_id
    raw_feedback_ref = template.get("feedback_signal_ref")
    return {
        "service_profile_id": str(uuid4()),
        "contract_id": str(uuid4()),
        "version": profile["profile"]["version"],
        "control_primitive": template["control_primitive"],
        "target_quantity": template["target_quantity"],
        "target_scope": template["target_scope"],
        "setpoint_source": template["setpoint_source"],
        "feedback_signal_ref": raw_feedback_ref.format(site_id=site_id) if raw_feedback_ref else None,
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


def _pq_envelope_row(profile: dict[str, Any], *, pq_envelope_id: str) -> dict[str, Any]:
    return {"pq_envelope_id": pq_envelope_id, "customer_id": str(uuid4()), **profile["pq_envelope"]}


def _reference_rows(committed_kw: float = REFERENCE_COMMITTED_KW) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = _load_profile()
    envelope_id = str(uuid4())
    service_row = _service_profile_row(
        profile,
        committed_kw=committed_kw,
        site_id=REFERENCE_SITE_ID,
        sustain_duration_s=REFERENCE_EMERGENCY_HOUR_S,
        pq_envelope_id=envelope_id,
    )
    return service_row, _pq_envelope_row(profile, pq_envelope_id=envelope_id)


# =========================================================================================================
# Activation-gate stage 1: schema validation
# =========================================================================================================


@pytest.mark.parametrize("committed_kw", [100.0, 5000.0, 20000.0])
def test_instantiated_service_profile_matches_schema_and_model(committed_kw: float) -> None:
    service_row, _ = _reference_rows(committed_kw)
    assert _schema_errors(service_row, _load_schema("contracts/service_profile.schema.json")) == []
    model = pq.ServiceProfile.model_validate(service_row)
    assert model.control_primitive == "CAPACITY_HOLD"


def test_instantiated_pq_envelope_matches_schema_and_model() -> None:
    _, envelope_row = _reference_rows()
    assert _schema_errors(envelope_row, _load_schema("contracts/pq_envelope.schema.json")) == []
    envelope = pq.PowerQualityEnvelope.model_validate(envelope_row)
    assert envelope.phase_config == "3P"


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
    assert profile["profile"]["profile_ref"] == f"pjm-capacity-profile@{profile['profile']['version']}"


def test_static_rule_every_billing_line_references_an_mv_output() -> None:
    lines = _load_profile()["settlement"]["lines"]
    assert {line["name"] for line in lines} == {"capacity_payment_x_pf", "nonperformance_charge"}
    assert all(line["mv_output"] for line in lines)


def test_static_rule_ramp_rule_reaches_committed_kw_within_response_time() -> None:
    for committed_kw in (50.0, 5000.0, 50000.0):
        service_row, _ = _reference_rows(committed_kw)
        reachable_kw = service_row["ramp_limit"] * service_row["response_time_s"] / 60.0
        assert reachable_kw >= committed_kw - 1e-9


# =========================================================================================================
# S4.c / 03 S2.6: non-firm, grid-code-minimum, simulated capacity hold
# =========================================================================================================


def test_control_law_is_a_capacity_hold_on_meter_net_load() -> None:
    template = _load_profile()["service_profile"]
    assert (template["control_primitive"], template["target_quantity"], template["target_scope"]) == (
        "CAPACITY_HOLD",
        "KW",
        "SITE_METER",
    )


def test_non_firm_tier_by_default() -> None:
    profile = _load_profile()
    assert profile["profile"]["tier"] == "T3"


def test_pjm_stays_simulated_no_pq_aware_selection() -> None:
    """S4.c pattern: PJM_CAPACITY does not compete with DATA_CENTER/PIPELINE_AC for the fleet's
    highest-quality inverters."""
    eligibility = _load_profile()["eligibility"]
    assert eligibility["pq_aware_selection"] is False


def test_envelope_is_grid_code_minimum_only_phase_config_set() -> None:
    profile = _load_profile()
    assert set(profile["pq_envelope"]) == {"phase_config"}
    envelope = pq.PowerQualityEnvelope(pq_envelope_id=uuid4(), customer_id=uuid4(), **profile["pq_envelope"])
    fleet_default = pq.PowerQualityEnvelope(pq_envelope_id=uuid4(), customer_id=uuid4(), phase_config="3P")
    assert envelope.max_phase_imbalance_pct == fleet_default.max_phase_imbalance_pct
    assert envelope.voltage_band_pct == fleet_default.voltage_band_pct
    assert envelope.pf_min == fleet_default.pf_min


def test_asset_state_eligibility_allows_degraded_like_grid_code_minimum_services() -> None:
    eligibility = _load_profile()["eligibility"]
    eligible = set(eligibility["eligible_asset_states"])
    excluded = set(eligibility["excluded_asset_states"])
    assert eligible == {"OK", "WATCH", "DEGRADED"}
    assert excluded == {"QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
    assert eligible.isdisjoint(excluded)
    assert eligible | excluded == set(get_args(pq.AssetState))


def test_emergency_call_source_is_simulated() -> None:
    control = _load_profile()["control"]
    assert control["emergency_call_source"] == "SIMULATED_ISO_INSTRUCTION"


def test_no_feedback_signal_ref_and_no_measured_feedback_constraint() -> None:
    """Lead fix 2026-09-26: a prior draft set feedback_signal_ref to a non-existent 'p_kw_net'
    site-meter field, which failed resolution as malformed. setpoint_source = ISO_INSTRUCTION (not
    MEASURED_FEEDBACK), so the schema's allOf does not require a feedback_signal_ref at all -- the
    template omits it entirely instead of pointing it at a real-but-unused field."""
    template = _load_profile()["service_profile"]
    assert "feedback_signal_ref" not in template
    assert template["setpoint_source"] != "MEASURED_FEEDBACK"
    service_row, _ = _reference_rows()
    assert service_row["feedback_signal_ref"] is None
    # Validates cleanly with no feedback_signal_ref (the schema/model only require it for
    # MEASURED_FEEDBACK, per service_profile.schema.json's allOf and
    # ServiceProfile._check_feedback_and_target_constraints).
    pq.ServiceProfile.model_validate(service_row)
