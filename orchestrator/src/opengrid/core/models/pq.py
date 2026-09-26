"""Service-profile and power-quality (PQ) contracts -- MVP-S+ increment (BUILD.md S2a.3-4).

Mirrors `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md` (single source of
truth for field names/meanings) and the schemas/DDL this module is generated alongside:

- Contract-side objects (validated at profile authoring/admission, not MQTT messages): `ServiceProfile`,
  `PowerQualityEnvelope` -- mirror `interfaces/contracts/service_profile.schema.json` and
  `interfaces/contracts/pq_envelope.schema.json`, and the `og.service_profile` / `og.pq_envelope` rows
  (`orchestrator/migrations/0010_service_profile.sql`).
- Fleet/asset-health row shapes: `HubInverterPq`, `PqWaveformSummaryRow`, `PqWaveformRawIndex`,
  `CalibrationAttempt`, `MaintenanceWorkOrder`, `AssetEvent` -- mirror `og.hub_inverter_pq` (as extended by
  `0011_asset_health.sql`) and that migration's new tables.
- Wire messages (mirror `interfaces/mqtt/*.schema.json` exactly, same rule as `models/mqtt.py`):
  `WaveformSummary`, `WaveformRawCaptureHeader`, `WaveformCaptureRequest`, `CalibrationCommand`,
  `CalibrationAck`. `TelemetryPq` (the `telemetry.schema.json` "pq" addition, S6.1) lives here too and is
  attached as an optional field on `models.mqtt.Telemetry` by that module.

If a field name or type here diverges from the schema/DDL file it mirrors, the schema/DDL is authoritative
(BUILD.md S1: "the repo copy is the single source of truth") -- fix this file to match, not the reverse.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from opengrid.core.models.engine import Tier

PhaseConnection = Literal["A", "B", "C", "AB", "BC", "CA", "ABC"]
SyncSource = Literal["ptp", "gps", "ntp_disciplined"]
WaveformTriggerReason = Literal["PQ_DEVIATION", "API_REQUEST", "ROTATING_AUDIT", "CALIBRATION_VERIFICATION"]
AssetState = Literal["OK", "WATCH", "DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HarmonicComponent(_Row):
    """One harmonic order's magnitude (% of fundamental) and angle (deg, relative to a sync reference)."""

    mag_pct: Decimal
    angle_deg: Decimal


Harmonics = dict[str, HarmonicComponent]  # key: harmonic order as a string, e.g. "3", "5"


# =========================================================================================================
# S6.1 telemetry extension (attached to models.mqtt.Telemetry.pq)
# =========================================================================================================


class TelemetryPq(_Wire):
    """Optional per-phase quality block on `Telemetry` (interfaces/mqtt/telemetry.schema.json S6.1)."""

    v_rms: float | None = None
    i_rms: float | None = None
    q_kvar: float | None = None
    freq_hz: float | None = None
    pf: float | None = None
    thd_v_pct: float | None = None
    thd_i_pct: float | None = None
    phase: PhaseConnection | None = None


# =========================================================================================================
# S1-S2 contract-side objects: ServiceProfile, PowerQualityEnvelope
# =========================================================================================================

ControlPrimitive = Literal[
    "OPEN_LOOP_SCHEDULE",
    "CLOSED_LOOP_REGULATION",
    "PRICE_RESPONSE",
    "CAPACITY_HOLD",
    "EVENT_SCHEDULE_TRACKING",
    "MODE_ISLAND_CONTROL",
]
TargetQuantity = Literal["KW", "KVAR", "LINE_CURRENT_A", "PIPE_TO_SOIL_V", "BANK_KVA", "NET_POWER_MW", "NONE"]
TargetScope = Literal["HUB", "BANK", "FEEDER", "CORRIDOR_LINE", "ADER", "SITE_METER"]
SetpointSource = Literal["PLAN", "MEASURED_FEEDBACK", "ISO_INSTRUCTION", "PRICE_FEED", "CUSTOMER_API"]
RampLimitUnit = Literal["kw_per_min", "a_per_min", "kvar_per_min", "mw_per_min"]


