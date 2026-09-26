"""`GuardianService.evaluate_and_sign` integration over in-memory fakes: PASS/VETO/PARTLY_VETOED/TIMEOUT
paths, signature round-trip with `core.crypto`, and "unsigned/forged batch never published" (BUILD.md
test list)."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from decimal import Decimal
from uuid import uuid4

from opengrid.core.crypto import generate_keypair, verify_payload
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import L2Instruction

from .conftest import (
    BANK_ID,
    NOW,
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


async def test_g19_commitment_lock_allows_override_reason(fakes, guardian_config, signing_seed):
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id,
        obligation_granted_kw=Decimal("2.0"),
        reason_code="R-COMMIT-LOCK-OVERRIDE-L1",
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    batch = make_batch_row(proposal)
    service = service_with(fakes, guardian_config, signing_seed)

    verdict = await service.evaluate_and_sign(batch)
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
