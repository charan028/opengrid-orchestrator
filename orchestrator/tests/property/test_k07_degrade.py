"""K7 degrade, don't trip (00-invariants.md K7): TIMEOUT is a hold, distinct from a VETO and never a STOP."""

from __future__ import annotations

import asyncio
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.escalation import BatchOutcome, EscalationTracker
from opengrid.guardian.ports import ProposedBatch

from .support import Signer, World, evaluate, make_hub, make_proposal, passing_world
from .test_k03_sole_signer import FAULTS

SIGNER = Signer.new()
DEFAULT_CLOCK_LIMIT_MS = GuardianConfig(key_path="").clock_offset_max_ms


class _TraceDownWorld(World):
    async def append_verdict(self, batch_id: UUID, payload: dict[str, object]) -> None:
        raise RuntimeError("trace backend unavailable")


class _SlowProposalWorld(World):
    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        await asyncio.sleep(0.2)
        return self.proposal


@settings(max_examples=100)
@given(st.floats(min_value=-5_000.0, max_value=5_000.0, allow_nan=False))
def test_k07_a_bad_guardian_clock_is_a_timeout_hold_not_a_veto(offset_ms):
    world = passing_world()
    world.offset_ms = offset_ms

    verdict = evaluate(world, SIGNER)

    if abs(offset_ms) > DEFAULT_CLOCK_LIMIT_MS:
        assert verdict.outcome == "TIMEOUT"
        assert verdict.vetoed_rule_ids == ["G-20"]
        assert verdict.signature is None
    else:
        assert verdict.outcome == "PASS"


@settings(max_examples=100)
@given(st.sets(st.sampled_from(sorted(FAULTS))))
def test_k07_losing_the_verdict_trace_never_changes_the_signing_decision(faults):
    healthy, degraded = passing_world(), passing_world()
    degraded_world = _TraceDownWorld(proposal=degraded.proposal, hub=make_hub())
    for fault in faults:
        FAULTS[fault](healthy)
        FAULTS[fault](degraded_world)

    assert evaluate(degraded_world, SIGNER).outcome == evaluate(healthy, SIGNER).outcome


def test_k07_a_slow_dependency_times_out_into_a_hold_with_no_signature():
    world = _SlowProposalWorld(proposal=make_proposal(), hub=make_hub())
    config = GuardianConfig(key_path="", verdict_timeout_ms=10.0)

    verdict = evaluate(world, SIGNER, config=config)

    assert verdict.outcome == "TIMEOUT"
    assert verdict.signature is None


_tick_ratios = st.lists(
    st.tuples(st.integers(min_value=0, max_value=40), st.integers(min_value=0, max_value=40)),
    min_size=1,
    max_size=40,
)


@settings(max_examples=300)
@given(_tick_ratios)
def test_k07_vetoes_degrade_to_conservative_and_only_ever_request_a_stop(ticks):
    """ES06-S04 / TS-06-04: for any sequence of ticks, a scope is CONSERVATIVE exactly after a tick with more
    than 5 % vetoed (until a tick at or under 5 % clears it); a safe stop is requested only on the 3rd
    consecutive CONSERVATIVE tick, at most once per episode; and the counter itself can only request."""
    tracker = EscalationTracker(idle_clear_ticks=10_000)
    scope = ("BANK", "bank-000")
    streak = 0
    requested_this_episode = False
    for vetoed, extra in ticks:
        commands = vetoed + extra
        transitions = tracker.observe_tick(
            [BatchOutcome("bank-000", commands, vetoed)] if commands else [], zone_by_bank={}
        )
        kinds = [t.kind for t in transitions]
        if commands == 0:
            assert kinds == []
            continue
        if vetoed / commands > 0.05:
            streak += 1
            assert tracker.posture(scope) == "CONSERVATIVE"
            expect_request = streak >= 3 and not requested_this_episode
            assert ("REQUEST_SAFE_STOP" in kinds) == expect_request
            requested_this_episode = requested_this_episode or expect_request
        else:
            assert tracker.posture(scope) == "NORMAL"
            assert kinds == (["CLEAR"] if streak else [])
            streak, requested_this_episode = 0, False
        assert set(kinds) <= {"ENTER_CONSERVATIVE", "STAY_CONSERVATIVE", "REQUEST_SAFE_STOP", "CLEAR"}
