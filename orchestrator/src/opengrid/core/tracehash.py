"""JCS canonicalization + SHA-256 hash chain for the trace, plus checkpoint verification.

Single owner per 02a S8.2 / 02b S12. `opengrid.trace` is the only module that WRITES trace rows
(via append()/verify() built on these functions); everything else that needs to know "did this get
traced" asks `opengrid.trace`, never recomputes a hash itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opengrid.core.crypto import canonicalize_json, sha256_hex


@dataclass(frozen=True, slots=True)
class TraceRecordHeader:
    stream_id: str
    seq: int
    decision_type: str
    event_class: str
    prev_hash: str | None
    payload_hash: str


def payload_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(canonicalize_json(payload))


def record_hash(header: TraceRecordHeader) -> str:
    """sha256_hex(JCS(header)) -- the record's own hash, chaining to prev_hash (02a S8.2)."""
    header_dict = {
        "stream_id": header.stream_id,
        "seq": header.seq,
        "decision_type": header.decision_type,
        "event_class": header.event_class,
        "prev_hash": header.prev_hash,
        "payload_hash": header.payload_hash,
    }
    return sha256_hex(canonicalize_json(header_dict))


def compute_next_hash(
    *,
    stream_id: str,
    seq: int,
    decision_type: str,
    event_class: str,
    prev_hash: str | None,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Convenience wrapper: returns (payload_hash, record_hash) for a new record to be appended."""
    ph = payload_hash(payload)
    header = TraceRecordHeader(stream_id, seq, decision_type, event_class, prev_hash, ph)
    return ph, record_hash(header)


@dataclass(frozen=True, slots=True)
class ChainRecord:
    """The minimal fields verify_chain needs from a persisted trace row."""

    seq: int
    decision_type: str
    event_class: str
    payload: dict[str, Any]
    prev_hash: str | None
    hash: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    broken_at_seq: int | None = None
    reason: str | None = None


def verify_chain(
    stream_id: str,
    records: list[ChainRecord],
    *,
    starting_prev_hash: str | None = None,
) -> VerifyResult:
    """Re-derive each record's hash from its fields and confirm the chain links and seq are gapless.
    `starting_prev_hash` lets verification resume after a checkpoint that pruned earlier rows
    (02a S8.3's "checkpointed pruning") -- pass the checkpoint's stored head hash for this stream.
    """
    expected_prev = starting_prev_hash
    for rec in records:
        if rec.prev_hash != expected_prev:
            return VerifyResult(False, rec.seq, "PREV_HASH_MISMATCH")
        ph = payload_hash(rec.payload)
        header = TraceRecordHeader(stream_id, rec.seq, rec.decision_type, rec.event_class, rec.prev_hash, ph)
        expected_hash = record_hash(header)
        if expected_hash != rec.hash:
            return VerifyResult(False, rec.seq, "HASH_MISMATCH")
        expected_prev = rec.hash
    return VerifyResult(True)


def checkpoint_hash(stream_heads: dict[str, dict[str, Any]]) -> str:
    """sha256 over stream_heads (JCS, sorted keys) -- 02a S1.14 trace_checkpoint.checkpoint_hash."""
    return sha256_hex(canonicalize_json(stream_heads))
