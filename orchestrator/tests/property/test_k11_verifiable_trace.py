"""K11 verifiable track record (00-invariants.md K11): the hash chain verifies after any prune, and only then."""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st

from opengrid.trace import store as trace_store
from opengrid.trace.store import TraceStore

from .support import InMemoryTraceBackend, run

STREAMS = ["engine", "guardian", "settle"]
_appends = st.lists(st.sampled_from(STREAMS), min_size=1, max_size=40)


def _filled_store(stream_of_each_append: list[str]) -> tuple[TraceStore, InMemoryTraceBackend]:
    backend = InMemoryTraceBackend()
    store = TraceStore(backend)

    async def fill() -> None:
        for index, stream_id in enumerate(stream_of_each_append):
            await store.append(stream_id, "DECISION", "RT_ALLOCATION", {"i": index})

    run(fill())
    return store, backend


@given(_appends)
def test_k11_every_stream_verifies_after_arbitrary_interleaved_appends(stream_of_each_append):
    store, backend = _filled_store(stream_of_each_append)

    assert all(run(store.verify(stream_id)).ok for stream_id in backend.streams)


@given(_appends, st.integers(min_value=0, max_value=40))
def test_k11_a_stream_still_verifies_after_pruning_any_prefix(stream_of_each_append, keep_from_seq):
    store, backend = _filled_store(stream_of_each_append)

    for stream_id in list(backend.streams):
        run(backend.prune_before(stream_id, keep_from_seq=keep_from_seq))
        assert run(store.verify(stream_id)).ok


@given(st.integers(min_value=2, max_value=40), st.integers(min_value=1, max_value=40))
def test_k11_the_stores_own_retention_prune_keeps_the_chain_verifiable(record_count, retained):
    store, backend = _filled_store(["engine"] * record_count)

    with patch.object(trace_store, "CHECKPOINT_EVERY_N_RECORDS", retained):
        run(store.prune())

    assert run(store.verify("engine")).ok
    assert len(backend.streams["engine"]) == min(record_count, retained)


@given(st.integers(min_value=1, max_value=30), st.data())
def test_k11_tampering_with_any_surviving_record_is_detected_at_that_record(record_count, data):
    store, backend = _filled_store(["engine"] * record_count)
    victim = data.draw(st.integers(min_value=0, max_value=record_count - 1))
    backend.streams["engine"][victim].payload = {"tampered": True}

    result = run(store.verify("engine"))

    assert not result.ok
    assert result.broken_at_seq == victim
