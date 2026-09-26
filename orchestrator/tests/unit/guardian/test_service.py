"""`GuardianService.evaluate_and_sign` integration over in-memory fakes: PASS/VETO/PARTLY_VETOED/TIMEOUT
paths, signature round-trip with `core.crypto`, and "unsigned/forged batch never published" (BUILD.md
test list)."""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.core.crypto import generate_keypair, verify_payload
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import L2Instruction, ProposedItem

from .conftest import (
    BANK_ID,
    NOW,
    FakeBankMembers,
    make_bank_snapshot,
    make_batch_row,
    make_hub_snapshot,
    make_proposal,
    service_with,
    wire_default_passing_scenario,
)


async def test_pass_signs_and_verifies(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.outcome == "PASS"
    assert verdict.vetoed_rule_ids == []
    assert verdict.signature is not None
    assert verdict.signed_at is not None

    public_key = generate_keypair.__wrapped__ if False else None  # no-op placeholder, see below
    _ = public_key


async def test_pass_signature_verifies_against_public_key(fakes, guardian_config):
    seed, public = generate_keypair()
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, seed)

    verdict = await service.evaluate_and_sign(batch)

    payload = {
        "command_batch_id": str(batch.command_batch_id),
        "outcome": "PASS",
        "vetoed_rule_ids": [],
        "inputs_hash": verdict.inputs_hash,
        "signed_at": verdict.signed_at.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    assert verify_payload(public, payload, verdict.signature)


async def test_forged_signature_never_verifies(fakes, guardian_config, signing_seed):
    _other_seed, other_public = generate_keypair()
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    payload = {
        "command_batch_id": str(batch.command_batch_id),
        "outcome": "PASS",
        "vetoed_rule_ids": [],
        "inputs_hash": verdict.inputs_hash,
        "signed_at": verdict.signed_at.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }
    # Verifying against the WRONG public key must fail -- guardian's signature is not forgeable/reusable.
    assert not verify_payload(other_public, payload, verdict.signature)


async def test_vetoed_batch_carries_no_signature(fakes, guardian_config, signing_seed):
    proposal = make_proposal(p_kw_setpoint=999.0)  # exceeds hub power limit -> G-02
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.outcome in ("VETOED", "PARTLY_VETOED")
    assert verdict.signature is None
    assert verdict.signed_at is None
    assert "G-02" in verdict.vetoed_rule_ids


async def test_missing_trace_preimage_holds_never_signs(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.trace.preimage_exists = False
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.signature is None
    assert "G-14" in verdict.vetoed_rule_ids


async def test_g14_looks_up_the_preimage_by_the_batch_rows_trace_pointer(
    fakes, guardian_config, signing_seed
):
    """The pre-image is its own trace row: guardian must look it up by `trace_pre_image_id`, never by
    `command_batch_id` (which is not a trace id, so that lookup vetoed every batch as G-14 live)."""
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    await service.evaluate_and_sign(batch)

    assert fakes.trace.preimage_refs_checked == [batch.trace_pre_image_id]


async def test_batch_row_without_a_preimage_pointer_holds_never_signs(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal).model_copy(update={"trace_pre_image_id": None})
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.signature is None
    assert "G-14" in verdict.vetoed_rule_ids
    assert fakes.trace.preimage_refs_checked == []


async def test_unknown_proposal_never_signs(fakes, guardian_config, signing_seed):
    batch = make_batch_row(make_proposal())  # never added to fakes.proposals
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.signature is None
    assert verdict.outcome in ("VETOED", "PARTLY_VETOED")


async def test_clock_skew_holds_as_timeout_not_veto(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.clock.offset_ms = 500.0  # exceeds default 200 ms
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.outcome == "TIMEOUT"
    assert verdict.vetoed_rule_ids == ["G-20"]
    assert verdict.signature is None


async def test_clock_within_limit_proceeds_normally(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.clock.offset_ms = 50.0
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert verdict.outcome == "PASS"


async def test_processing_timeout_holds(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)

    async def slow_fetch(command_batch_id):
        await asyncio.sleep(1.0)
        return None

    fakes.proposals.fetch = slow_fetch  # type: ignore[method-assign]
    fast_config = GuardianConfig(key_path="", verdict_timeout_ms=5.0, cycle_interval_s=2.0)
    service = service_with(fakes, fast_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.outcome == "TIMEOUT"
    assert verdict.vetoed_rule_ids == []
    assert verdict.signature is None


async def test_stale_ledger_version_holds(fakes, guardian_config, signing_seed):
    proposal = make_proposal(ledger_version=1)
    wire_default_passing_scenario(fakes, proposal)
    fakes.ledger.version = 2  # guardian's own independent read disagrees with the batch
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert "G-09" in verdict.vetoed_rule_ids
    assert verdict.signature is None


async def test_safe_stop_engaged_blocks_signing(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.safe_stop.stopped.add(("BANK", BANK_ID))
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert verdict.signature is None
    assert "SAFE_STOP" in verdict.vetoed_rule_ids


async def test_l2_estop_blocks_nonzero_setpoint(fakes, guardian_config, signing_seed):
    proposal = make_proposal(p_kw_setpoint=3.0)
    wire_default_passing_scenario(fakes, proposal)
    fakes.l2_instructions.active[BANK_ID] = L2Instruction(kind="ESTOP", limit_kw=None)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert "G-15" in verdict.vetoed_rule_ids


async def test_g19_commitment_lock_blocks_unreasoned_reduction(fakes, guardian_config, signing_seed):
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id, obligation_granted_kw=Decimal("2.0"), reason_code=None
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert "G-19" in verdict.vetoed_rule_ids
    assert verdict.signature is None


def _reduced_commitment(fakes, reason_code: str):
    """A batch granting 2 kW against a 5 kW frozen commitment, claiming `reason_code`."""
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id, obligation_granted_kw=Decimal("2.0"), reason_code=reason_code
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    return proposal


def _bank_members(fakes, *socs_kwh: float) -> None:
    fakes.bank_members = FakeBankMembers()
    fakes.bank_members.members[BANK_ID] = [make_hub_snapshot(soc_kwh=soc) for soc in socs_kwh]


async def test_g19_commitment_lock_allows_override_reason(fakes, guardian_config, signing_seed):
    """L1 (homeowner reserve) is signed when the guardian's OWN telemetry shows the bank's hubs cannot
    carry the commitment: every member hub is at its reserve floor."""
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-OVERRIDE-L1")
    _bank_members(fakes, 7.84, 7.84)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "PASS"


@pytest.mark.parametrize(
    "reason_code",
    ["R-COMMIT-LOCK-INFEASIBLE", "R-COMMIT-LOCK-OVERRIDE-L0", "R-COMMIT-LOCK-OVERRIDE-L1"],
)
async def test_g19_forged_capability_override_is_vetoed(fakes, guardian_config, signing_seed, reason_code):
    """K13 regression: the guardian accepted any reduction below the lock whose item merely CLAIMED
    INFEASIBLE/L0/L1. Its own read of the bank's hubs (2 x 5 kW, well above reserve) covers the 5 kW
    commitment, so the claim is false and the reduction is vetoed."""
    proposal = _reduced_commitment(fakes, reason_code)
    _bank_members(fakes, 20.0, 20.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "VETOED" and verdict.signature is None
    assert "G-19" in verdict.vetoed_rule_ids
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "COMMIT_LOCK_OVERRIDE_INFEASIBLE_UNVERIFIED" in reasons


async def test_g19_capability_override_without_an_independent_read_is_vetoed(
    fakes, guardian_config, signing_seed
):
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    assert fakes.bank_members is None  # no guardian-side capability read wired

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-19" in verdict.vetoed_rule_ids and verdict.signature is None


async def test_g19_infeasible_is_signed_when_the_bank_really_cannot_deliver(
    fakes, guardian_config, signing_seed
):
    """One member hub a sliver above reserve (~1.7 kW sustainable over the 10 s lease) and one offline
    (0 kW, K1): the bank's own capability is below the 5 kW commitment, so the claim is corroborated."""
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    fakes.bank_members = FakeBankMembers()
    offline = replace(make_hub_snapshot(soc_kwh=30.0), health="offline")
    fakes.bank_members.members[BANK_ID] = [make_hub_snapshot(soc_kwh=7.845, p_kw=5.0), offline]

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "PASS"


async def test_g19_forged_l2_override_is_vetoed(fakes, guardian_config, signing_seed):
    """K13 regression: an L2 override claim with no utility instruction in the guardian's own L2 read."""
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-OVERRIDE-L2")
    _bank_members(fakes, 7.84)  # even with capability short, L2 needs an actual instruction

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "VETOED" and "G-19" in verdict.vetoed_rule_ids
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "COMMIT_LOCK_OVERRIDE_L2_UNVERIFIED" in reasons


async def test_g19_l2_override_is_signed_under_an_active_instruction(fakes, guardian_config, signing_seed):
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-OVERRIDE-L2")
    fakes.l2_instructions.active[BANK_ID] = L2Instruction(kind="LIMIT", limit_kw=10.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "PASS"


async def test_g19_cannot_be_bypassed_by_omitting_obligation(fakes, guardian_config, signing_seed):
    """GUARD-01: an obligation with an ACTIVE commitment that the batch's items never mention at all
    must still be evaluated by G-19 -- guardian enumerates active obligations itself, so "just don't
    include it" is not a way to dodge the commitment lock."""
    proposal = make_proposal()  # no obligation_id on its single item
    wire_default_passing_scenario(fakes, proposal)
    obligation_id = uuid4()
    fakes.commitments.active_by_bank.setdefault(proposal.bank_id, set()).add(obligation_id)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert "G-19" in verdict.vetoed_rule_ids
    assert verdict.signature is None


async def test_g19_cannot_be_bypassed_by_null_obligation_id(fakes, guardian_config, signing_seed):
    """GUARD-01: relabelling an item's obligation_id=None does not remove the obligation from
    guardian's own independently-enumerated active set, so it still counts as new_kw=0 -> VETO."""
    obligation_id = uuid4()
    proposal = make_proposal(obligation_id=None, reason_code="SELECTOR")
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.active_by_bank.setdefault(proposal.bank_id, set()).add(obligation_id)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert "G-19" in verdict.vetoed_rule_ids
    assert verdict.signature is None


async def test_g19_as_release_disabled_by_default_still_vetoes(fakes, guardian_config, signing_seed):
    """GUARD-01: R-AS-RELEASE only excuses a reduction when as_release_enabled is explicitly turned on
    (default False, per GuardianConfig.as_release_enabled) -- an unreasoned-looking release must not
    sneak past the independently-enumerated obligation just because it carries that reason code."""
    assert guardian_config.as_release_enabled is False
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id, obligation_granted_kw=Decimal("0.0"), reason_code="R-AS-RELEASE"
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert "G-19" in verdict.vetoed_rule_ids
    assert verdict.signature is None


async def test_partly_vetoed_when_only_some_hubs_affected(fakes, guardian_config, signing_seed):
    from opengrid.guardian.ports import ProposedBatch, ProposedItem

    proposal = ProposedBatch(
        command_batch_id=uuid4(),
        bank_id=BANK_ID,
        cycle_id="cycle-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        ledger_version=1,
        items=[
            ProposedItem(hub_id="hub-ok", p_kw_setpoint=3.0, reason_code="SELECTOR"),
            ProposedItem(hub_id="hub-bad", p_kw_setpoint=999.0, reason_code="SELECTOR"),
        ],
    )
    fakes.proposals.add(proposal)
    fakes.ledger.version = 1
    fakes.hubs.hubs["hub-ok"] = make_hub_snapshot(prev_p_kw=3.0)
    fakes.hubs.hubs["hub-bad"] = make_hub_snapshot(prev_p_kw=999.0)
    fakes.banks.banks[BANK_ID] = make_bank_snapshot()
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert verdict.outcome == "PARTLY_VETOED"
    assert verdict.signature is None


async def test_unknown_hub_fails_closed(fakes, guardian_config, signing_seed):
    proposal = make_proposal(hub_id="hub-does-not-exist")
    fakes.proposals.add(proposal)
    fakes.ledger.version = proposal.ledger_version
    fakes.banks.banks[BANK_ID] = make_bank_snapshot()
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert verdict.signature is None
    assert "G-01" in verdict.vetoed_rule_ids


async def test_g01_energy_lease_vetoes_discharge_that_drains_below_reserve_within_lease(
    fakes, guardian_config, signing_seed
):
    """K1: a command well within instantaneous power/reserve bounds can still be vetoed once the
    guardian projects the hub's OWN independently-read SoC across the command's full lease -- capacity
    (kW) headroom alone is not enough; energy above reserve must hold for the whole hold duration."""
    from dataclasses import replace

    proposal = make_proposal(p_kw_setpoint=-30.0)
    proposal = replace(proposal, expires_at=proposal.issued_at + timedelta(hours=1))
    wire_default_passing_scenario(fakes, proposal)
    fakes.hubs.hubs["hub-0001"] = make_hub_snapshot(soc_kwh=10.0, p_kw=30.0)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)

    assert verdict.signature is None
    assert "G-01-ENERGY" in verdict.vetoed_rule_ids
    # The instantaneous G-01 check alone passes (soc 10.0 > reserve 7.84 + margin) -- proving this is a
    # genuinely independent, additional check, not a duplicate of G-01.
    assert "G-01" not in verdict.vetoed_rule_ids


async def test_verdict_is_traced_on_pass(fakes, guardian_config, signing_seed):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    await service.evaluate_and_sign(batch)
    assert len(fakes.trace.appended) == 1
    assert fakes.trace.appended[0][0] == batch.command_batch_id


async def test_tracing_failure_never_crashes_verdict_path(fakes, guardian_config, signing_seed):
    from .conftest import FailingTrace

    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.trace = FailingTrace()
    fakes.proposals.add(proposal)  # re-add since fakes.trace swap doesn't affect proposals
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
    assert verdict.outcome == "PASS"  # tracing the verdict failed, but signing itself must still succeed


async def test_discharge_on_an_overloaded_bank_is_not_vetoed_by_g03(fakes, guardian_config, signing_seed):
    """Regression (live 2026-09-26): G-03 vetoed every discharge batch because the bank's SCADA load
    already exceeded its rating, although discharging adds no load (it relieves the bank). G-03 bounds
    the load a batch ADDS."""
    proposal = make_proposal(p_kw_setpoint=-3.0)
    wire_default_passing_scenario(fakes, proposal)
    fakes.banks.banks[proposal.bank_id] = make_bank_snapshot(bank_load_kva=3000.0, kva_rating=600.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-03" not in verdict.vetoed_rule_ids


async def test_charging_an_overloaded_bank_is_still_vetoed_by_g03(fakes, guardian_config, signing_seed):
    proposal = make_proposal(p_kw_setpoint=3.0)
    wire_default_passing_scenario(fakes, proposal)
    fakes.hubs.hubs[proposal.items[0].hub_id] = make_hub_snapshot(prev_p_kw=0.0, p_kw=11.0)
    fakes.banks.banks[proposal.bank_id] = make_bank_snapshot(bank_load_kva=3000.0, kva_rating=600.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-03" in verdict.vetoed_rule_ids


async def test_veto_trace_records_each_violation_reason(fakes, guardian_config, signing_seed):
    """A veto must be explainable from the trace alone (live 2026-09-26: only rule ids were traced)."""
    proposal = make_proposal(p_kw_setpoint=3.0)
    wire_default_passing_scenario(fakes, proposal)
    fakes.hubs.hubs[proposal.items[0].hub_id] = make_hub_snapshot(prev_p_kw=0.0, p_kw=11.0)
    fakes.banks.banks[proposal.bank_id] = make_bank_snapshot(bank_load_kva=3000.0, kva_rating=600.0)

    await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(make_batch_row(proposal))

    (_batch_id, payload) = fakes.trace.appended[-1]
    assert {
        "rule_id": "G-03",
        "reason": "BANK_KVA_LIMIT",
        "hub_id": BANK_ID,
        "obligation_id": None,
    } in payload["violations"]


# --- K4/G-03 in both directions ---------------------------------------------------------------------


def _bank_batch(
    fakes, setpoints_and_prev: list[tuple[float, float]], *, bank_load_kva: float, kva_rating: float
):
    """A batch of hubs `hub-i` moving from `prev` to `setpoint` on one bank (hubs rated 20 kW so G-02/G-04
    stay quiet with a generous per-hub ramp)."""
    proposal = replace(
        make_proposal(),
        items=[ProposedItem(f"hub-{i}", sp, "SELECTOR") for i, (sp, _prev) in enumerate(setpoints_and_prev)],
    )
    wire_default_passing_scenario(fakes, proposal)
    for i, (_sp, prev) in enumerate(setpoints_and_prev):
        hub = make_hub_snapshot(soc_kwh=30.0, prev_p_kw=prev, p_kw=20.0)
        fakes.hubs.hubs[f"hub-{i}"] = replace(hub, params=replace(hub.params, ramp_kw_per_s=100.0))
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(bank_load_kva=bank_load_kva, kva_rating=kva_rating)
    return proposal


async def test_g03_discharge_into_reverse_flow_beyond_rating_is_vetoed(fakes, guardian_config, signing_seed):
    """K4 regression: G-03 only ran when a batch added charge, so discharge that drove the bank into
    reverse flow past its rating was never checked. 10 kVA import - 80 kW more discharge = 70 kW export
    through a 75 kVA bank (limit (75-5)*0.95 = 66.5)."""
    guardian_config = GuardianConfig(key_path="", cycle_interval_s=2.0, inverter_cap_kw=20.0)
    proposal = _bank_batch(fakes, [(-20.0, 0.0)] * 4, bank_load_kva=10.0, kva_rating=75.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-03" in verdict.vetoed_rule_ids and verdict.signature is None
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "BANK_KVA_LIMIT_REVERSE_FLOW" in reasons


async def test_g03_discharge_within_the_reverse_rating_is_signed(fakes, guardian_config, signing_seed):
    guardian_config = GuardianConfig(key_path="", cycle_interval_s=2.0, inverter_cap_kw=20.0)
    proposal = _bank_batch(fakes, [(-20.0, 0.0)] * 3, bank_load_kva=10.0, kva_rating=75.0)  # -> 50 kW export

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-03" not in verdict.vetoed_rule_ids


async def test_g03_cutting_discharge_on_an_importing_bank_is_bounded_too(
    fakes, guardian_config, signing_seed
):
    """Stopping 20 kW of discharge adds 20 kW of import (60 -> 80 kVA on a 75 kVA bank) although the batch
    adds no charge -- the old additional-charge-only rule let this through."""
    guardian_config = GuardianConfig(key_path="", cycle_interval_s=2.0, inverter_cap_kw=20.0)
    proposal = _bank_batch(fakes, [(0.0, -20.0)], bank_load_kva=60.0, kva_rating=75.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-03" in verdict.vetoed_rule_ids


async def test_g03_relief_on_an_overloaded_bank_is_still_signed(fakes, guardian_config, signing_seed):
    guardian_config = GuardianConfig(key_path="", cycle_interval_s=2.0, inverter_cap_kw=20.0)
    proposal = _bank_batch(fakes, [(-20.0, 0.0)], bank_load_kva=3000.0, kva_rating=600.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-03" not in verdict.vetoed_rule_ids


@pytest.mark.parametrize("age_s", [math.inf, 31.0])
async def test_g03_missing_or_stale_scada_is_vetoed(fakes, guardian_config, signing_seed, age_s):
    """K4 regression: with no SCADA reading the bank looked like 0 kVA and G-03 passed everything."""
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(bank_load_age_s=age_s)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "VETOED" and "G-03" in verdict.vetoed_rule_ids
    assert {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]} == {"BANK_LOAD_STALE"}


# --- K4/G-06 now that banks carry a feeder -----------------------------------------------------------


async def test_g06_evaluates_for_a_bank_with_a_feeder(fakes, guardian_config, signing_seed):
    """G-06 was inert because no bank had a feeder_id; with the seeded feeder it binds firm events."""
    guardian_config = GuardianConfig(
        key_path="",
        cycle_interval_s=2.0,
        inverter_cap_kw=20.0,
        default_feeder_ramp_ceiling_kw_per_min=600.0,  # 20 kW per 2 s tick
    )
    proposal = replace(
        _bank_batch(fakes, [(-20.0, 0.0)] * 2, bank_load_kva=10.0, kva_rating=600.0), is_firm_event=True
    )
    fakes.proposals.add(proposal)
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(
        bank_load_kva=10.0, kva_rating=600.0, feeder_id="feeder-LZ_NORTH-00"
    )

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-06" in verdict.vetoed_rule_ids

    fakes.banks.banks[BANK_ID] = make_bank_snapshot(bank_load_kva=10.0, kva_rating=600.0, feeder_id=None)
    unassigned = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert "G-06" not in unassigned.vetoed_rule_ids


# --- K12/G-20 fail-closed at the service -------------------------------------------------------------


async def test_clock_port_failure_holds_as_timeout(fakes, guardian_config, signing_seed):
    class BrokenClock:
        async def offset_from_ntp_ms(self) -> float:
            raise RuntimeError("clock source unavailable")

    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.clock = BrokenClock()  # type: ignore[assignment]

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "TIMEOUT" and verdict.vetoed_rule_ids == ["G-20"] and verdict.signature is None


@pytest.mark.parametrize("offset_ms", [math.inf, -math.inf, math.nan])
async def test_non_finite_clock_offset_holds_as_timeout(fakes, guardian_config, signing_seed, offset_ms):
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.clock.offset_ms = offset_ms

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "TIMEOUT" and verdict.vetoed_rule_ids == ["G-20"]


# --- the published batch is exactly the evaluated one ------------------------------------------------


async def test_evaluated_proposal_is_what_was_checked_not_a_later_pre_image(
    fakes, guardian_config, signing_seed
):
    """A second RT_ALLOCATION row for the same batch id written after evaluation must not be what gets
    signed and published (main.py used to re-read the pre-image after PASS)."""
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))
    fakes.proposals.add(replace(proposal, items=[ProposedItem("hub-0001", 999.0, "SELECTOR")]))

    assert verdict.outcome == "PASS"
    assert service.evaluated_proposal(proposal.command_batch_id) == proposal


@pytest.mark.parametrize("reason_code", ["R-COMMIT-LOCK-INFEASIBLE", "R-COMMIT-LOCK-OVERRIDE-L1"])
async def test_g19_capability_override_on_a_stale_bank_is_vetoed(
    fakes, guardian_config, signing_seed, reason_code
):
    """Gap (a) regression: with the whole bank's telemetry stale, the seen capability is 0 kW and an
    INFEASIBLE claim looked corroborated. Unseen hubs now count at their full rating, so the claim fails
    closed with its own reason."""
    proposal = _reduced_commitment(fakes, reason_code)
    fakes.bank_members = FakeBankMembers()
    fakes.bank_members.members[BANK_ID] = [replace(make_hub_snapshot(soc_kwh=7.84), health="stale")] * 2

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "COMMIT_LOCK_OVERRIDE_EVIDENCE_STALE" in reasons


async def test_g19_shortfall_that_holds_even_with_unseen_hubs_at_full_rating_is_signed(
    fakes, guardian_config, signing_seed
):
    """One live hub at reserve (0 kW) and one stale 3 kW hub: even at its full rating the bank cannot
    carry 5 kW, so the INFEASIBLE claim stands."""
    proposal = _reduced_commitment(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    fakes.bank_members = FakeBankMembers()
    fakes.bank_members.members[BANK_ID] = [
        make_hub_snapshot(soc_kwh=7.84),
        replace(make_hub_snapshot(soc_kwh=30.0, p_kw=3.0), health="stale"),
    ]

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "PASS"


# --- K13 need basis (owner decision 2026-09-26) and best-effort SHORTFALL ----------------------------------


class _Profiles:
    def __init__(self, sources: dict) -> None:
        self.sources = sources

    async def setpoint_source(self, obligation_id):
        return self.sources.get(obligation_id)


def _need_basis_service(fakes, config, seed, sources: dict | None):
    service = service_with(fakes, config, seed)
    profiles = _Profiles(sources) if sources is not None else None
    service.ports = replace(fakes.as_ports(), service_profiles=profiles)
    return service


def _closed_loop_batch(fakes, *, extra: tuple | None = None):
    """Need-basis obligation A: 5 kW reserved maximum, 2 kW granted (measured need). `extra` adds another
    obligation B as (obligation_id, committed_kw, granted_kw) on the same bank and cycle."""
    need_id = uuid4()
    items = [ProposedItem("hub-0001", 3.0, "R-GRANT-CLOSED-LOOP", need_id, Decimal("2.0"))]
    if extra is not None:
        other_id, _committed, granted = extra
        items.append(ProposedItem("hub-0002", 3.0, "R-GRANT-COMMITTED", other_id, granted))
    proposal = replace(make_proposal(), items=items)
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[need_id] = Decimal("5.0")
    fakes.prior_grants.prior[need_id] = Decimal("5.0")
    if extra is not None:
        fakes.commitments.frozen[extra[0]] = extra[1]
        if extra[1] > 0:
            fakes.commitments.active_by_bank[BANK_ID].add(extra[0])
    return proposal, need_id


async def test_need_basis_grant_below_the_reserved_maximum_is_signed(fakes, guardian_config, signing_seed):
    proposal, need_id = _closed_loop_batch(fakes)
    service = _need_basis_service(fakes, guardian_config, signing_seed, {need_id: "MEASURED_FEEDBACK"})

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))
    assert verdict.outcome == "PASS"


@pytest.mark.parametrize("source", ["PLAN", None])
async def test_the_same_grant_on_a_fixed_profile_is_vetoed(fakes, guardian_config, signing_seed, source):
    proposal, need_id = _closed_loop_batch(fakes)
    service = _need_basis_service(fakes, guardian_config, signing_seed, {need_id: source} if source else {})

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "NEED_BASIS_PROFILE_NOT_MEASURED_FEEDBACK" in reasons


async def test_need_basis_without_a_profile_read_is_vetoed(fakes, guardian_config, signing_seed):
    proposal, _need_id = _closed_loop_batch(fakes)
    service = _need_basis_service(fakes, guardian_config, signing_seed, None)
    assert "G-19" in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


@pytest.mark.parametrize(
    ("committed", "granted"),
    [(Decimal("1.0"), Decimal("4.0")), (Decimal("0"), Decimal("3.0"))],  # over its own commitment / none here
)
async def test_need_basis_reservation_used_by_another_obligation_is_vetoed(
    fakes, guardian_config, signing_seed, committed, granted
):
    other_id = uuid4()
    proposal, need_id = _closed_loop_batch(fakes, extra=(other_id, committed, granted))
    service = _need_basis_service(fakes, guardian_config, signing_seed, {need_id: "MEASURED_FEEDBACK"})

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
    reasons = {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert "NEED_BASIS_RESERVATION_REASSIGNED" in reasons


async def test_another_obligation_within_its_own_commitment_does_not_block_need_basis(
    fakes, guardian_config, signing_seed
):
    other_id = uuid4()
    proposal, need_id = _closed_loop_batch(fakes, extra=(other_id, Decimal("3.0"), Decimal("3.0")))
    service = _need_basis_service(fakes, guardian_config, signing_seed, {need_id: "MEASURED_FEEDBACK"})
    assert (await service.evaluate_and_sign(make_batch_row(proposal))).outcome == "PASS"


# --- K13 ERCOT_AS capacity hold: R-GRANT-AS-HOLD (migration 0020) ------------------------------------------


class _AsAwards:
    def __init__(self, service_types: dict, deployed: set) -> None:
        self.service_types = service_types
        self.deployed = deployed

    async def service_type(self, obligation_id):
        return self.service_types.get(obligation_id)

    async def deployment_active(self, obligation_id):
        return obligation_id in self.deployed


def _as_hold_batch(fakes, *, extra: tuple | None = None):
    """AS award A: 5 kW committed, held at 0 kW. `extra` adds obligation B as (id, committed_kw, granted_kw)."""
    as_id = uuid4()
    items = [ProposedItem("hub-0001", 0.0, "R-GRANT-AS-HOLD", as_id, Decimal("0"))]
    if extra is not None:
        other_id, _committed, granted = extra
        items.append(ProposedItem("hub-0002", 3.0, "R-GRANT-COMMITTED", other_id, granted))
    proposal = replace(make_proposal(), items=items)
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[as_id] = Decimal("5.0")
    fakes.prior_grants.prior[as_id] = Decimal("5.0")
    if extra is not None:
        fakes.commitments.frozen[extra[0]] = extra[1]
        if extra[1] > 0:
            fakes.commitments.active_by_bank[BANK_ID].add(extra[0])
    return proposal, as_id


def _as_hold_service(fakes, config, seed, awards: _AsAwards | None):
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), as_awards=awards)
    return service


def _g19_reasons(fakes) -> set:
    return {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}


async def test_held_as_award_with_no_deployment_is_signed(fakes, guardian_config, signing_seed):
    proposal, as_id = _as_hold_batch(fakes)
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards({as_id: "ERCOT_AS"}, set()))
    assert (await service.evaluate_and_sign(make_batch_row(proposal))).outcome == "PASS"


async def test_held_as_award_while_deployed_is_vetoed(fakes, guardian_config, signing_seed):
    """A deployment (for this award or for all AS) is active: the award must deliver; holding it is a K13
    reduction that needs the normal override/shortfall reasons."""
    proposal, as_id = _as_hold_batch(fakes)
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards({as_id: "ERCOT_AS"}, {as_id}))

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
    assert "AS_HOLD_WHILE_DEPLOYED" in _g19_reasons(fakes)


@pytest.mark.parametrize(
    ("committed", "granted"),
    [(Decimal("1.0"), Decimal("4.0")), (Decimal("0"), Decimal("3.0"))],  # over its own commitment / none here
)
async def test_held_as_award_with_its_reservation_borrowed_is_vetoed(
    fakes, guardian_config, signing_seed, committed, granted
):
    other_id = uuid4()
    proposal, as_id = _as_hold_batch(fakes, extra=(other_id, committed, granted))
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards({as_id: "ERCOT_AS"}, set()))

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
    assert "AS_HOLD_RESERVATION_REASSIGNED" in _g19_reasons(fakes)


async def test_another_obligation_within_its_commitment_does_not_block_an_as_hold(
    fakes, guardian_config, signing_seed
):
    other_id = uuid4()
    proposal, as_id = _as_hold_batch(fakes, extra=(other_id, Decimal("3.0"), Decimal("3.0")))
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards({as_id: "ERCOT_AS"}, set()))
    assert (await service.evaluate_and_sign(make_batch_row(proposal))).outcome == "PASS"


@pytest.mark.parametrize("service_type", ["ERCOT_ENERGY", "PIPELINE", None])
async def test_as_hold_claimed_on_a_non_as_obligation_is_vetoed(
    fakes, guardian_config, signing_seed, service_type
):
    proposal, as_id = _as_hold_batch(fakes)
    types = {as_id: service_type} if service_type else {}
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards(types, set()))

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.signature is None and "AS_HOLD_NOT_AN_AS_AWARD" in _g19_reasons(fakes)


async def test_as_hold_without_the_award_read_is_vetoed(fakes, guardian_config, signing_seed):
    proposal, _as_id = _as_hold_batch(fakes)
    service = _as_hold_service(fakes, guardian_config, signing_seed, None)
    assert "G-19" in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


async def test_as_hold_reason_does_not_excuse_a_different_obligation(fakes, guardian_config, signing_seed):
    """The hold is per obligation: a second, non-held obligation cut below its lock is still vetoed."""
    other_id = uuid4()
    proposal, as_id = _as_hold_batch(fakes, extra=(other_id, Decimal("3.0"), Decimal("1.0")))
    fakes.prior_grants.prior[other_id] = Decimal("3.0")
    service = _as_hold_service(fakes, guardian_config, signing_seed, _AsAwards({as_id: "ERCOT_AS"}, set()))
    assert "G-19" in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


async def test_best_effort_shortfall_grant_is_signed_when_the_guardian_confirms_the_shortfall(
    fakes, guardian_config, signing_seed
):
    """Owner decision: a mid-window SHORTFALL is best effort; the partial grant carries the shortfall reason
    and is corroborated like the override it maps to (bank capability short here)."""
    proposal = _reduced_commitment(fakes, "R-SHORTFALL-BANK-CAPACITY")
    _bank_members(fakes, 7.84, 7.84)
    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.outcome == "PASS"


@pytest.mark.parametrize("reason_code", ["R-SHORTFALL-BANK-CAPACITY", "R-SHORTFALL-NO-SUBSTITUTE"])
async def test_best_effort_shortfall_claim_is_vetoed_when_the_bank_could_deliver(
    fakes, guardian_config, signing_seed, reason_code
):
    proposal = _reduced_commitment(fakes, reason_code)
    _bank_members(fakes, 20.0, 20.0)
    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )
    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids


async def test_best_effort_l2_shortfall_needs_an_active_instruction(fakes, guardian_config, signing_seed):
    proposal = _reduced_commitment(fakes, "R-SHORTFALL-L2-INSTRUCTION")
    assert (
        "G-19"
        in (
            await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
                make_batch_row(proposal)
            )
        ).vetoed_rule_ids
    )
    fakes.l2_instructions.active[BANK_ID] = L2Instruction(kind="LIMIT", limit_kw=10.0)
    assert (
        await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(make_batch_row(proposal))
    ).outcome == "PASS"


