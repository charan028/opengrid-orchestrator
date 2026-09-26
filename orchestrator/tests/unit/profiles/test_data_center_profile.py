"""DATA_CENTER service-profile definition (WP-F): `config/service_profiles/data_center.toml`.

Spec: docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S4.b, ES12, S5.5.3, and the
profile-level preconditions of TS-12b and TS-13a; activation-gate stages 1-2 (schema validation and static
rules) of 02-architecture/03-decision-engine.md S2.7.

The file is a template: these tests instantiate it for reference contracts and check the result against the
core pydantic models, the interfaces/ JSON Schemas and the spec. They only read the file and call
`opengrid.core` functions; the eligibility filter itself belongs to the allocator (S5.2).
"""

from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from opengrid.core.models import pq
from opengrid.core.pq import (
    ComplianceState,
    PqEnvelopeLimits,
    PqMeasurement,
    evaluate_envelope,
    inverter_quality_score,
    worst_verdict,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = _REPO_ROOT / "orchestrator" / "config" / "service_profiles" / "data_center.toml"
INTERFACES_DIR = _REPO_ROOT / "interfaces"

# 03 S2.5 element-8 library values a firm profile may use as its signal-loss behaviour.
SIGNAL_LOSS_BEHAVIOURS = {"HOLD_THEN_SCHEDULE", "NEUTRAL_ON_SIGNAL_LOSS", "CONTINUE_TO_DECLARED_END"}

REFERENCE_COMMITTED_KW = 1000.0
REFERENCE_SITE_ID = "dc-austin-01"
REFERENCE_SUSTAIN_S = 900.0


def _load_profile() -> dict[str, Any]:
    with PROFILE_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _load_schema(relative_path: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((INTERFACES_DIR / relative_path).read_text(encoding="utf-8"))
    return schema


def _schema_errors(instance: dict[str, Any], schema: dict[str, Any]) -> list[str]:
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
    """The og.service_profile row a contract would get from this template (JSON-shaped: numbers stay numbers)."""
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


def _pq_envelope_row(profile: dict[str, Any], *, pq_envelope_id: str) -> dict[str, Any]:
    return {"pq_envelope_id": pq_envelope_id, "customer_id": str(uuid4()), **profile["pq_envelope"]}


def _reference_rows(committed_kw: float = REFERENCE_COMMITTED_KW) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = _load_profile()
    envelope_id = str(uuid4())
    service_row = _service_profile_row(
        profile,
        committed_kw=committed_kw,
        site_id=REFERENCE_SITE_ID,
        sustain_duration_s=REFERENCE_SUSTAIN_S,
        pq_envelope_id=envelope_id,
    )
    return service_row, _pq_envelope_row(profile, pq_envelope_id=envelope_id)


def _limits(envelope: pq.PowerQualityEnvelope) -> PqEnvelopeLimits:
    return PqEnvelopeLimits(
        max_phase_imbalance_pct=float(envelope.max_phase_imbalance_pct),
        voltage_band_pct=float(envelope.voltage_band_pct),
        freq_tolerance_hz=float(envelope.freq_tolerance_hz),
        pf_min=float(envelope.pf_min),
        thd_voltage_limit_pct=float(envelope.thd_voltage_limit_pct),
        thd_current_limit_pct=float(envelope.thd_current_limit_pct),
        current_limit_a=None if envelope.current_limit_a is None else float(envelope.current_limit_a),
    )


def _data_center_envelope() -> pq.PowerQualityEnvelope:
    _, envelope_row = _reference_rows()
    return pq.PowerQualityEnvelope.model_validate(envelope_row)


def _fleet_default_envelope() -> pq.PowerQualityEnvelope:
    """S2 / S4.c: the fleet-default envelope (grid-code minimum) that HOME and ERCOT_ENERGY use."""
    return pq.PowerQualityEnvelope(pq_envelope_id=uuid4(), customer_id=uuid4(), phase_config="3P")


# =========================================================================================================
# 03 S2.7 stage 1: schema validation
# =========================================================================================================


@pytest.mark.parametrize("committed_kw", [100.0, 1000.0, 5000.0])
def test_ts_12_instantiated_service_profile_matches_schema_and_model(committed_kw: float) -> None:
    service_row, _ = _reference_rows(committed_kw)
    assert _schema_errors(service_row, _load_schema("contracts/service_profile.schema.json")) == []
    model = pq.ServiceProfile.model_validate(service_row)
    assert model.control_primitive == "CLOSED_LOOP_REGULATION"


def test_ts_12_instantiated_pq_envelope_matches_schema_and_model() -> None:
    _, envelope_row = _reference_rows()
    assert _schema_errors(envelope_row, _load_schema("contracts/pq_envelope.schema.json")) == []
    assert pq.PowerQualityEnvelope.model_validate(envelope_row).phase_config == "3P"


def test_ts_12_template_carries_no_row_ids_or_unknown_fields() -> None:
    """Ids are generated per contract; any other key must be a schema field, so the template cannot drift."""
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
# 03 S2.7 stage 2: static rules
# =========================================================================================================


def test_static_rule_closed_loop_names_a_fresh_measured_point_with_unit_typed_limit() -> None:
    profile = _load_profile()
    template = profile["service_profile"]
    assert template["setpoint_source"] == "MEASURED_FEEDBACK"
    assert template["feedback_signal_ref"].startswith("site_meter:")
    assert "{site_id}" in template["feedback_signal_ref"]
    assert 0 < profile["control"]["feedback_freshness_s"] <= template["response_time_s"]
    assert template["ramp_limit_unit"] == "kw_per_min"


def test_static_rule_firm_profile_defines_signal_loss_behaviour() -> None:
    profile = _load_profile()
    assert profile["profile"]["tier"] == "T1"
    assert profile["service_profile"]["failure_behaviour"] in SIGNAL_LOSS_BEHAVIOURS


def test_static_rule_every_billing_line_references_an_mv_output() -> None:
    lines = _load_profile()["settlement"]["lines"]
    assert {line["name"] for line in lines} == {"capacity_payment_x_pf", "pq_compliance_pct"}
    assert all(line["mv_output"] for line in lines)


def test_static_rule_profile_tier_matches_contract_tier() -> None:
    profile = _load_profile()
    assert profile["service_profile"]["priority_tier"] == profile["profile"]["tier"]
    assert profile["profile"]["profile_ref"] == f"data-center-profile@{profile['profile']['version']}"


# =========================================================================================================
# S4.b control law, M&V and failure behaviour
# =========================================================================================================


def test_spec_4b_closed_loop_kw_at_the_site_meter_within_two_seconds() -> None:
    template = _load_profile()["service_profile"]
    assert (template["control_primitive"], template["target_quantity"], template["target_scope"]) == (
        "CLOSED_LOOP_REGULATION",
        "KW",
        "SITE_METER",
    )
    assert template["response_time_s"] <= 2.0


def test_spec_4b_fast_loop_is_proportional_only_k9() -> None:
    control = _load_profile()["control"]
    assert control["fast_loop"] == "PROPORTIONAL_ONLY"
    assert control["integrator"] is False


@pytest.mark.parametrize("committed_kw", [50.0, 1000.0, 20000.0])
def test_spec_4b_ramp_rule_reaches_committed_kw_within_response_time(committed_kw: float) -> None:
    service_row, _ = _reference_rows(committed_kw)
    reachable_kw = service_row["ramp_limit"] * service_row["response_time_s"] / 60.0
    assert reachable_kw >= committed_kw - 1e-9


def test_spec_4b_mv_settlement_and_failure_behaviour() -> None:
    profile = _load_profile()
    template = profile["service_profile"]
    assert template["mv_method"] == "DIRECT_HUB_METER"
    assert template["settlement_metric"] == "capacity_payment_x_pf"
    assert profile["settlement"]["performance_factor"] == "min(pf_kw, pf_pq)"
    assert template["failure_behaviour"] == "HOLD_THEN_SCHEDULE"


# =========================================================================================================
# ES12 envelope and S4.b ride-through
# =========================================================================================================


def test_es12_envelope_values() -> None:
    envelope = _data_center_envelope()
    assert envelope.phase_config == "3P"
    assert envelope.voltage_band_pct == Decimal("2.0")
    assert envelope.max_phase_imbalance_pct == Decimal("1.5")
    assert envelope.ride_through_class == "CATEGORY_III"
    assert envelope.pf_min >= Decimal("0.95")


def test_spec_4b_envelope_is_never_looser_than_the_fleet_default() -> None:
    data_center = _data_center_envelope()
    fleet = _fleet_default_envelope()
    assert data_center.max_phase_imbalance_pct <= fleet.max_phase_imbalance_pct
    assert data_center.voltage_band_pct <= fleet.voltage_band_pct
    assert data_center.freq_tolerance_hz <= fleet.freq_tolerance_hz
    assert data_center.thd_voltage_limit_pct <= fleet.thd_voltage_limit_pct
    assert data_center.thd_current_limit_pct <= fleet.thd_current_limit_pct
    assert data_center.pf_min >= fleet.pf_min


def test_spec_4b_only_category_iii_ride_through_is_eligible() -> None:
    eligibility = _load_profile()["eligibility"]
    assert eligibility["required_ride_through_class"] == "CATEGORY_III"
    assert _data_center_envelope().ride_through_class == eligibility["required_ride_through_class"]


# =========================================================================================================
# S5.5.3 asset-state eligibility (ES17: DEGRADED is excluded from DATA_CENTER immediately)
# =========================================================================================================


def test_spec_5_5_3_asset_state_eligibility_covers_every_state() -> None:
    eligibility = _load_profile()["eligibility"]
    eligible = set(eligibility["eligible_asset_states"])
    excluded = set(eligibility["excluded_asset_states"])
    assert eligible == {"OK", "WATCH"}
    assert excluded == {"DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
    assert eligible.isdisjoint(excluded)
    assert eligible | excluded == set(get_args(pq.AssetState))


# =========================================================================================================
# TS-12b (profile-level precondition): the quality threshold separates normal from anomalous inverters
# =========================================================================================================


def _score(hub: pq.HubInverterPq) -> float:
    return inverter_quality_score(
        float(hub.freq_offset_hz),
        float(hub.voltage_offset_pct),
        float(hub.thd_current_pct),
        float(hub.phase_angle_error_deg),
    )


def test_ts_12b_default_characterized_inverter_passes_the_quality_threshold() -> None:
    minimum = _load_profile()["eligibility"]["min_quality_score"]
    default_hub = pq.HubInverterPq(hub_id="hub-00001", phase_connection="A", kva_rating=Decimal("11"))
    assert _score(default_hub) >= minimum


@pytest.mark.parametrize(
    ("anomaly", "overrides"),
    [
        ("harmonic_injection", {"thd_current_pct": Decimal("8.0")}),
        ("amplitude_deviation", {"voltage_offset_pct": Decimal("3.0")}),
        ("phase_angle_drift", {"phase_angle_error_deg": Decimal("6.0")}),
    ],
)
def test_ts_12b_anomalous_inverter_fails_the_quality_threshold(
    anomaly: str, overrides: dict[str, Decimal]
) -> None:
    minimum = _load_profile()["eligibility"]["min_quality_score"]
    hub = pq.HubInverterPq.model_validate(
        {"hub_id": "hub-00002", "phase_connection": "B", "kva_rating": Decimal("11"), **overrides}
    )
    assert _score(hub) < minimum, anomaly


# =========================================================================================================
# TS-13a (profile-level precondition): arbitrage's fleet-default envelope accepts what DATA_CENTER rejects,
# never the other way round
# =========================================================================================================


def test_ts_13a_measurement_inside_fleet_default_but_outside_data_center_envelope() -> None:
    measurement = PqMeasurement(
        imbalance_pct=1.0,
        voltage_deviation_pct=3.0,
        freq_deviation_hz=0.02,
        pf=0.97,
        thd_voltage_pct=1.0,
        thd_current_pct=1.0,
    )
    fleet = worst_verdict(evaluate_envelope(measurement, _limits(_fleet_default_envelope())))
    data_center = worst_verdict(evaluate_envelope(measurement, _limits(_data_center_envelope())))
    assert fleet == ComplianceState.NOMINAL
    assert data_center == ComplianceState.BREACH


_SEVERITY = {ComplianceState.NOMINAL: 0, ComplianceState.WARN: 1, ComplianceState.BREACH: 2}


@given(
    imbalance=st.floats(min_value=0.0, max_value=10.0),
    voltage=st.floats(min_value=0.0, max_value=10.0),
    freq=st.floats(min_value=0.0, max_value=1.0),
    thd_v=st.floats(min_value=0.0, max_value=10.0),
    thd_i=st.floats(min_value=0.0, max_value=10.0),
)
def test_ts_13a_data_center_verdict_is_never_milder_than_fleet_default(
    imbalance: float, voltage: float, freq: float, thd_v: float, thd_i: float
) -> None:
    measurement = PqMeasurement(
        imbalance_pct=imbalance,
        voltage_deviation_pct=voltage,
        freq_deviation_hz=freq,
        pf=1.0,
        thd_voltage_pct=thd_v,
        thd_current_pct=thd_i,
    )
    fleet = evaluate_envelope(measurement, _limits(_fleet_default_envelope()))
    data_center = evaluate_envelope(measurement, _limits(_data_center_envelope()))
    for dimension, fleet_verdict in fleet.items():
        assert _SEVERITY[data_center[dimension]] >= _SEVERITY[fleet_verdict], dimension
