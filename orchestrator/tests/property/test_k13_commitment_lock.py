"""K13 commitment lock (00-invariants.md K13; G-19): a commitment shrinks only for L0/L1/L2/infeasible."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core import reasons
from opengrid.core.limits import check_commitment_lock
from opengrid.guardian.checks import CAPABILITY_OVERRIDE_REASONS
from opengrid.guardian.ports import ActiveObligation, L2Instruction, ProposedItem

from .support import BASE_HUB, HUB_ID, Signer, evaluate, make_hub, passing_world

SIGNER = Signer.new()
OVERRIDES = sorted(reasons.COMMIT_LOCK_OVERRIDE_REASONS)
_REDUCTION_REASONS = [None, "R-BETTER-PRICE", reasons.R_SUBSTITUTION, reasons.R_AS_RELEASE, *OVERRIDES]
_kw = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
_frozen_kw = st.decimals(min_value="0.5", max_value=5, places=1)


@given(_kw, _kw, _kw, st.sampled_from(_REDUCTION_REASONS), st.booleans())
def test_k13_a_reduction_below_the_floor_needs_an_allowed_reason(
    new_kw, frozen_kw, prior_kw, reason, as_enabled
):
    result = check_commitment_lock(new_kw, frozen_kw, prior_kw, reason, as_release_enabled=as_enabled)

    allowed = (
        new_kw >= min(frozen_kw, prior_kw) - 1e-9
        or reason in OVERRIDES
        or (reason == reasons.R_AS_RELEASE and as_enabled)
    )
    assert result.ok == allowed


@given(_kw, _kw, _kw)
def test_k13_ancillary_release_and_a_better_price_never_unlock_a_commitment_by_default(
    new_kw, frozen_kw, prior_kw
):
    below_floor = new_kw < min(frozen_kw, prior_kw) - 1e-9

    for reason in (reasons.R_AS_RELEASE, "R-BETTER-PRICE", reasons.R_SUBSTITUTION):
        assert check_commitment_lock(new_kw, frozen_kw, prior_kw, reason).ok != below_floor


@given(st.lists(_frozen_kw, min_size=1, max_size=4), st.data())
def test_k13_guardian_vetoes_a_batch_that_omits_any_committed_obligation(frozen, data):
    ids = [uuid4() for _ in frozen]
    included = data.draw(st.sets(st.sampled_from(range(len(frozen))), max_size=len(frozen)))
    items = [ProposedItem(HUB_ID, 1.0, "SELECTOR")] + [
        ProposedItem(HUB_ID, 1.0, "SELECTOR", ids[i], frozen[i]) for i in sorted(included)
    ]
    world = passing_world(items)
    # The hub executes the SUM of its items: that is its unchanged prior setpoint (no ramp).
    world.hub = make_hub(prev_p_kw=sum(item.p_kw_setpoint for item in items))
    world.active = [ActiveObligation(oid, kw) for oid, kw in zip(ids, frozen, strict=True)]

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == (len(included) == len(frozen))
    if verdict.outcome != "PASS":
        assert "G-19" in verdict.vetoed_rule_ids


@given(
    _frozen_kw,
    st.sampled_from(_REDUCTION_REASONS),
    st.decimals(min_value="0", max_value="0.9", places=1),
    st.booleans(),
    st.booleans(),
)
def test_k13_guardian_signs_a_reduced_grant_only_under_an_override_its_own_reads_corroborate(
    frozen, reason, fraction, l2_active, bank_short
):
    """An override reason is the engine's claim. The guardian signs the reduction only when its own
    reads agree: an active L2 instruction for `-L2`; for `-L0`/`-L1`/`INFEASIBLE`, its own telemetry of
    the bank's hubs showing less deliverable capability than the commitment."""
    oid = uuid4()
    items = [ProposedItem(HUB_ID, 1.0, reason or "SELECTOR", oid, frozen * fraction)]
    world = passing_world(items)
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, frozen)]
    world.l2 = L2Instruction("LIMIT", 100.0) if l2_active else None
    # Members at their reserve floor deliver 0 kW; two full hubs deliver 22 kW > any frozen amount here.
    world.members = [make_hub(soc_kwh=BASE_HUB.r_kwh)] if bank_short else [make_hub(soc_kwh=39.0)] * 2

    verdict = evaluate(world, SIGNER)

    corroborated = (reason == reasons.R_COMMIT_LOCK_OVERRIDE_L2 and l2_active) or (
        reason in CAPABILITY_OVERRIDE_REASONS and bank_short
    )
    assert (verdict.outcome == "PASS") == corroborated


@given(_frozen_kw, st.sampled_from(OVERRIDES), st.decimals(min_value="0", max_value="0.9", places=1))
def test_k13_a_forged_override_is_never_signed(frozen, reason, fraction):
    """Nothing in the guardian's own reads backs the claim: no L2 instruction, and the bank's hubs could
    carry the commitment. Whatever override code the batch carries, the reduction is vetoed."""
    oid = uuid4()
    world = passing_world([ProposedItem(HUB_ID, 1.0, reason, oid, frozen * fraction)])
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, frozen)]
    world.members = [make_hub(soc_kwh=39.0)] * 2

    verdict = evaluate(world, SIGNER)

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids


@given(
    _frozen_kw,
    st.sampled_from(sorted(CAPABILITY_OVERRIDE_REASONS)),
    st.decimals(min_value="0", max_value="0.9", places=1),
)
def test_k13_stale_telemetry_never_corroborates_a_shortfall(frozen, reason, fraction):
    """The guardian cannot see any hub on the bank: however empty their last reports, a shortfall claim
    is not corroborated (unseen hubs count at full rating), so the reduction is vetoed."""
    oid = uuid4()
    world = passing_world([ProposedItem(HUB_ID, 1.0, reason, oid, frozen * fraction)])
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, frozen)]
    world.members = [replace(make_hub(soc_kwh=BASE_HUB.r_kwh), health="stale")] * 2

    verdict = evaluate(world, SIGNER)

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids


@given(_frozen_kw, st.decimals(min_value="0", max_value="0.9", places=1))
def test_k13_a_closed_loop_reduction_is_never_signed_without_the_guardians_own_profile_read(frozen, fraction):
    """Need basis (owner decision 2026-09-26): `R-GRANT-CLOSED-LOOP` below the reserved maximum is a claim;
    with no profile read by the guardian it is never signed."""
    oid = uuid4()
    world = passing_world([ProposedItem(HUB_ID, 1.0, reasons.R_GRANT_CLOSED_LOOP, oid, frozen * fraction)])
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, frozen)]

    verdict = evaluate(world, SIGNER)

    assert verdict.signature is None and "G-19" in verdict.vetoed_rule_ids
