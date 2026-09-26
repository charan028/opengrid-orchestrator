"""K13 commitment lock (00-invariants.md K13; G-19): a commitment shrinks only for L0/L1/L2/infeasible."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core import reasons
from opengrid.core.limits import check_commitment_lock
from opengrid.guardian.ports import ActiveObligation, ProposedItem

from .support import HUB_ID, Signer, evaluate, make_hub, passing_world

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
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, kw) for oid, kw in zip(ids, frozen, strict=True)]

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == (len(included) == len(frozen))
    if verdict.outcome != "PASS":
        assert "G-19" in verdict.vetoed_rule_ids


@given(_frozen_kw, st.sampled_from(_REDUCTION_REASONS), st.decimals(min_value="0", max_value="0.9", places=1))
def test_k13_guardian_signs_a_reduced_grant_only_under_an_override_reason(frozen, reason, fraction):
    oid = uuid4()
    items = [ProposedItem(HUB_ID, 1.0, reason or "SELECTOR", oid, frozen * fraction)]
    world = passing_world(items)
    world.hub = make_hub(prev_p_kw=1.0)
    world.active = [ActiveObligation(oid, frozen)]

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == (reason in OVERRIDES)
