"""K14 wiring in `GuardianService` (G-21..G-25, 06-service-profiles-and-power-quality.md S5.3/S6.7): the
PQ checks run only behind a bank serving a non-default envelope, a DEGRADED hub is excluded from a
PQ-sensitive delivery (G-24, item-level), and a calibration command is signed only after G-20 and G-25."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from opengrid.core.pq import CalibrationBounds, OffsetVector, PqEnvelopeLimits, PqMeasurement
from opengrid.guardian.ports import PqPorts
from opengrid.guardian.pq_ports import HubAssetSnapshot, ProposedCalibrationCommand

from .conftest import make_batch_row, make_proposal, service_with, wire_default_passing_scenario

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


def _calibration() -> ProposedCalibrationCommand:
    return ProposedCalibrationCommand(
        hub_id="hub-1",
        correction=OffsetVector(freq_hz=0.01, voltage_pct=0.5, phase_deg=1.0),
        bounds=CalibrationBounds(max_freq_hz=0.05, max_voltage_pct=1.0, max_phase_deg=2.0),
    )


async def test_calibration_is_signed_when_g20_and_g25_pass(fakes, guardian_config, signing_seed):
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = _with_pq(fakes, _Pq())
    assert await service.evaluate_and_sign_calibration(_calibration()) is not None


async def test_calibration_is_held_while_the_hub_serves_a_sensitive_grant(
    fakes, guardian_config, signing_seed
):
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = _with_pq(fakes, _Pq(sensitive_grant=True))
    assert await service.evaluate_and_sign_calibration(_calibration()) is None
