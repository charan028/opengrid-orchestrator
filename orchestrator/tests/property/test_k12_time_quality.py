"""K12 time quality (00-invariants.md K12; G-20): the guardian will not sign on a clock it cannot trust."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.timeutil import clock_offset_ok
from opengrid.guardian.config import GuardianConfig

from .support import Signer, evaluate, passing_world

SIGNER = Signer.new()
_offset_ms = st.floats(min_value=-5_000.0, max_value=5_000.0, allow_nan=False)
_limit_ms = st.floats(min_value=1.0, max_value=1_000.0)


@given(_offset_ms, _limit_ms)
def test_k12_offset_acceptance_is_symmetric_and_bounded_by_the_limit(offset_ms, limit_ms):
    assert clock_offset_ok(offset_ms, limit_ms) == clock_offset_ok(-offset_ms, limit_ms)
    assert clock_offset_ok(offset_ms, limit_ms) == (abs(offset_ms) <= limit_ms)


@given(_offset_ms, _limit_ms, _limit_ms)
def test_k12_a_looser_limit_never_rejects_what_a_tighter_limit_accepted(offset_ms, limit_ms, extra_ms):
    assert not clock_offset_ok(offset_ms, limit_ms) or clock_offset_ok(offset_ms, limit_ms + extra_ms)


@given(_offset_ms, _limit_ms)
def test_k12_the_guardian_signs_only_while_its_own_clock_is_within_the_limit(offset_ms, limit_ms):
    world = passing_world()
    world.offset_ms = offset_ms

    verdict = evaluate(world, SIGNER, config=GuardianConfig(key_path="", clock_offset_max_ms=limit_ms))

    assert (verdict.signature is not None) == (abs(offset_ms) <= limit_ms)