class ServiceProfile(_Row):
    """og.service_profile row / service_profile.schema.json (S1.3-1.5). The per-contract instance of
    dispatch-profile elements 4 (control) and 6 (performance) plus the pq_envelope binding -- not a new
    dispatch-profile element (S1.1)."""

    service_profile_id: UUID
    contract_id: UUID
    version: int = 1
    control_primitive: ControlPrimitive
    target_quantity: TargetQuantity
    target_scope: TargetScope
    setpoint_source: SetpointSource
    feedback_signal_ref: str | None = None
    response_time_s: Decimal
    ramp_limit: Decimal
    ramp_limit_unit: RampLimitUnit = "kw_per_min"
    sustain_duration_s: Decimal | None = None
    accuracy_tolerance: Decimal
    deadband: Decimal
    priority_tier: Tier
    mv_method: str
    settlement_metric: str
    pq_envelope_id: UUID
    failure_behaviour: str

    @model_validator(mode="after")
    def _check_feedback_and_target_constraints(self) -> ServiceProfile:
        """Mirrors service_profile.schema.json's `allOf` (S1.4): MEASURED_FEEDBACK requires a feedback
        signal ref, and CLOSED_LOOP_REGULATION cannot target NONE."""
        if self.setpoint_source == "MEASURED_FEEDBACK" and self.feedback_signal_ref is None:
            msg = "feedback_signal_ref is required when setpoint_source is MEASURED_FEEDBACK"
            raise ValueError(msg)
        if self.control_primitive == "CLOSED_LOOP_REGULATION" and self.target_quantity == "NONE":
            msg = "target_quantity cannot be NONE when control_primitive is CLOSED_LOOP_REGULATION"
            raise ValueError(msg)
        return self


PhaseConfig = Literal["1P", "SPLIT_PHASE", "3P"]
CurrentLimitScope = Literal["PER_PHASE", "SPECIFIC_LINE"]


class PowerQualityEnvelope(_Row):
    """og.pq_envelope row / pq_envelope.schema.json (S2). Per customer; a residential aggregate uses a
    fleet-default envelope shared by all HOME contracts unless tightened by an interconnection agreement."""

    pq_envelope_id: UUID
    customer_id: UUID
    phase_config: PhaseConfig
    max_phase_imbalance_pct: Decimal = Decimal("3.0")
    voltage_band_pct: Decimal = Decimal("5.0")
    ride_through_class: str = "CATEGORY_III"
    current_limit_a: Decimal | None = None
    current_limit_scope: CurrentLimitScope | None = None
    freq_tolerance_hz: Decimal = Decimal("0.5")
    rocof_limit_hz_s: Decimal | None = None
    pf_min: Decimal = Decimal("0.90")
    reactive_requirement: str | None = None
    thd_voltage_limit_pct: Decimal = Decimal("5.0")
    thd_current_limit_pct: Decimal = Decimal("5.0")
    flicker_pst_limit: Decimal | None = None

    @model_validator(mode="after")
    def _check_current_limit_scope_required(self) -> PowerQualityEnvelope:
        if self.current_limit_a is not None and self.current_limit_scope is None:
            msg = "current_limit_scope is required when current_limit_a is set"
            raise ValueError(msg)
        return self


# =========================================================================================================
# S3 / S8.5: per-hub inverter characterization and asset health
# =========================================================================================================


class HubInverterPq(_Row):
    """og.hub_inverter_pq row (0010_service_profile.sql S1.5, extended with asset-health columns by
    0011_asset_health.sql S8.5). Refreshed from a periodic sim/estimation job or measured waveform
    summaries (S6.5); never billed directly."""

    hub_id: str
    phase_connection: PhaseConnection
    kva_rating: Decimal
    pf_min_leading: Decimal = Decimal("0.90")
    pf_min_lagging: Decimal = Decimal("0.90")
    freq_offset_hz: Decimal = Decimal("0")
    freq_offset_std_hz: Decimal = Decimal("0.01")
    voltage_offset_pct: Decimal = Decimal("0")
    voltage_offset_std_pct: Decimal = Decimal("0.5")
    thd_current_pct: Decimal = Decimal("3.0")
    dominant_harmonics: Harmonics | None = None
    phase_angle_error_deg: Decimal = Decimal("0")
    response_time_ms: Decimal = Decimal("200")
    ride_through_class: str = "CATEGORY_III"
    quality_score: Decimal = Decimal("1.0")
    last_estimated_at: datetime | None = None
    # 0011_asset_health.sql additions (S5.5.2):
    asset_state: AssetState = "OK"
    asset_state_since: datetime | None = None
    consecutive_correctable_drifts: int = 0
    last_recalibration_at: datetime | None = None


