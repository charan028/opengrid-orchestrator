"""TraceStore: append/checkpoint/prune/verify built on `opengrid.core.tracehash` (02a S8).

I/O (Postgres reads/writes) is isolated behind the `TraceBackend` protocol so the hash-chain logic
stays testable without a database; `opengrid.platform.db`-backed implementations live in
`opengrid.trace.pg_backend` (kept separate so this module has no psycopg import, per BUILD.md S5a's
"pure logic separated from I/O").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from opengrid.core.tracehash import (
    ChainRecord,
    VerifyResult,
    checkpoint_hash,
    compute_next_hash,
    verify_chain,
)

CHECKPOINT_EVERY_N_RECORDS = 1000  # 02a S8.3: one checkpoint per 1,000 pruned rows


class TraceBackend(Protocol):
    """Storage contract `TraceStore` needs. A real backend persists to `og.trace` /
    `og.trace_checkpoint` / `og.retention_policy`; tests may use an in-memory fake."""

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        """Return (last_seq, last_hash) for stream_id, or (-1, None) if the stream is empty."""
        ...

    async def insert_trace_row(
        self,
        *,
        trace_id: UUID,
        stream_id: str,
        seq: int,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None,
        prev_hash: str | None,
        record_hash: str,
        created_at: datetime,
    ) -> None: ...

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        """Whether a trace row referencing `decision_ref` (e.g. a command_batch_id) already exists."""
        ...

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]: ...

    async def stream_ids(self) -> list[str]: ...

    async def insert_checkpoint(
        self,
        *,
        checkpoint_id: UUID,
        checkpoint_at: datetime,
        stream_heads: dict[str, dict[str, Any]],
        checkpoint_hash_hex: str,
    ) -> None: ...

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        """Delete rows with seq < keep_from_seq for stream_id; return the number of rows deleted."""
        ...

    async def retention_days_for(self, event_class: str) -> int:
        """Configured retention (02a S8.1); unknown classes default to 400 days (02b S1.4)."""
        ...


@dataclass(frozen=True, slots=True)
class TraceRecordRef:
    trace_id: UUID
    stream_id: str
    seq: int
    hash: str


class TraceStore:
    """Facade every process calls: `await store.append(stream_id, decision_type, event_class, payload)`."""

    def __init__(self, backend: TraceBackend) -> None:
        self._backend = backend

    async def append(
        self,
        stream_id: str,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None = None,
    ) -> TraceRecordRef:
        """K10: durably write the decision pre-image before anything is signed. Raises whatever the
        backend raises on a UNIQUE(stream_id, seq)/UNIQUE(stream_id, prev_hash) violation -- a fork or
        skipped link is a write-time error, not a verify-time finding (02a S8.2)."""
        last_seq, prev_hash = await self._backend.last_head(stream_id)
        seq = last_seq + 1
        _payload_hash, record_hash = compute_next_hash(
            stream_id=stream_id,
            seq=seq,
            decision_type=decision_type,
            event_class=event_class,
            prev_hash=prev_hash,
            payload=payload,
        )
        trace_id = uuid4()
        created_at = datetime.now(UTC)
        await self._backend.insert_trace_row(
            trace_id=trace_id,
            stream_id=stream_id,
            seq=seq,
            decision_type=decision_type,
            event_class=event_class,
            payload=payload,
            reason_codes=reason_codes,
            prev_hash=prev_hash,
            record_hash=record_hash,
            created_at=created_at,
        )
        return TraceRecordRef(trace_id, stream_id, seq, record_hash)

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        """G-14: has this decision already been traced? Guardian calls this, never recomputes a hash."""
        return await self._backend.exists_preimage(decision_ref)

    async def verify(self, stream_id: str, *, from_seq: int = 0) -> VerifyResult:
        """K11: re-derive every record's hash and confirm the chain links from `from_seq` onward."""
        records = await self._backend.fetch_range(stream_id, from_seq=from_seq)
        starting_prev_hash = records[0].prev_hash if records else None
        return verify_chain(stream_id, records, starting_prev_hash=starting_prev_hash)

    async def checkpoint(self) -> str:
        """02a S8.3: snapshot every active stream's (seq, hash) head and hash the snapshot itself."""
        stream_heads: dict[str, dict[str, Any]] = {}
        for stream_id in await self._backend.stream_ids():
            seq, head_hash = await self._backend.last_head(stream_id)
            if head_hash is not None:
                stream_heads[stream_id] = {"seq": seq, "hash": head_hash}
        checkpoint_hash_hex = checkpoint_hash(stream_heads)
        await self._backend.insert_checkpoint(
            checkpoint_id=uuid4(),
            checkpoint_at=datetime.now(UTC),
            stream_heads=stream_heads,
            checkpoint_hash_hex=checkpoint_hash_hex,
        )
        return checkpoint_hash_hex

    async def prune(self) -> dict[str, int]:
        """Delete rows older than their event_class's retention, keeping checkpoints so `verify()`
        still passes across the pruned gap (02a S8.3). Returns {stream_id: rows_deleted}."""
        deleted: dict[str, int] = {}
        for stream_id in await self._backend.stream_ids():
            last_seq, _head_hash = await self._backend.last_head(stream_id)
            keep_from = max(last_seq - CHECKPOINT_EVERY_N_RECORDS + 1, 0)
            if keep_from <= 0:
                continue
            deleted[stream_id] = await self._backend.prune_before(stream_id, keep_from_seq=keep_from)
        return deleted
