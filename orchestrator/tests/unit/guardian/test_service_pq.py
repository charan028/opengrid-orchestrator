"""K14 wiring in `GuardianService` (G-21..G-25, 06-service-profiles-and-power-quality.md S5.3/S6.7): the
PQ checks run only behind a bank serving a non-default envelope, a DEGRADED hub is excluded from a
PQ-sensitive delivery (G-24, item-level), and a calibration command is signed only after G-20 and G-25."""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from opengrid.core.crypto import private_key_from_seed, verify_payload
from opengrid.core.models.pq import CalibrationCommand, CalibrationReference
from opengrid.core.pq import CalibrationBounds, OffsetVector, PqEnvelopeLimits, PqMeasurement
from opengrid.guardian.ports import PqPorts
from opengrid.guardian.pq_ports import HubAssetSnapshot, ProposedCalibrationCommand

from .conftest import NOW, make_batch_row, make_proposal, service_with, wire_default_passing_scenario

LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=2.0,
    voltage_band_pct=2.0,
    freq_tolerance_hz=0.05,
    pf_min=0.95,
    thd_voltage_limit_pct=3.0,
    thd_current_limit_pct=5.0,
)
CLEAN = PqMeasurement(
    imbalance_pct=0.5,
    voltage_deviation_pct=0.5,
    freq_deviation_hz=0.01,
    pf=0.99,
    thd_voltage_pct=1.0,
    thd_current_pct=2.0,
)


@dataclass
class _Pq:
    limits: PqEnvelopeLimits | None = LIMITS
    measurement: PqMeasurement = CLEAN
    asset: HubAssetSnapshot | None = field(default_factory=lambda: HubAssetSnapshot("OK", "CATEGORY_III"))
    last_attempt: float | None = None
    sensitive_grant: bool = False

    async def tightest_active_limits(self, bank_id):
        return self.limits

    async def aggregate_measurement(self, bank_id):
        return self.measurement, False

    async def snapshot(self, hub_id):
        return self.asset

    async def last_attempt_epoch_s(self, hub_id):
        return self.last_attempt

    async def max_bounds_for_hub(self, hub_id):
        return CalibrationBounds(max_freq_hz=0.1, max_voltage_pct=2.0, max_phase_deg=5.0)

    async def has_active_non_default_envelope_grant(self, hub_id):
        return self.sensitive_grant

    def ports(self) -> PqPorts:
        return PqPorts(self, self, self, self, self, self)  # type: ignore[arg-type]


def _with_pq(fakes, pq: _Pq):
    fakes_ports = fakes.as_ports()
    return dataclasses.replace(fakes_ports, pq=pq.ports())


async def _verdict(fakes, config, seed, pq: _Pq):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, config, seed)
    service.ports = _with_pq(fakes, pq)
    return await service.evaluate_and_sign(make_batch_row(proposal))


async def test_no_sensitive_envelope_behind_the_bank_means_no_pq_gate(fakes, guardian_config, signing_seed):
    verdict = await _verdict(fakes, guardian_config, signing_seed, _Pq(limits=None, asset=None))
    assert verdict.outcome == "PASS"


async def test_clean_measurement_and_ok_asset_pass(fakes, guardian_config, signing_seed):
    verdict = await _verdict(fakes, guardian_config, signing_seed, _Pq())
    assert verdict.outcome == "PASS"


async def test_thd_breach_behind_a_sensitive_bank_is_vetoed_g22(fakes, guardian_config, signing_seed):
    breach = dataclasses.replace(CLEAN, thd_current_pct=9.0)
    verdict = await _verdict(fakes, guardian_config, signing_seed, _Pq(measurement=breach))
    assert verdict.outcome == "VETOED"
    assert "G-22" in verdict.vetoed_rule_ids


async def test_degraded_hub_is_excluded_from_a_sensitive_delivery_g24(fakes, guardian_config, signing_seed):
    verdict = await _verdict(
        fakes, guardian_config, signing_seed, _Pq(asset=HubAssetSnapshot("DEGRADED", "CATEGORY_III"))
    )
    assert verdict.outcome == "PARTLY_VETOED"
    assert verdict.vetoed_rule_ids == ["G-24"]


def _calibration(*, hub_id: str = "hub-1", calibration_id: UUID | None = None) -> ProposedCalibrationCommand:
    return ProposedCalibrationCommand(
        hub_id=hub_id,
        correction=OffsetVector(freq_hz=0.01, voltage_pct=0.5, phase_deg=1.0),
        bounds=CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.0),
        calibration_id=calibration_id or uuid4(),
        reference=CalibrationReference(
            phase_deg=0.0, freq_hz=60.0, amplitude_v=240.0, sync_source="ntp_disciplined"
        ),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )


def _calibration_service(fakes, config, seed, pq: _Pq, *, now=NOW):
    service = service_with(fakes, config, seed, now=now)
    service.ports = _with_pq(fakes, pq)
    return service