class PqWaveformSummaryRow(_Row):
    """og.pq_waveform_summary row (0011_asset_health.sql S6.5, S8.5), one per hub per telemetry period."""

    hub_id: str
    ts: datetime
    v_rms_a: Decimal | None = None
    v_rms_b: Decimal | None = None
    v_rms_c: Decimal | None = None
    i_rms_a: Decimal | None = None
    i_rms_b: Decimal | None = None
    i_rms_c: Decimal | None = None
    freq_hz: Decimal | None = None
    pf_a: Decimal | None = None
    pf_b: Decimal | None = None
    pf_c: Decimal | None = None
    thd_v_pct_a: Decimal | None = None
    thd_v_pct_b: Decimal | None = None
    thd_v_pct_c: Decimal | None = None
    thd_i_pct_a: Decimal | None = None
    thd_i_pct_b: Decimal | None = None
    thd_i_pct_c: Decimal | None = None
    phase_angle_deg_a: Decimal | None = None
    phase_angle_deg_b: Decimal | None = None
    phase_angle_deg_c: Decimal | None = None
    harmonics_v: Harmonics | None = None
    harmonics_i: Harmonics | None = None
    sync_source: SyncSource | None = None
    sync_quality_ns: Decimal | None = None


class PqWaveformRawIndex(_Row):
    """og.pq_waveform_raw_index row (S6.5, S8.5) -- indexes an object-stored raw waveform capture."""

    capture_id: UUID
    hub_id: str
    ts: datetime
    trigger_reason: WaveformTriggerReason
    blob_ref: str
    channels: int
    sample_rate_hz: Decimal = Decimal("7680")
    cycles: int = 10
    retain_until: datetime | None = None


CalibrationOutcome = Literal["PENDING", "IMPROVED", "CORRECTED", "NO_CHANGE", "WORSE_ROLLED_BACK", "FAILED_NO_ACK"]


class CalibrationAttempt(_Row):
    """og.calibration_attempt row (S5.5.4, S8.5)."""

    calibration_id: UUID
    hub_id: str
    requested_at: datetime
    reference_phase_deg: Decimal
    reference_freq_hz: Decimal
    reference_amplitude_v: Decimal
    measured_offset_freq_hz: Decimal | None = None
    measured_offset_voltage_pct: Decimal | None = None
    measured_offset_phase_deg: Decimal | None = None
    correction_freq_hz: Decimal | None = None
    correction_voltage_pct: Decimal | None = None
    correction_phase_deg: Decimal | None = None
    command_batch_id: UUID | None = None
    outcome: CalibrationOutcome = "PENDING"
    verified_at: datetime | None = None


class MaintenanceWorkOrder(_Row):
    """og.maintenance_work_order row (S5.5.5, S8.5)."""

    work_order_id: UUID
    hub_id: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "URGENT"]
    evidence: dict[str, Any]
    status: Literal["OPEN", "IN_PROGRESS", "CLOSED", "CANCELLED"] = "OPEN"
    opened_at: datetime
    closed_at: datetime | None = None
    technician_notes: str | None = None


class AssetEvent(_Row):
    """og.asset_event row (S5.5.5, S8.5) -- a physical hardware action, never a dispatch "swap" (S5.5.1
    terminology note: substitution moves delivery between hubs; an inverter swap/replacement is hardware)."""

    asset_event_id: UUID
    hub_id: str
    work_order_id: UUID | None = None
    event_type: Literal["STATE_TRANSITION", "INVERTER_REPLACED", "RECOMMISSIONED"]
    from_state: str | None = None
    to_state: str | None = None
    old_inverter_serial: str | None = None
    new_inverter_serial: str | None = None
    old_firmware: str | None = None
    new_firmware: str | None = None
    reason_code: str | None = None
    occurred_at: datetime


# =========================================================================================================
# S6.4-6.7 wire messages: waveform transport, on-demand capture, calibration command/ack
# =========================================================================================================


