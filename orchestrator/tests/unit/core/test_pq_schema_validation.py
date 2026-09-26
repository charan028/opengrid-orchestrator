"""Schema-validation tests for opengrid.core.models.pq (BUILD.md WP-A item 3): every contract-side
object and wire message mirroring interfaces/{contracts,mqtt}/*.schema.json must actually validate
against that schema, so the single pydantic definition and the JSON Schema file never silently drift
apart (docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md).

Decimal-typed fields dump to JSON strings under pydantic v2's `model_dump(mode="json")` (its lossless
default for arbitrary-precision decimals), while the JSON Schema files declare them `"type": "number"`
(mirroring the DDL's `numeric(...)` columns) -- `_jsonable()` below converts Decimal -> float (and leaves
UUID/datetime's already-correct ISO string dumps alone) purely for this schema-shape comparison; it does
not change any production serialization path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from opengrid.core.models import mqtt, pq

INTERFACES_DIR = Path(__file__).resolve().parents[4] / "interfaces"
NOW = datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    """Recursively convert a pydantic `model_dump(mode="json")` tree so Decimal-derived JSON strings
    become JSON numbers again, for comparison against a schema that declares `"type": "number"`."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, str):
        try:
            return float(value) if "." in value or "e" in value.lower() else int(value)
        except ValueError:
            return value
    return value


def _load_schema(relative_path: str) -> dict[str, Any]:
    return json.loads((INTERFACES_DIR / relative_path).read_text(encoding="utf-8"))


