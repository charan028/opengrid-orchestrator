from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from opengrid.core.tracehash import ChainRecord
from opengrid.trace.store import TraceStore


@dataclass
class _Row:
    trace_id: UUID
    seq: int
    decision_type: str
    event_class: str
    payload: dict[str, Any]
    prev_hash: str | None
    hash: str
    created_at: datetime


@dataclass
class InMemoryBackend:
    """A minimal fake satisfying the TraceBackend protocol, for unit testing TraceStore without a DB."""

    streams: dict[str, list[_Row]] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    preimages: set[UUID] = field(default_factory=set)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        rows = self.streams.get(stream_id, [])
        if not rows:
            return -1, None
        return rows[-1].seq, rows[-1].hash

    async def insert_trace_row(
        self,
        *,
        trace_id,
        stream_id,
        seq,
        decision_type,
        event_class,
        payload,
        reason_codes,
        prev_hash,
        record_hash,
        created_at,
    ) -> None:
        rows = self.streams.setdefault(stream_id, [])
        if rows and rows[-1].seq >= seq:
            raise ValueError("UNIQUE(stream_id, seq) violation")
        rows.append(
            _Row(trace_id, seq, decision_type, event_class, payload, prev_hash, record_hash, created_at)
        )
        self.preimages.add(trace_id)

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return decision_ref in self.preimages

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        rows = self.streams.get(stream_id, [])
        return [
            ChainRecord(r.seq, r.decision_type, r.event_class, r.payload, r.prev_hash, r.hash)
            for r in rows
            if r.seq >= from_seq
        ]

    async def stream_ids(self) -> list[str]:
        return list(self.streams.keys())

    async def insert_checkpoint(
        self, *, checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash_hex
    ) -> None:
        self.checkpoints.append({"stream_heads": stream_heads, "hash": checkpoint_hash_hex})

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        rows = self.streams.get(stream_id, [])
        kept = [r for r in rows if r.seq >= keep_from_seq]
        deleted = len(rows) - len(kept)
        self.streams[stream_id] = kept
        return deleted

    async def retention_days_for(self, event_class: str) -> int:
        return 400


@pytest.fixture
def store() -> TraceStore:
    return TraceStore(InMemoryBackend())


async def test_append_first_record_has_no_prev_hash(store: TraceStore):
    ref = await store.append("selector", "ADMISSION", "ADMISSION", {"a": 1})
    assert ref.seq == 0


async def test_append_chains_sequential_records(store: TraceStore):
    ref1 = await store.append("selector", "ADMISSION", "ADMISSION", {"a": 1})
    ref2 = await store.append("selector", "ADMISSION", "ADMISSION", {"a": 2})
    assert ref2.seq == ref1.seq + 1
    assert ref1.hash != ref2.hash


async def test_verify_passes_on_intact_chain(store: TraceStore):
    for i in range(5):
        await store.append("selector", "ADMISSION", "ADMISSION", {"i": i})
    result = await store.verify("selector")
    assert result.ok


async def test_exists_preimage_true_after_append(store: TraceStore):
    ref = await store.append("guardian", "GUARDIAN_VERDICT", "GUARDIAN_VERDICT", {"outcome": "PASS"})
    assert await store.exists_preimage(ref.trace_id)


async def test_exists_preimage_false_for_unknown_id(store: TraceStore):
    from uuid import uuid4

    assert not await store.exists_preimage(uuid4())


async def test_checkpoint_skips_streams_with_no_head(store: TraceStore):
    backend = store._backend
    backend.streams["empty-stream"] = []  # registered but no records yet -> last_head is (-1, None)
    checkpoint_hash_hex = await store.checkpoint()
    assert isinstance(checkpoint_hash_hex, str)
    assert "empty-stream" not in store._backend.checkpoints[-1]["stream_heads"]


async def test_prune_skips_short_streams(store: TraceStore):
    await store.append("short-stream", "ADMISSION", "ADMISSION", {"a": 1})
    deleted = await store.prune()
    assert "short-stream" not in deleted


async def test_checkpoint_then_prune_then_verify_still_passes(store: TraceStore):
    for i in range(2500):
        await store.append("allocator-bank-07", "RT_ALLOCATION", "RT_ALLOCATION", {"i": i})
    await store.checkpoint()
    deleted = await store.prune()
    assert deleted.get("allocator-bank-07", 0) > 0

    # verify() from the earliest remaining seq must still pass (checkpointed pruning, K11)
    remaining = await store._backend.fetch_range("allocator-bank-07", from_seq=0)
    from_seq = remaining[0].seq
    result = await store.verify("allocator-bank-07", from_seq=from_seq)
    assert result.ok
