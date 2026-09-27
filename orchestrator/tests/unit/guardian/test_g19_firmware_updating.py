"""r3.4.3 (with DISPATCH's M1): G-19's capability read counts firmware-UPDATING hubs as unavailable, so an
R-COMMIT-LOCK-OVERRIDE-L0 for the capacity a firmware campaign took out of service corroborates."""

from __future__ import annotations

from dataclasses import replace

from opengrid.guardian.repo import _FIRMWARE_UPDATING_SQL

from .conftest import make_batch_row, service_with
from .test_g19_evidence import _hub, _members, _reasons, _reduced

L0 = "R-COMMIT-LOCK-OVERRIDE-L0"


class _Updating:
    def __init__(self, hubs: set[str], *, fail: bool = False) -> None:
        self.hubs, self.fail, self.asked = hubs, fail, []

    async def updating_hub_ids(self, bank_id: str) -> set[str]:
        self.asked.append(bank_id)
        if self.fail:
            raise RuntimeError("db down")
        return set(self.hubs)


def _world(fakes, config, seed, port):
    """8 kW commitment reduced to 5 kW on a bank of two 5 kW hubs (10 kW rated)."""
    proposal, _ = _reduced(fakes, L0, frozen_kw="8.0", granted_kw="5.0")
    _members(fakes, {"hub-a": _hub(temp_c=25.0), "hub-b": _hub(temp_c=25.0)})
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), firmware_updating=port)
    return service, proposal


async def test_an_l0_shortfall_from_a_firmware_updating_hub_is_corroborated(
    fakes, guardian_config, signing_seed
):
    port = _Updating({"hub-b"})
    service, proposal = _world(fakes, guardian_config, signing_seed, port)

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "PASS" and port.asked


async def test_without_a_firmware_update_the_same_l0_claim_is_vetoed(fakes, guardian_config, signing_seed):
    service, proposal = _world(fakes, guardian_config, signing_seed, _Updating(set()))

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "VETOED" and "G-19" in verdict.vetoed_rule_ids
    assert any("UNVERIFIED" in r for r in _reasons(fakes))


async def test_a_failed_firmware_read_is_never_evidence(fakes, guardian_config, signing_seed):
    service, proposal = _world(fakes, guardian_config, signing_seed, _Updating({"hub-b"}, fail=True))

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "VETOED" and "G-19" in verdict.vetoed_rule_ids


def test_the_read_matches_the_engines_exclusion_states() -> None:
    sql = " ".join(_FIRMWARE_UPDATING_SQL.split())
    assert "j.state IN ('SENT', 'UPDATING')" in sql
    assert "(j.state = 'PENDING' AND j.command_id IS NOT NULL)" in sql
    assert "c.state <> 'ABORTED'" in sql
