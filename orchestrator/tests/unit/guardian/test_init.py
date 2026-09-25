"""The fixed module-level public interface (`orchestrator/INTERFACES.md`): `configure` + `evaluate_and_sign`."""

from __future__ import annotations

import pytest

import opengrid.guardian as guardian_module

from .conftest import make_batch_row, make_proposal, service_with, wire_default_passing_scenario


async def test_evaluate_and_sign_raises_before_configure(fakes, guardian_config, signing_seed):
    guardian_module._service = None  # simulate a fresh, unconfigured process
    batch = make_batch_row(make_proposal())
    with pytest.raises(RuntimeError, match="configure"):
        await guardian_module.evaluate_and_sign(batch)


async def test_configure_then_evaluate_and_sign_delegates(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    guardian_module.configure(service)
    try:
        verdict = await guardian_module.evaluate_and_sign(batch)
        assert verdict.outcome == "PASS"
    finally:
        guardian_module._service = None
