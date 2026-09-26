"""K7 degrade, don't trip (00-invariants.md K7): TIMEOUT is a hold, distinct from a VETO and never a STOP."""

from __future__ import annotations

import asyncio
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from opengrid.guardian.config import GuardianConfig
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
