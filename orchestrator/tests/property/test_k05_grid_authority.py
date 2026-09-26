"""K5 grid authority (00-invariants.md K5; G-15): an L2 instruction is a hard limit that value never relaxes."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.guardian.checks import check_g15_l2_boundary
from opengrid.guardian.ports import L2Instruction, ProposedItem

from .support import BANK_ID, HUB_ID, Signer, evaluate, make_hub, passing_world

SIGNER = Signer.new()
_EPSILON = 1e-9
_setpoint_kw = st.floats(min_value=-11.0, max_value=11.0, allow_nan=False)
_instructions = st.one_of(
    st.builds(L2Instruction, kind=st.sampled_from(["ESTOP", "BLOCK"]), limit_kw=st.none()),
    st.builds(L2Instruction, kind=st.just("LIMIT"), limit_kw=st.floats(min_value=0.0, max_value=11.0)),
)


def _allowed_kw(instruction: L2Instruction | None) -> float:
    if instruction is None:
        return float("inf")
    return 0.0 if instruction.kind in ("ESTOP", "BLOCK") else instruction.limit_kw or 0.0


@given(st.none() | _instructions, st.floats(min_value=0.0, max_value=500.0))
def test_k05_boundary_check_passes_exactly_when_the_aggregate_is_within_the_instruction(
    instruction, aggregate_kw
):
    outcome = check_g15_l2_boundary(instruction, BANK_ID, aggregate_kw)

    assert outcome.ok == (aggregate_kw <= _allowed_kw(instruction) + _EPSILON)


@given(st.none() | _instructions, _setpoint_kw)
def test_k05_guardian_never_signs_a_batch_that_exceeds_an_active_instruction(instruction, setpoint_kw):
    world = passing_world([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")])
    world.hub = make_hub(prev_p_kw=setpoint_kw)
    world.l2 = instruction

    verdict = evaluate(world, SIGNER)

    assert (verdict.outcome == "PASS") == (abs(setpoint_kw) <= _allowed_kw(instruction) + _EPSILON)
    if abs(setpoint_kw) > _allowed_kw(instruction) + _EPSILON:
        assert verdict.signature is None