def _assert_valid(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


# =========================================================================================================
# Contract-side objects
# =========================================================================================================


def test_service_profile_matches_schema():
    profile = pq.ServiceProfile(
        service_profile_id=uuid4(),
        contract_id=uuid4(),
        control_primitive="CLOSED_LOOP_REGULATION",
        target_quantity="BANK_KVA",
        target_scope="BANK",
        setpoint_source="MEASURED_FEEDBACK",
        feedback_signal_ref="scada:bank-1:kva",
        response_time_s=Decimal("2.0"),
        ramp_limit=Decimal("50"),
        accuracy_tolerance=Decimal("0.02"),
        deadband=Decimal("0.01"),
        priority_tier="T2",
        mv_method="SCADA_OUTCOME",
        settlement_metric="achieved_line_current_delta_a",
        pq_envelope_id=uuid4(),
        failure_behaviour="NEUTRAL_ON_SIGNAL_LOSS",
    )
    schema = _load_schema("contracts/service_profile.schema.json")
    _assert_valid(_jsonable(profile.model_dump(mode="json")), schema)


def test_service_profile_rejects_measured_feedback_without_signal_ref():
    with pytest.raises(ValueError, match="feedback_signal_ref"):
        pq.ServiceProfile(
            service_profile_id=uuid4(),
            contract_id=uuid4(),
            control_primitive="PRICE_RESPONSE",
            target_quantity="KW",
            target_scope="SITE_METER",
            setpoint_source="MEASURED_FEEDBACK",
            response_time_s=Decimal("2.0"),
            ramp_limit=Decimal("50"),
            accuracy_tolerance=Decimal("0.02"),
            deadband=Decimal("0.01"),
            priority_tier="T2",
            mv_method="SCADA_OUTCOME",
            settlement_metric="energy_x_price",
            pq_envelope_id=uuid4(),
            failure_behaviour="NEUTRAL_ON_SIGNAL_LOSS",
        )


def test_service_profile_rejects_closed_loop_with_none_target():
    with pytest.raises(ValueError, match="target_quantity"):
        pq.ServiceProfile(
            service_profile_id=uuid4(),
            contract_id=uuid4(),
            control_primitive="CLOSED_LOOP_REGULATION",
            target_quantity="NONE",
            target_scope="SITE_METER",
            setpoint_source="PLAN",
            response_time_s=Decimal("2.0"),
            ramp_limit=Decimal("50"),
            accuracy_tolerance=Decimal("0.02"),
            deadband=Decimal("0.01"),
            priority_tier="T2",
            mv_method="SCADA_OUTCOME",
            settlement_metric="energy_x_price",
            pq_envelope_id=uuid4(),
            failure_behaviour="NEUTRAL_ON_SIGNAL_LOSS",
        )


def test_pq_envelope_matches_schema():
    envelope = pq.PowerQualityEnvelope(
        pq_envelope_id=uuid4(),
        customer_id=uuid4(),
        phase_config="3P",
        max_phase_imbalance_pct=Decimal("1.5"),
        voltage_band_pct=Decimal("2.0"),
        current_limit_a=Decimal("400"),
        current_limit_scope="PER_PHASE",
        pf_min=Decimal("0.95"),
    )
    schema = _load_schema("contracts/pq_envelope.schema.json")
    _assert_valid(_jsonable(envelope.model_dump(mode="json")), schema)


def test_pq_envelope_default_fleet_envelope_matches_schema():
    envelope = pq.PowerQualityEnvelope(
        pq_envelope_id=uuid4(), customer_id=uuid4(), phase_config="SPLIT_PHASE"
    )
    schema = _load_schema("contracts/pq_envelope.schema.json")
    _assert_valid(_jsonable(envelope.model_dump(mode="json")), schema)


def test_pq_envelope_requires_scope_when_current_limit_set():
    with pytest.raises(ValueError, match="current_limit_scope"):
        pq.PowerQualityEnvelope(
            pq_envelope_id=uuid4(), customer_id=uuid4(), phase_config="3P", current_limit_a=Decimal("100")
        )


# =========================================================================================================
# Telemetry "pq" extension and SCADA per-phase signal enum (S6.1, S6.2)
# =========================================================================================================


def test_telemetry_with_pq_block_matches_schema():
    msg = mqtt.Telemetry(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="LZ_NORTH",
        ts=NOW,
        soc_kwh=5.0,
        p_kw=-1.0,
        health="online",
        seq=1,
        epoch=1,
        pq=pq.TelemetryPq(v_rms=240.1, i_rms=12.5, freq_hz=59.98, pf=0.97, phase="AB"),
    )
    schema = _load_schema("mqtt/telemetry.schema.json")
    _assert_valid(msg.model_dump(mode="json", exclude_none=True), schema)


def test_telemetry_without_pq_block_still_matches_schema():
    msg = mqtt.Telemetry(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="LZ_NORTH",
        ts=NOW,
        soc_kwh=5.0,
        p_kw=-1.0,
        health="online",
        seq=1,
        epoch=1,
    )
    schema = _load_schema("mqtt/telemetry.schema.json")
    _assert_valid(msg.model_dump(mode="json"), schema)


def test_scada_bank_signal_new_enum_values_match_schema():
    schema = _load_schema("mqtt/scada_bank_signal.schema.json")
    for signal, unit in [
        ("VOLTAGE_A_PU", "pu"),
        ("CURRENT_A_PHASE_A", "A"),
        ("FREQUENCY_HZ", "Hz"),
        ("THD_V_PCT", "%"),
    ]:
        msg = mqtt.ScadaBankSignal(
            bank_id="bank-1", signal=signal, value=1.0, unit=unit, quality="good", ts=NOW
        )
        _assert_valid(msg.model_dump(mode="json"), schema)


# =========================================================================================================
# Waveform transport (S6.4)
# =========================================================================================================


def test_waveform_summary_fast_block_matches_schema():
    """A publish carrying only the fast sub-block (harmonics omitted, S6.4a bandwidth mitigation)."""
    msg = pq.WaveformSummary(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="LZ_NORTH",
        ts=NOW,
        v_rms_a=240.0,
        i_rms_a=10.0,
        freq_hz=60.01,
        pf_a=0.98,
        sync_source="ptp",
        sync_quality_ns=50.0,
    )
    schema = _load_schema("mqtt/pq_waveform_summary.schema.json")
    _assert_valid(msg.model_dump(mode="json"), schema)


def test_waveform_summary_with_harmonics_matches_schema():
    msg = pq.WaveformSummary(
        hub_id="hub-1",
        bank_id="bank-1",
        zone="LZ_NORTH",
        ts=NOW,
        v_rms_a=240.0,
        i_rms_a=10.0,
        freq_hz=60.01,
        pf_a=0.98,
        harmonics_i={"3": pq.HarmonicComponent(mag_pct=Decimal("1.2"), angle_deg=Decimal("40"))},
        sync_source="gps",
        sync_quality_ns=20.0,
    )
    schema = _load_schema("mqtt/pq_waveform_summary.schema.json")
    _assert_valid(_jsonable(msg.model_dump(mode="json")), schema)


def test_waveform_raw_capture_header_matches_schema():
    header = pq.WaveformRawCaptureHeader(
        hub_id="hub-1",
        phase_connection="ABC",
        ts=NOW,
        channels=["V_A", "V_B", "V_C", "I_A", "I_B", "I_C"],
        sync_source="ptp",
        sync_quality_ns=50.0,
        compression="delta_generic",
        trigger_reason="PQ_DEVIATION",
    )
    schema = _load_schema("mqtt/pq_waveform_raw.schema.json")
    _assert_valid(header.model_dump(mode="json"), schema)


def test_waveform_capture_request_matches_schema():
    req = pq.WaveformCaptureRequest(
        request_id=uuid4(), hub_id="hub-1", trigger_reason="API_REQUEST", issued_at=NOW, expires_at=NOW
    )
    schema = _load_schema("mqtt/waveform_capture_request.schema.json")
    _assert_valid(req.model_dump(mode="json"), schema)


# =========================================================================================================
# Calibration command/ack (S6.7)
# =========================================================================================================


def test_calibration_command_matches_schema_and_signing_payload_excludes_signature():
    cmd = pq.CalibrationCommand(
        calibration_id=uuid4(),
        hub_id="hub-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW,
        reference=pq.CalibrationReference(phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ptp"),
        correction=pq.CalibrationCorrection(freq_hz=0.01, phase_deg=1.5),
        bounds=pq.CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=2.0, max_phase_deg=5.0),
        key_id="guardian-2026a",
        signature="sig",
    )
    schema = _load_schema("mqtt/calibration_command.schema.json")
    _assert_valid(cmd.model_dump(mode="json", exclude_none=True), schema)

    payload = cmd.signing_payload()
    assert "signature" not in payload and "key_id" not in payload
    assert payload["hub_id"] == "hub-1"


def test_calibration_ack_matches_schema():
    ack = pq.CalibrationAck(
        calibration_id=uuid4(),
        hub_id="hub-1",
        applied=True,
        applied_at=NOW,
        resulting_offsets=pq.CalibrationOffsets(freq_hz=0.001, voltage_pct=0.1, phase_deg=0.2),
        status="APPLIED",
    )
    schema = _load_schema("mqtt/calibration_ack.schema.json")
    _assert_valid(ack.model_dump(mode="json"), schema)