class WaveformSummary(_Wire):
    """Hub -> orchestrator, `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/summary`, QoS 0 (S6.4a).
    Mirrors `pq_waveform_summary.schema.json`. `harmonics_v`/`harmonics_i` may be omitted on a "fast"
    sub-block publish (S6.4a's bandwidth mitigation) -- the harmonic-detail sub-block publishes at a
    slower cadence or on-change."""

    hub_id: str
    bank_id: str
    zone: str
    ts: datetime
    v_rms_a: float | None = None
    v_rms_b: float | None = None
    v_rms_c: float | None = None
    i_rms_a: float | None = None
    i_rms_b: float | None = None
    i_rms_c: float | None = None
    freq_hz: float | None = None
    pf_a: float | None = None
    pf_b: float | None = None
    pf_c: float | None = None
    thd_v_pct_a: float | None = None
    thd_v_pct_b: float | None = None
    thd_v_pct_c: float | None = None
    thd_i_pct_a: float | None = None
    thd_i_pct_b: float | None = None
    thd_i_pct_c: float | None = None
    phase_angle_deg_a: float | None = None
    phase_angle_deg_b: float | None = None
    phase_angle_deg_c: float | None = None
    harmonics_v: Harmonics | None = None
    harmonics_i: Harmonics | None = None
    sync_source: SyncSource
    sync_quality_ns: float


WaveformChannel = Literal["V", "I", "V_A", "V_B", "V_C", "I_A", "I_B", "I_C"]
WaveformCompression = Literal["none", "delta_generic"]


class WaveformRawCaptureHeader(_Wire):
    """Hub -> orchestrator, `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/raw`, QoS 1, triggered only
    (S6.4a-b). Mirrors `pq_waveform_raw.schema.json`'s structured header; the binary sample block that
    follows on the wire is out of scope for this model (S6.4a: binary, not base64-in-JSON) -- see
    `samples` on the JSON Schema for the test/fixture-only equivalent representation."""

    hub_id: str
    phase_connection: PhaseConnection
    ts: datetime
    sample_rate_hz: float = 7680.0
    cycles: int = 10
    channels: list[WaveformChannel]
    sync_source: SyncSource
    sync_quality_ns: float
    compression: WaveformCompression
    trigger_reason: WaveformTriggerReason


class WaveformCaptureRequest(_Wire):
    """Orchestrator -> hub, `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/request`, QoS 1 (S6.4b)."""

    request_id: UUID
    hub_id: str
    trigger_reason: WaveformTriggerReason
    issued_at: datetime
    expires_at: datetime


class CalibrationReference(_Wire):
    phase_deg: float
    freq_hz: float
    amplitude_v: float
    sync_source: SyncSource


class CalibrationCorrection(_Wire):
    freq_hz: float | None = None
    voltage_pct: float | None = None
    phase_deg: float | None = None


class CalibrationBounds(_Wire):
    max_freq_hz: float
    max_voltage_pct: float
    max_phase_deg: float


class CalibrationCommand(_Wire):
    """Guardian -> hub, `<root>/cmd/cal/<hub_id>`, QoS 1, guardian-signed (S6.7). Mirrors
    `calibration_command.schema.json` and the signed-envelope pattern of `models.mqtt.CommandBatch`."""

    calibration_id: UUID
    hub_id: str
    epoch: int
    seq: int
    issued_at: datetime
    expires_at: datetime
    reference: CalibrationReference
    correction: CalibrationCorrection
    bounds: CalibrationBounds
    key_id: str
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        """Fields covered by the Ed25519 signature (excludes key_id/signature), per interfaces/crypto.md."""
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"key_id", "signature"})
        return data


class CalibrationOffsets(_Wire):
    freq_hz: float
    voltage_pct: float
    phase_deg: float


class CalibrationAck(_Wire):
    """Hub -> orchestrator, `<root>/ack/cal/<hub_id>`, QoS 1 (S6.7). Mirrors `calibration_ack.schema.json`."""

    calibration_id: UUID
    hub_id: str
    applied: bool
    applied_at: datetime
    resulting_offsets: CalibrationOffsets
    status: Literal["APPLIED", "REJECTED", "EXPIRED"]