async def test_default_guardian_signs_a_dual_unit_home_at_its_20_kw_rating(
    fakes, guardian_config, signing_seed
):
    """Demo-critical regression: with the default config (11 kW per-unit inverter cap) every dual-unit
    (20 kW) home command above 11 kW was vetoed by G-02."""
    assert guardian_config.inverter_cap_kw == 11.0
    proposal = make_proposal(p_kw_setpoint=-20.0)
    wire_default_passing_scenario(fakes, proposal)
    hub = make_hub_snapshot(soc_kwh=60.0, prev_p_kw=-20.0, p_kw=20.0)
    fakes.hubs.hubs[proposal.items[0].hub_id] = replace(
        hub, params=replace(hub.params, e_kwh=78.4, r_kwh=15.68, units=2)
    )
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(bank_load_kva=100.0, kva_rating=600.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-02" not in verdict.vetoed_rule_ids and verdict.outcome == "PASS"


async def test_default_guardian_vetoes_a_mis_seeded_single_unit_home_at_20_kw(
    fakes, guardian_config, signing_seed
):
    """Review fix (G-02 dead per-unit cap): p_kw mis-seeded at 20 kW on a single-unit home is vetoed."""
    proposal = make_proposal(p_kw_setpoint=-20.0)
    wire_default_passing_scenario(fakes, proposal)
    hub = make_hub_snapshot(soc_kwh=30.0, prev_p_kw=-20.0, p_kw=20.0)
    fakes.hubs.hubs[proposal.items[0].hub_id] = replace(hub, params=replace(hub.params, units=1))
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(bank_load_kva=100.0, kva_rating=600.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome in ("VETOED", "PARTLY_VETOED") and "G-02" in verdict.vetoed_rule_ids
    assert verdict.signature is None


# --- TS-06-16: ZONE-scope safe stop ----------------------------------------------------------------------


async def test_ts_06_16_a_zone_stop_holds_only_the_banks_in_that_zone(fakes, guardian_config, signing_seed):
    """K8: a ZONE-scoped stop refuses batches for banks in that zone and nowhere else. Regression: og-guardian
    was started with an empty bank->zone map, so it never saw a ZONE stop at all."""
    fakes.safe_stop.stopped.add(("ZONE", "LZ_NORTH"))
    in_zone = make_proposal(bank_id="bank-000")
    other_zone = make_proposal(bank_id="bank-001")
    for proposal in (in_zone, other_zone):
        wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = replace(fakes.as_ports(), zones_by_bank={"bank-000": "LZ_NORTH", "bank-001": "LZ_SOUTH"})

    stopped = await service.evaluate_and_sign(make_batch_row(in_zone))
    running = await service.evaluate_and_sign(make_batch_row(other_zone))

    assert stopped.signature is None and "SAFE_STOP" in stopped.vetoed_rule_ids
    assert running.outcome == "PASS"


# --- ALR-CLOCK-QUALITY -------------------------------------------------------------------------------------


class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def raise_alert(self, rule, severity, summary, condition_key, detail):
        self.calls.append(("raise", rule, severity))

    async def clear_alert(self, rule, condition_key):
        self.calls.append(("clear", rule))


async def test_clock_hold_raises_alr_clock_quality_once_and_clears_on_recovery(
    fakes, guardian_config, signing_seed
):
    alerts = _Alerts()
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = replace(fakes.as_ports(), alerts=alerts)

    fakes.clock.offset_ms = 5_000.0
    for _ in range(3):
        assert (await service.evaluate_and_sign(make_batch_row(proposal))).outcome == "TIMEOUT"
    assert alerts.calls == [("raise", "ALR-CLOCK-QUALITY", "critical")]

    fakes.clock.offset_ms = 1.0
    await service.evaluate_and_sign(make_batch_row(proposal))
    await service.evaluate_and_sign(make_batch_row(proposal))
    assert alerts.calls == [("raise", "ALR-CLOCK-QUALITY", "critical"), ("clear", "ALR-CLOCK-QUALITY")]


async def test_a_failed_verdict_trace_is_counted_and_alerted_and_the_verdict_stands(
    fakes, guardian_config, signing_seed
):
    """K11/K7: `_trace_verdict`'s False return is no longer ignored -- a metric and a warning alert -- but
    the control flow is unchanged: the verdict is still signed and returned."""
    from opengrid.guardian.service import guardian_trace_verdict_failures_total

    from .conftest import FailingTrace

    alerts = _Alerts()
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    fakes.trace = FailingTrace()
    fakes.proposals.add(proposal)
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = replace(fakes.as_ports(), alerts=alerts)
    before = guardian_trace_verdict_failures_total._value.get()

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "PASS" and verdict.signature is not None
    assert guardian_trace_verdict_failures_total._value.get() == before + 1
    assert alerts.calls == [("raise", "ALR-TRACE-VERDICT-WRITE-FAILED", "warning")]


async def test_a_written_verdict_trace_raises_no_alert(fakes, guardian_config, signing_seed):
    alerts = _Alerts()
    proposal = make_proposal()
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, guardian_config, signing_seed)
    service.ports = replace(fakes.as_ports(), alerts=alerts)

    await service.evaluate_and_sign(make_batch_row(proposal))

    assert alerts.calls == []


async def test_batch_outcome_counts_only_explicit_vetoes(fakes, guardian_config, signing_seed):
    proposal = make_proposal(p_kw_setpoint=999.0)  # G-02 item veto
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, guardian_config, signing_seed)
    verdict = await service.evaluate_and_sign(make_batch_row(proposal))
    outcome = service.batch_outcome(verdict)
    assert outcome is not None and (outcome.bank_id, outcome.commands, outcome.vetoed_commands) == (
        BANK_ID,
        1,
        1,
    )

    fakes.clock.offset_ms = 5_000.0
    held = await service.evaluate_and_sign(make_batch_row(make_proposal()))
    assert service.batch_outcome(held) is None  # a G-20 hold never read a proposal: not counted
