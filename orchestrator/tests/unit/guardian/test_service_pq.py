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

import pytest

from opengrid.core.crypto import private_key_from_seed, verify_payload
from opengrid.core.models.pq import CalibrationCommand, CalibrationReference
from opengrid.core.pq import CalibrationBounds, OffsetVector, PqEnvelopeLimits, PqMeasurement
from opengrid.guardian.ports import PqPorts
from opengrid.guardian.pq_ports import CalibrationFleetUsage, HubAssetSnapshot, ProposedCalibrationCommand

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
    # CalibrationLedgerPort fake: a durable per-hub counter and the claims, shared across "restarts".
    usage: CalibrationFleetUsage = field(default_factory=lambda: CalibrationFleetUsage(2000, 0, 0, 1))
    claims: dict = field(default_factory=dict)
    seq_by_hub: dict = field(default_factory=dict)
    signed: list = field(default_factory=list)
    alerts: list = field(default_factory=list)

    async def reserve(self, calibration_id, hub_id):
        if calibration_id in self.claims:
            return None
        self.seq_by_hub[hub_id] = self.seq_by_hub.get(hub_id, 0) + 1
        self.claims[calibration_id] = ("RESERVED", hub_id)
        return (1, self.seq_by_hub[hub_id])

    async def mark_signed(self, calibration_id):
        self.signed.append(calibration_id)
        self.claims[calibration_id] = ("SIGNED", self.claims[calibration_id][1])

    async def refuse(self, calibration_id, hub_id, reason):
        self.claims.setdefault(calibration_id, ("REFUSED", hub_id, reason))

    async def fleet_usage(self, *, window_s):
        return self.usage

    async def raise_alert(self, rule, summary, detail):
        self.alerts.append((rule, detail["reason"]))

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
        return PqPorts(self, self, self, self, self, self, calibration_ledger=self)  # type: ignore[arg-type]


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
    pq = _Pq()
    service = _calibration_service(fakes, guardian_config, signing_seed, pq)
    first = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    second = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    other = await service.evaluate_and_sign_calibration(_calibration(hub_id="hub-2"))
    assert first is not None and second is not None and other is not None
    assert (first.epoch, first.seq) < (second.epoch, second.seq)
    assert other.seq == 1

    restarted = _calibration_service(fakes, guardian_config, signing_seed, pq, now=NOW + timedelta(seconds=5))
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
    assert fakes.trace.calibration_verdicts[-1][1]["reason"] == "PQ_CALIBRATION_NOT_WIRED"


# --- durable ledger claim, fleet budget (#10), systemic-drift hold ------------------------------------------


async def test_the_sequence_comes_from_the_durable_ledger_not_the_process_or_the_wall_clock(
    fakes, guardian_config, signing_seed
):
    """Regression (#15): (epoch, seq) lived only in memory with a wall-clock epoch. It now comes from
    the ledger, so a restarted guardian continues the same per-hub counter."""
    pq = _Pq(seq_by_hub={"hub-1": 41})
    command = await _calibration_service(
        fakes, guardian_config, signing_seed, pq
    ).evaluate_and_sign_calibration(_calibration(hub_id="hub-1"))
    assert command is not None and (command.epoch, command.seq) == (1, 42)
    assert pq.signed == [command.calibration_id]


async def test_an_attempt_already_claimed_is_never_signed_again(fakes, guardian_config, signing_seed):
    """#20: the ledger INSERT is the atomic claim; a second evaluation of the same attempt signs nothing."""
    pq = _Pq()
    proposed = _calibration()
    service = _calibration_service(fakes, guardian_config, signing_seed, pq)
    assert await service.evaluate_and_sign_calibration(proposed) is not None
    assert await service.evaluate_and_sign_calibration(proposed) is None
    assert pq.signed == [proposed.calibration_id]


@pytest.mark.parametrize(
    ("usage", "reason"),
    [
        (
            CalibrationFleetUsage(2000, 40, 0, 1),
            "PQ_CALIBRATION_FLEET_BUDGET_EXCEEDED",
        ),  # 2% of 2,000 per hour
        (CalibrationFleetUsage(2000, 3, 10, 1), "PQ_CALIBRATION_CONCURRENCY_EXCEEDED"),
        (CalibrationFleetUsage(2000, 0, 0, 101), "PQ_CALIBRATION_SYSTEMIC_DRIFT_SUSPECTED"),  # > 5% flagged
        (CalibrationFleetUsage(0, 0, 0, 0), "PQ_CALIBRATION_BUDGET_UNKNOWN"),
    ],
)
async def test_fleet_caps_hold_calibration_and_alert(fakes, guardian_config, signing_seed, usage, reason):
    """#10: autonomous calibration had no fleet-wide cap (the sweep covers every hub each minute). The
    guardian now enforces a budget per window, a concurrency cap and a systemic-drift hold, and alerts."""
    pq = _Pq(usage=usage)
    proposed = _calibration()
    assert (
        await _calibration_service(fakes, guardian_config, signing_seed, pq).evaluate_and_sign_calibration(
            proposed
        )
        is None
    )
    assert pq.alerts == [("ALR-CALIBRATION-BUDGET", reason)]
    assert pq.claims[proposed.calibration_id][0] == "REFUSED" and pq.signed == []
    assert fakes.trace.calibration_verdicts[-1][1]["reason"] == reason


async def test_within_the_fleet_budget_a_calibration_is_signed(fakes, guardian_config, signing_seed):
    pq = _Pq(usage=CalibrationFleetUsage(2000, 39, 9, 100))
    assert (
        await _calibration_service(fakes, guardian_config, signing_seed, pq).evaluate_and_sign_calibration(
            _calibration()
        )
        is not None
    )
    assert pq.alerts == []


async def test_calibration_without_a_ledger_is_refused(fakes, guardian_config, signing_seed):
    pq = _Pq()
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = dataclasses.replace(
        fakes.as_ports(), pq=dataclasses.replace(pq.ports(), calibration_ledger=None)
    )
    assert await service.evaluate_and_sign_calibration(_calibration()) is None
    assert fakes.trace.calibration_verdicts[-1][1]["reason"] == "PQ_CALIBRATION_NOT_WIRED"