def _public_key(seed: bytes) -> bytes:
    return private_key_from_seed(seed).public_key().public_bytes_raw()


async def test_calibration_is_signed_when_g20_and_g25_pass(fakes, guardian_config, signing_seed):
    proposed = _calibration()
    command = await _calibration_service(
        fakes, guardian_config, signing_seed, _Pq()
    ).evaluate_and_sign_calibration(proposed)

    assert isinstance(command, CalibrationCommand)
    assert command.calibration_id == proposed.calibration_id and command.hub_id == "hub-1"
    assert command.key_id == guardian_config.key_id
    (calibration_id, payload) = fakes.trace.calibration_verdicts[-1]
    assert calibration_id == proposed.calibration_id and payload["outcome"] == "SIGNED"


async def test_calibration_signature_covers_the_full_wire_envelope_the_hub_verifies(
    fakes, guardian_config, signing_seed
):
    """Regression: the signature covered only {hub_id, correction, bounds}, so every hub rejected it as
    BAD_SIGNATURE (the hub verifies calibration_id, epoch, seq, issued_at, expires_at, reference too)."""
    command = await _calibration_service(
        fakes, guardian_config, signing_seed, _Pq()
    ).evaluate_and_sign_calibration(_calibration())
    assert command is not None
    wire = json.loads(command.model_dump_json())
    signed_fields = {k: v for k, v in wire.items() if k not in ("key_id", "signature")}
    assert set(signed_fields) == {
        "calibration_id",
        "hub_id",
        "epoch",
        "seq",
        "issued_at",
        "expires_at",
        "reference",
        "correction",
        "bounds",
    }
    assert verify_payload(_public_key(signing_seed), signed_fields, command.signature)
    tampered = {**signed_fields, "seq": signed_fields["seq"] + 1}
    assert not verify_payload(_public_key(signing_seed), tampered, command.signature)


async def test_calibration_epoch_and_seq_are_assigned_by_the_guardian_and_strictly_increase(
    fakes, guardian_config, signing_seed
):
    service = _calibration_service(fakes, guardian_config, signing_seed, _Pq())
    first = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    second = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    other = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-2"))
    assert first is not None and second is not None and other is not None
    assert (first.epoch, first.seq) < (second.epoch, second.seq)
    assert other.seq == 1

    restarted = _calibration_service(
        fakes, guardian_config, signing_seed, _Pq(), now=NOW + timedelta(seconds=5)
    )
    after_restart = await restarted.evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    assert after_restart is not None
    assert (after_restart.epoch, after_restart.seq) > (second.epoch, second.seq)


async def test_calibration_is_held_while_the_hub_serves_a_sensitive_grant(
    fakes, guardian_config, signing_seed
):
    proposed = _calibration()
    service = _calibration_service(fakes, guardian_config, signing_seed, _Pq(sensitive_grant=True))
    assert await service.evaluate_and_sign_calibration(proposed) is None
    (calibration_id, payload) = fakes.trace.calibration_verdicts[-1]
    assert calibration_id == proposed.calibration_id
    assert payload["outcome"] == "REFUSED" and payload["reason"] == "PQ_CALIBRATION_ACTIVE_SENSITIVE_GRANT"


async def test_calibration_is_held_when_the_clock_is_out_of_limit(fakes, guardian_config, signing_seed):
    fakes.clock.offset_ms = math.inf
    service = _calibration_service(fakes, guardian_config, signing_seed, _Pq())
    assert await service.evaluate_and_sign_calibration(_calibration()) is None
    assert fakes.trace.calibration_verdicts[-1][1]["rule_id"] == "G-20"


async def test_calibration_is_held_when_the_lease_is_already_expired(fakes, guardian_config, signing_seed):
    service = _calibration_service(
        fakes, guardian_config, signing_seed, _Pq(), now=NOW + timedelta(minutes=5)
    )
    assert await service.evaluate_and_sign_calibration(_calibration()) is None
    assert fakes.trace.calibration_verdicts[-1][1]["reason"] == "PQ_CALIBRATION_LEASE_INVALID"


async def test_a_signed_calibration_is_withheld_when_its_verdict_cannot_be_traced(
    fakes, guardian_config, signing_seed
):
    """K10: a signed command leaves the guardian only once its verdict record is durable."""
    from .conftest import FailingTrace

    fakes.trace = FailingTrace()
    service = _calibration_service(fakes, guardian_config, signing_seed, _Pq())
    assert await service.evaluate_and_sign_calibration(_calibration()) is None


async def test_calibration_without_pq_ports_is_refused(fakes, guardian_config, signing_seed):
    service = service_with(fakes, guardian_config, signing_seed)
    assert await service.evaluate_and_sign_calibration(_calibration()) is None
    assert fakes.trace.calibration_verdicts[-1][1]["reason"] == "PQ_PORTS_NOT_WIRED"
