"""Tests for ogsim.fleet.calibration -- CalibrationCommand verification and application (Â§5.5.4, Â§6.7).

Covers: signature verification, expiry bounds, rate limiting, bounds-exceeded
rejection, correctable vs hardware drift outcomes, and the calibration_ack
shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import FleetConfig, MqttSettings
from ogsim.common.crypto import sign
from ogsim.fleet.calibration import apply_calibration, build_calibration_ack
from ogsim.fleet.pq import PqAnomalyManager, build_inverter_pq_state
from ogsim.fleet.state import build_fleet_state

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")
RATE_LIMIT_S = 86400.0
NOW = datetime.now(UTC)
NOW_TS = NOW.timestamp()


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _build_pq(seed: int = 0):
    config = FleetConfig(mqtt=MQTT, hub_count=10, bank_count=1)
    rng = np.random.default_rng(seed)
    state = build_fleet_state(config, rng)
    pq = build_inverter_pq_state(config, state, rng)
    return pq


def _command(
    guardian_key: Ed25519PrivateKey,
    *,
    hub_id: str = "hub-00000",
    epoch: int = 1,
    seq: int = 1,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    correction: dict | None = None,
    bounds: dict | None = None,
    unsigned: bool = False,
    wrong_key: bool = False,
) -> dict:
    issued_at = issued_at or NOW - timedelta(seconds=1)
    expires_at = expires_at or NOW + timedelta(seconds=60)
    command = {
        "calibration_id": str(uuid.uuid4()),
        "hub_id": hub_id,
        "epoch": epoch,
        "seq": seq,
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "reference": {"phase_deg": 0.0, "freq_hz": 60.0, "amplitude_v": 240.0, "sync_source": "ptp"},
        "correction": correction if correction is not None else {"freq_hz": 0.05, "voltage_pct": 1.0},
        "bounds": bounds
        if bounds is not None
        else {"max_freq_hz": 0.5, "max_voltage_pct": 5.0, "max_phase_deg": 10.0},
    }
    signing_key = Ed25519PrivateKey.generate() if wrong_key else guardian_key
    signing_fields = {
        k: command[k]
        for k in (
            "calibration_id",
            "hub_id",
            "epoch",
            "seq",
            "issued_at",
            "expires_at",
            "reference",
            "correction",
            "bounds",
        )
    }
    command["key_id"] = "guardian-test"
    command["signature"] = "" if unsigned else sign(signing_key, signing_fields)
    return command


# ---------------------------------------------------------------------------
# Signature checks
# ---------------------------------------------------------------------------


def test_valid_signed_command_is_applied(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key)
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.applied is True
    assert outcome.status == "APPLIED"


def test_missing_signature_rejected_as_bad_signature(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key, unsigned=True)
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.applied is False
    assert outcome.status == "REJECTED"
    assert outcome.reject_reason == "BAD_SIGNATURE"


def test_wrong_key_rejected_as_bad_signature(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key, wrong_key=True)
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.reject_reason == "BAD_SIGNATURE"


def test_unknown_hub_rejected_before_signature_check(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key, hub_id="hub-not-real")
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.reject_reason == "UNKNOWN_HUB"


# ---------------------------------------------------------------------------
# Expiry bounds
# ---------------------------------------------------------------------------


def test_not_yet_valid_command_rejected_as_expired(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    now = datetime.now(UTC)
    command = _command(
        guardian_key, issued_at=now + timedelta(seconds=10), expires_at=now + timedelta(seconds=40)
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=now.timestamp(), rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.status == "EXPIRED"
    assert outcome.applied is False


def test_past_expiry_command_rejected_as_expired(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    now = datetime.now(UTC)
    command = _command(
        guardian_key, issued_at=now - timedelta(seconds=40), expires_at=now - timedelta(seconds=10)
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=now.timestamp(), rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.status == "EXPIRED"


# ---------------------------------------------------------------------------
# Bounds violation
# ---------------------------------------------------------------------------


def test_correction_exceeding_bounds_is_rejected(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(
        guardian_key,
        correction={"freq_hz": 1.0},
        bounds={"max_freq_hz": 0.1, "max_voltage_pct": 5.0, "max_phase_deg": 10.0},
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.status == "REJECTED"
    assert outcome.reject_reason == "BOUNDS_EXCEEDED"


def test_correction_within_bounds_is_accepted(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(
        guardian_key,
        correction={"freq_hz": 0.05},
        bounds={"max_freq_hz": 0.1, "max_voltage_pct": 5.0, "max_phase_deg": 10.0},
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.applied is True


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_second_attempt_within_rate_limit_window_is_rejected(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    first = _command(guardian_key, epoch=1, seq=1)
    outcome1 = apply_calibration(
        pq, anomalies, first, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome1.applied is True

    second = _command(guardian_key, epoch=1, seq=2, expires_at=NOW + timedelta(seconds=300))
    outcome2 = apply_calibration(
        pq, anomalies, second, guardian_key.public_key(), now=NOW_TS + 100.0, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome2.status == "REJECTED"
    assert outcome2.reject_reason == "RATE_LIMITED"


def test_attempt_after_rate_limit_window_elapses_is_accepted(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    first = _command(guardian_key, epoch=1, seq=1)
    apply_calibration(pq, anomalies, first, guardian_key.public_key(), now=NOW_TS, rate_limit_s=100.0)

    second = _command(guardian_key, epoch=1, seq=2, expires_at=NOW + timedelta(seconds=300))
    outcome2 = apply_calibration(
        pq, anomalies, second, guardian_key.public_key(), now=NOW_TS + 200.0, rate_limit_s=100.0
    )
    assert outcome2.applied is True


# ---------------------------------------------------------------------------
# Correctable vs hardware drift outcomes
# ---------------------------------------------------------------------------


def test_correctable_drift_is_fully_corrected_by_a_matching_command(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq(seed=11)
    anomalies = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    anomalies.start(
        "d1",
        "calibration_drift_correctable",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.1, "target_voltage_offset_pct": 1.0},
        start=0.0,
        duration_s=None,
    )
    baseline_freq = float(pq.freq_offset_baseline_hz[idx])
    baseline_voltage = float(pq.voltage_offset_baseline_pct[idx])
    gap_freq = float(pq.freq_offset_hz[idx]) - baseline_freq
    gap_voltage = float(pq.voltage_offset_pct[idx]) - baseline_voltage

    command = _command(
        guardian_key,
        hub_id=hub_id,
        correction={"freq_hz": abs(gap_freq) + 1.0, "voltage_pct": abs(gap_voltage) + 1.0},
        bounds={"max_freq_hz": 5.0, "max_voltage_pct": 10.0, "max_phase_deg": 20.0},
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.outcome_label == "CORRECTED"
    assert pq.freq_offset_hz[idx] == pytest.approx(baseline_freq, abs=1e-9)
    assert pq.voltage_offset_pct[idx] == pytest.approx(baseline_voltage, abs=1e-9)


def test_correctable_drift_is_partially_improved_by_an_undersized_correction(
    guardian_key: Ed25519PrivateKey,
) -> None:
    pq = _build_pq(seed=12)
    anomalies = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    anomalies.start(
        "d2",
        "calibration_drift_correctable",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.3, "target_voltage_offset_pct": 3.0},
        start=0.0,
        duration_s=None,
    )
    pre_residual_freq = abs(float(pq.freq_offset_hz[idx]) - float(pq.freq_offset_baseline_hz[idx]))

    command = _command(
        guardian_key,
        hub_id=hub_id,
        correction={"freq_hz": pre_residual_freq * 0.5},
        bounds={"max_freq_hz": 5.0, "max_voltage_pct": 10.0, "max_phase_deg": 20.0},
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.outcome_label == "IMPROVED"


def test_hardware_drift_is_never_moved_by_any_command(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq(seed=13)
    anomalies = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    idx = pq.indices_for_hub(hub_id)[0]
    anomalies.start(
        "d3",
        "calibration_drift_hardware",
        "hub",
        hub_id,
        {"target_freq_offset_hz": 0.3, "target_voltage_offset_pct": 3.0},
        start=0.0,
        duration_s=None,
    )
    pre_freq = float(pq.freq_offset_hz[idx])
    pre_voltage = float(pq.voltage_offset_pct[idx])

    command = _command(
        guardian_key,
        hub_id=hub_id,
        correction={"freq_hz": 1.0, "voltage_pct": 1.0},
        bounds={"max_freq_hz": 5.0, "max_voltage_pct": 10.0, "max_phase_deg": 20.0},
    )
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.outcome_label == "NO_CHANGE"
    assert pq.freq_offset_hz[idx] == pytest.approx(pre_freq)
    assert pq.voltage_offset_pct[idx] == pytest.approx(pre_voltage)


def test_no_active_drift_and_zero_magnitude_correction_reports_already_corrected(
    guardian_key: Ed25519PrivateKey,
) -> None:
    """No active calibration_drift_* anomaly means the unit's offsets already equal baseline
    (zero residual), so even a no-op correction reports CORRECTED, not NO_CHANGE (NO_CHANGE is
    reserved for calibration_drift_hardware, which never moves)."""
    pq = _build_pq(seed=14)
    anomalies = PqAnomalyManager(pq)
    hub_id = "hub-00000"
    command = _command(guardian_key, hub_id=hub_id, correction={})
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    assert outcome.outcome_label == "CORRECTED"


# ---------------------------------------------------------------------------
# Ack shape
# ---------------------------------------------------------------------------


def test_build_calibration_ack_matches_expected_shape(guardian_key: Ed25519PrivateKey) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key)
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    ack = build_calibration_ack(outcome, applied_at="2026-09-25T12:00:00.000Z")
    assert ack["calibration_id"] == outcome.calibration_id
    assert ack["hub_id"] == outcome.hub_id
    assert ack["applied"] is True
    assert ack["status"] == "APPLIED"
    assert ack["applied_at"] == "2026-09-25T12:00:00.000Z"
    assert ack["reject_reason"] is None


def test_build_calibration_ack_for_rejected_outcome_has_no_resulting_offsets(
    guardian_key: Ed25519PrivateKey,
) -> None:
    pq = _build_pq()
    anomalies = PqAnomalyManager(pq)
    command = _command(guardian_key, unsigned=True)
    outcome = apply_calibration(
        pq, anomalies, command, guardian_key.public_key(), now=NOW_TS, rate_limit_s=RATE_LIMIT_S
    )
    ack = build_calibration_ack(outcome, applied_at="2026-09-25T12:00:00.000Z")
    assert ack["applied"] is False
    assert ack["resulting_offsets"] is None
    assert ack["reject_reason"] == "BAD_SIGNATURE"
