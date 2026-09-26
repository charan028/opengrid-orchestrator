"""K12 time quality (00-invariants.md K12; G-20): the guardian will not sign on a clock it cannot trust."""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core.timeutil import clock_offset_ok
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.repo import parse_chrony_tracking_offset_ms

from .support import Signer, evaluate, passing_world

SIGNER = Signer.new()
_offset_ms = st.floats(min_value=-5_000.0, max_value=5_000.0, allow_nan=False)
_limit_ms = st.floats(min_value=1.0, max_value=1_000.0)


@settings(max_examples=500)
@given(_offset_ms, _limit_ms)
def test_k12_offset_acceptance_is_symmetric_and_bounded_by_the_limit(offset_ms, limit_ms):
    assert clock_offset_ok(offset_ms, limit_ms) == clock_offset_ok(-offset_ms, limit_ms)
    assert clock_offset_ok(offset_ms, limit_ms) == (abs(offset_ms) <= limit_ms)


@settings(max_examples=500)
@given(_offset_ms, _limit_ms, _limit_ms)
def test_k12_a_looser_limit_never_rejects_what_a_tighter_limit_accepted(offset_ms, limit_ms, extra_ms):
    assert not clock_offset_ok(offset_ms, limit_ms) or clock_offset_ok(offset_ms, limit_ms + extra_ms)


@settings(max_examples=500)
@given(_offset_ms, _limit_ms)
def test_k12_the_guardian_signs_only_while_its_own_clock_is_within_the_limit(offset_ms, limit_ms):
    world = passing_world()
    world.offset_ms = offset_ms

    verdict = evaluate(world, SIGNER, config=GuardianConfig(key_path="", clock_offset_max_ms=limit_ms))

    assert (verdict.signature is not None) == (abs(offset_ms) <= limit_ms)


@settings(max_examples=500)
@given(st.sampled_from([math.inf, -math.inf, math.nan]), _limit_ms)
def test_k12_an_unknown_clock_quality_is_never_signed_on(offset_ms, limit_ms):
    """Every clock adapter reports a failed/unsynchronised read as a non-finite offset; the guardian
    must hold (TIMEOUT), never sign, on it."""
    world = passing_world()
    world.offset_ms = offset_ms

    verdict = evaluate(world, SIGNER, config=GuardianConfig(key_path="", clock_offset_max_ms=limit_ms))

    assert verdict.signature is None and verdict.outcome == "TIMEOUT"


_chrony_lines = st.lists(
    st.sampled_from(
        [
            "Leap status     : Normal",
            "Leap status     : Not synchronised",
            "System time     : 0.000123 seconds fast of NTP time",
            "System time     : 0.450000 seconds slow of NTP time",
            "System time     : garbage",
            "506 Cannot talk to daemon",
            "",
        ]
    ),
    max_size=6,
)


@settings(max_examples=500)
@given(_chrony_lines)
def test_k12_chrony_output_yields_a_finite_offset_only_when_synchronised(lines):
    offset = parse_chrony_tracking_offset_ms("\n".join(lines))
    leap = [line for line in lines if line.startswith("Leap status")]
    if math.isfinite(offset):
        assert leap and leap[-1].endswith("Normal")
        assert any(line.startswith("System time") and "seconds" in line for line in lines)
