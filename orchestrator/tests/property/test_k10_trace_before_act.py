"""K10 trace before act (00-invariants.md K10): the guardian never signs a batch whose pre-image is untraced."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.trace.store import TraceStore

from .support import InMemoryTraceBackend, Signer, evaluate, passing_world, run

SIGNER = Signer.new()


@given(st.lists(st.booleans(), max_size=25))
def test_k10_signed_commands_never_outnumber_traced_decisions(traced_flags):
    store = TraceStore(InMemoryTraceBackend())
    signed = traced = 0
    for index, is_traced in enumerate(traced_flags):
        if is_traced:
            ref = run(store.append("engine", "RT_ALLOCATION", "RT_ALLOCATION", {"batch": index}))
            pre_image_id, traced = ref.trace_id, traced + 1
        else:
            pre_image_id = uuid4()
        world = passing_world()
        world.pre_image_id = pre_image_id
        world.trace_lookup = store.exists_preimage

        verdict = evaluate(world, SIGNER)

        assert (verdict.outcome == "PASS") == is_traced
        signed += verdict.signature is not None

    assert signed == traced == sum(traced_flags)


@given(st.integers(min_value=0, max_value=5))
def test_k10_a_missing_pre_image_vetoes_on_g14_and_signs_nothing(_variation):
    world = passing_world()
    world.preimage_exists = False

    verdict = evaluate(world, SIGNER)

    assert verdict.outcome == "VETOED"
    assert verdict.vetoed_rule_ids == ["G-14"]
    assert verdict.signature is None


def test_k10_a_batch_that_records_no_pre_image_id_is_never_signed():
    world = passing_world()
    world.pre_image_id = None

    verdict = evaluate(world, SIGNER)

    assert verdict.outcome == "VETOED"
    assert verdict.vetoed_rule_ids == ["G-14"]
    assert verdict.signature is None
