"""Owner decision 2026-09-26, end to end: a mid-window SHORTFALL never stops dispatch. The REAL allocator
cycle gives a SHORTFALL obligation its maximum feasible kW with an `R-SHORTFALL-*` code, and the REAL
guardian service signs that partial grant (PASS) when its own reads corroborate the shortfall -- and
vetoes the same code when they do not (a best-effort code is never a free pass)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    Schedule,
)
from opengrid.core.reasons import LOCK_REASON_BY_SHORTFALL
from opengrid.guardian.ports import L2Instruction

from .conftest import (
    BANK_ID,
    FakeBankMembers,
    make_batch_row,
    make_hub_snapshot,
    make_proposal,
    service_with,
    wire_default_passing_scenario,
)


def _allocator_partial_grant(*, instructions: tuple, committed_kw: float) -> tuple[str, float]:
    """(reason_code, granted_kw) the real allocator gives a SHORTFALL obligation on a 60 kW bank."""
    obligation_id = str(uuid4())
    hub = HubSnapshot(
        hub_id="h1",
        bank_id="b1",
        free_discharge_kw=60.0,
        health="OK",
        soc_kwh=1e6,
        reserve_kwh=0.0,
        e_kwh=1e6,
    )
    call = ObligationCall(
        obligation_id=obligation_id,
        bank_id="b1",
        service_type="PARTNER_CAPACITY",
        tier="T3",
        committed_kw=committed_kw,
        eligible_hub_ids=("h1",),
        in_shortfall=True,
    )
    result = cycle(
        datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
        FleetState(hubs=(hub,), banks=(BankSnapshot(bank_id="b1", capability_kw=100.0, kva_rating=100.0),)),
        LedgerView(calls=(call,)),
        Schedule(),
        {},
        instructions,
    )
    grant = next(g for g in result.grants if g.obligation_id == obligation_id)
    return grant.reason_code, grant.granted_kw


def _guardian_batch(fakes, reason_code: str):
    """The allocator's reason on a 2 kW grant against a 5 kW frozen commitment, through the guardian."""
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id, obligation_granted_kw=Decimal("2.0"), reason_code=reason_code
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    return proposal


async def _verdict(fakes, config, seed, proposal):
    return await service_with(fakes, config, seed).evaluate_and_sign(make_batch_row(proposal))


def _members(fakes, *socs_kwh: float) -> None:
    fakes.bank_members = FakeBankMembers()
    fakes.bank_members.members[BANK_ID] = [make_hub_snapshot(soc_kwh=soc) for soc in socs_kwh]


async def test_no_substitute_shortfall_keeps_dispatching_and_is_signed(fakes, guardian_config, signing_seed):
    reason, granted_kw = _allocator_partial_grant(instructions=(), committed_kw=80.0)
    assert reason == "R-SHORTFALL-NO-SUBSTITUTE" and reason in LOCK_REASON_BY_SHORTFALL
    assert granted_kw == 60.0  # the maximum feasible kW, not a stop

    proposal = _guardian_batch(fakes, reason)
    _members(fakes, 7.84, 7.84)  # the guardian's own read: the bank really cannot carry it
    verdict = await _verdict(fakes, guardian_config, signing_seed, proposal)
    assert verdict.outcome == "PASS"


async def test_the_same_code_is_vetoed_when_the_guardian_sees_capacity(fakes, guardian_config, signing_seed):
    reason, _ = _allocator_partial_grant(instructions=(), committed_kw=80.0)
    proposal = _guardian_batch(fakes, reason)
    _members(fakes, 20.0, 20.0)
    verdict = await _verdict(fakes, guardian_config, signing_seed, proposal)
    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids


async def test_l2_limit_shortfall_is_signed_under_the_active_instruction(
    fakes, guardian_config, signing_seed
):
    limit = (Instruction(scope="BANK", scope_ref="b1", kind="LIMIT", limit_kw=20.0),)
    reason, granted_kw = _allocator_partial_grant(instructions=limit, committed_kw=50.0)
    assert reason == "R-SHORTFALL-L2-INSTRUCTION"
    assert 0 < granted_kw <= 20.0  # dispatch continues at the instructed limit

    proposal = _guardian_batch(fakes, reason)
    fakes.l2_instructions.active[BANK_ID] = L2Instruction(kind="LIMIT", limit_kw=10.0)
    verdict = await _verdict(fakes, guardian_config, signing_seed, proposal)
    assert verdict.outcome == "PASS"
