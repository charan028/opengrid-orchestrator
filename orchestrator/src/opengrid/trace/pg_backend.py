"""Postgres `TraceBackend` (02a S8) -- the only module that issues SQL against `og.trace` /
`og.trace_checkpoint` / `og.retention_policy`. Implements the `opengrid.trace.store.TraceBackend`
protocol as-is (no signature changes -- BUILD.md S4/S5a); `opengrid.trace.store.TraceStore` owns all
hash-chain logic and never appears here.

Concurrency (K11 "no fork, no skipped link"): `TraceStore.append()` calls `last_head()` then
`insert_trace_row()` as two independent awaits, so this backend cannot hold one transaction across
both without changing the protocol. Safety therefore comes from the database itself:
`ux_trace_stream_seq` / `ux_trace_stream_prev` (02a S1.14) make a losing concurrent writer fail with a
`UniqueViolation`, which `insert_trace_row` translates to `TraceAppendConflictError` -- a write-time error,
never a silently corrupted chain (matches `TraceStore.append`'s docstring exactly). Callers may retry
the whole `append()` call.

Retention pruning (K11, 02a S8.3): `prune_before` (called by `TraceStore.prune()`'s generic seq-window
backstop) and `run_retention_prune_job` (the per-event-class daily/on-demand job, BUILD.md S4 "health"
row) both refuse to delete any row that is not covered by at least one `trace_checkpoint` whose
`stream_heads` entry for that stream has `seq >= row.seq` -- so `verify()` from the nearest surviving
checkpoint forward always still passes (02a S8.3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.tracehash import ChainRecord

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 400  # 02b S1.4: unknown/unconfigured event classes default here.

#: K11 fail-safe (00-invariants.md, adversarial review): relative, per-workspace default -- resolves
#: inside whichever process's own working directory runs it, the same convention `[pq_ingest].
#: blob_store_dir` already uses, so no per-workspace config file is needed. Production overrides this
#: via `PgTraceBackend(pool, journal_path=...)` (main.py callers may pass `[trace].journal_path`).
DEFAULT_JOURNAL_PATH = Path("var/trace_journal.jsonl")


def journal_path_from_config(cfg: object) -> Path:
    """Resolve `[trace].journal_path` (production: an absolute, opengrid-writable path, e.g.
    `/var/lib/opengrid/trace_journal.jsonl` -- set once in `orchestrator.toml`, applies to every process
    that constructs a `PgTraceBackend`) -- falls back to `DEFAULT_JOURNAL_PATH` (a workspace-relative
    default, still correct for tests/dev workspaces that never set it) when unconfigured. Takes `cfg` as
    `object` (structurally: anything with a `.get(dotted_path, default)` method, i.e.
    `opengrid.platform.config.Config`) so this module never has to import `platform.config` itself
    (`trace` has no dependency on `platform` today; adding one just for this accessor was worse than a
    duck-typed parameter)."""
    value = cfg.get("trace.journal_path", None) if hasattr(cfg, "get") else None
    return Path(str(value)) if value else DEFAULT_JOURNAL_PATH


class TraceJournalUnavailableError(RuntimeError):
    """K11 "no fork, no skipped link": the database is unreachable for `last_head(stream_id)`, AND this
    stream has no journal entry and no in-process cached head to safely resume from. Guessing a starting
    seq here risks writing a second, forked chain for a stream this process has never actually seen --
    refusing is the safe failure (the caller's `append()` fails loudly; nothing is silently corrupted)."""


class TraceAppendConflictError(RuntimeError):
    """A concurrent append already claimed this stream's next seq or prev_hash (UNIQUE violation on
    `ux_trace_stream_seq` / `ux_trace_stream_prev`). K11: a fork or skipped link is refused at write
    time, never silently accepted. The caller may retry the whole `TraceStore.append()` call."""

    def __init__(self, stream_id: str, seq: int, cause: Exception) -> None:
        super().__init__(f"concurrent append conflict on stream {stream_id!r} at seq {seq}: {cause}")
        self.stream_id = stream_id
        self.seq = seq


@dataclass(frozen=True, slots=True)
class _JournalEntry:
    """One fully-formed, already-hashed trace row that couldn't reach Postgres. Every field mirrors
    `insert_trace_row`'s own parameters exactly (JSON-safe: `payload`/`reason_codes` already are;
    `created_at` is stored as its ISO string)."""

    trace_id: str
    stream_id: str
    seq: int
    decision_type: str
    event_class: str
    payload: dict[str, Any]
    reason_codes: list[str] | None
    prev_hash: str | None
    record_hash: str
    created_at: str


class JournalCorruptError(RuntimeError):
    """A local trace-journal line could not be parsed -- surfaced, never silently skipped or dropped."""


def _journal_entry_to_line(entry: _JournalEntry) -> str:
    return json.dumps(asdict(entry), separators=(",", ":"))


def _journal_line_to_entry(line: str) -> _JournalEntry:
    try:
        data = json.loads(line)
        return _JournalEntry(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JournalCorruptError(f"unparseable trace journal line: {line!r}") from exc


_LAST_HEAD_SQL = """
SELECT seq, hash FROM og.trace WHERE stream_id = %(stream_id)s ORDER BY seq DESC LIMIT 1
"""

_INSERT_TRACE_SQL = """
INSERT INTO og.trace (
    trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes,
    prev_hash, hash, created_at
) VALUES (
    %(trace_id)s, %(decision_type)s, %(event_class)s, %(stream_id)s, %(seq)s, %(payload)s,
    %(reason_codes)s, %(prev_hash)s, %(record_hash)s, %(created_at)s
)
"""

_EXISTS_PREIMAGE_SQL = "SELECT EXISTS(SELECT 1 FROM og.trace WHERE trace_id = %(trace_id)s)"

_FETCH_RANGE_SQL = """
SELECT seq, decision_type, event_class, payload, prev_hash, hash
FROM og.trace WHERE stream_id = %(stream_id)s AND seq >= %(from_seq)s
ORDER BY seq
"""

_STREAM_IDS_SQL = "SELECT DISTINCT stream_id FROM og.trace"

_INSERT_CHECKPOINT_SQL = """
INSERT INTO og.trace_checkpoint (checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash)
VALUES (%(checkpoint_id)s, %(checkpoint_at)s, %(stream_heads)s, %(checkpoint_hash)s)
"""

_RETENTION_DAYS_SQL = "SELECT retention_days FROM og.retention_policy WHERE event_class = %(event_class)s"

# Covered = the highest seq per stream that any checkpoint's stream_heads has ever recorded. A row is
# safe to delete only if it is at or below its stream's covered seq (02a S8.3).
_COVERED_MAX_SEQ_CTE = """
WITH covered AS (
    SELECT kv.key AS stream_id, MAX((kv.value ->> 'seq')::bigint) AS max_seq
    FROM og.trace_checkpoint, LATERAL jsonb_each(stream_heads) AS kv
    GROUP BY kv.key
)
"""

_PRUNE_BEFORE_SQL = (
    _COVERED_MAX_SEQ_CTE
    + """
DELETE FROM og.trace t
USING covered c
WHERE t.stream_id = %(stream_id)s
  AND c.stream_id = t.stream_id
  AND t.seq < %(keep_from_seq)s
  AND t.seq <= c.max_seq
"""
)

_PRUNE_BY_CLASS_SQL = (
    _COVERED_MAX_SEQ_CTE
    + """
DELETE FROM og.trace t
USING covered c
WHERE t.event_class = %(event_class)s
  AND t.created_at < %(cutoff)s
  AND c.stream_id = t.stream_id
  AND t.seq <= c.max_seq
"""
)

_RETENTION_POLICIES_SQL = """
SELECT event_class, retention_days FROM og.retention_policy WHERE prune_after_checkpoint
"""


def _row_to_chain_record(row: tuple[int, str, str, dict[str, Any], str | None, str]) -> ChainRecord:
    seq, decision_type, event_class, payload, prev_hash, record_hash = row
    return ChainRecord(seq, decision_type, event_class, payload, prev_hash, record_hash)


class PgTraceBackend:
    """`TraceBackend` implementation backed by `opengrid.platform.db`'s async connection pool.

    K11 fail-safe (adversarial review): when the database is unreachable, `last_head`/`insert_trace_row`
    fall back to a local append-only journal (`journal_path`) instead of raising -- `TraceStore.append()`
    has already computed this record's hash before calling either method, so the chain stays internally
    consistent even while journaled; only its DURABLE HOME moves. Every successful DB call opportunistically
    drains any pending journal first (`_drain_journal_if_pending`), so recovery is automatic on the next
    call this process instance makes -- no separate replay process or cross-process journal sharing is
    needed. `replay()`/`pending_count()` are also exposed directly for a caller (e.g.
    `opengrid.invariants`) that wants to force/observe this."""

    def __init__(self, pool: AsyncConnectionPool, *, journal_path: Path | None = None) -> None:
        self._pool = pool
        # Best-effort in-process serialization for same-stream concurrent appends (reduces, but does
        # not replace, the DB unique-constraint safety net above -- see module docstring.
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._journal_path = journal_path if journal_path is not None else DEFAULT_JOURNAL_PATH
        # Bridges the gap between "last successful DB read/write" and "first journal entry" for a
        # stream that fails over mid-outage with nothing journaled for it yet (see `last_head`'s
        # fallback order: journal tail, then this cache, then refuse).
        self._last_known_head: dict[str, tuple[int, str | None]] = {}

    def _lock_for(self, stream_id: str) -> asyncio.Lock:
        lock = self._stream_locks.get(stream_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[stream_id] = lock
        return lock

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        await self._drain_journal_if_pending()
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_LAST_HEAD_SQL, {"stream_id": stream_id})
                row = await cur.fetchone()
        except Exception:
            logger.error(
                "trace DB unavailable for last_head; falling back to local journal",
                extra={"stream_id": stream_id},
                exc_info=True,
            )
            return self._fallback_last_head(stream_id)
        result = (-1, None) if row is None else (row[0], row[1])
        self._last_known_head[stream_id] = result
        return result

    def _fallback_last_head(self, stream_id: str) -> tuple[int, str | None]:
        journaled = self._journal_tail(stream_id)
        if journaled is not None:
            return journaled
        cached = self._last_known_head.get(stream_id)
        if cached is not None:
            return cached
        raise TraceJournalUnavailableError(
            f"stream {stream_id!r}: database unreachable and no journal/cached head to resume from"
        )

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
    ) -> None:
        await self._drain_journal_if_pending()
        async with self._lock_for(stream_id):
            try:
                async with self._pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        _INSERT_TRACE_SQL,
                        {
                            "trace_id": trace_id,
                            "decision_type": decision_type,
                            "event_class": event_class,
                            "stream_id": stream_id,
                            "seq": seq,
                            "payload": Jsonb(payload),
                            "reason_codes": reason_codes,
                            "prev_hash": prev_hash,
                            "record_hash": record_hash,
                            "created_at": created_at,
                        },
                    )
                    await conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                raise TraceAppendConflictError(stream_id, seq, exc) from exc
            except Exception:
                logger.error(
                    "trace DB unavailable; journaling trace row locally (K11 fail-safe)",
                    extra={"stream_id": stream_id, "seq": seq},
                    exc_info=True,
                )
                self._append_journal(
                    _JournalEntry(
                        trace_id=str(trace_id),
                        stream_id=stream_id,
                        seq=seq,
                        decision_type=decision_type,
                        event_class=event_class,
                        payload=payload,
                        reason_codes=reason_codes,
                        prev_hash=prev_hash,
                        record_hash=record_hash,
                        created_at=created_at.isoformat(),
                    )
                )
            self._last_known_head[stream_id] = (seq, record_hash)

    # --- K11 fail-safe journal: append-only local file, replayed in order on recovery -----------------

    def _append_journal(self, entry: _JournalEntry) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8") as fh:
            fh.write(_journal_entry_to_line(entry) + "\n")

    def _read_journal(self) -> list[_JournalEntry]:
        if not self._journal_path.exists():
            return []
        entries = []
        with self._journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    entries.append(_journal_line_to_entry(stripped))
        return entries

    def _journal_tail(self, stream_id: str) -> tuple[int, str | None] | None:
        last: _JournalEntry | None = None
        for entry in self._read_journal():
            if entry.stream_id == stream_id:
                last = entry
        return None if last is None else (last.seq, last.record_hash)

    def pending_count(self) -> int:
        """How many trace rows are currently journaled, not yet in Postgres -- a lag/health signal for
        `opengrid.invariants`."""
        return len(self._read_journal())

    async def _drain_journal_if_pending(self) -> None:
        """Opportunistic self-heal: if anything is journaled, try to flush it before this call's own DB
        round trip. A no-op (one cheap file-existence check) when the journal is empty, which is the
        overwhelming common case."""
        if self._journal_path.exists() and self._journal_path.stat().st_size > 0:
            await self.replay()

    async def replay(self) -> int:
        """Insert every journaled record into the real backend, in order (oldest first) -- K11 "replayed
        in order on recovery with the chain preserved". A record already applied (UNIQUE violation, e.g.
        a partially-successful prior replay) is treated as already-replayed, not an error. Stops at the
        first record that still fails for a reason OTHER than "already applied" (the DB is still down),
        leaving it and everything after it in the journal for the next attempt. Returns the count
        actually replayed (including ones found already-applied)."""
        entries = self._read_journal()
        if not entries:
            return 0
        remaining: list[_JournalEntry] = []
        replayed = 0
        stop = False
        for entry in entries:
            if stop:
                remaining.append(entry)
                continue
            try:
                async with self._pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        _INSERT_TRACE_SQL,
                        {
                            "trace_id": entry.trace_id,
                            "decision_type": entry.decision_type,
                            "event_class": entry.event_class,
                            "stream_id": entry.stream_id,
                            "seq": entry.seq,
                            "payload": Jsonb(entry.payload),
                            "reason_codes": entry.reason_codes,
                            "prev_hash": entry.prev_hash,
                            "record_hash": entry.record_hash,
                            "created_at": datetime.fromisoformat(entry.created_at),
                        },
                    )
                    await conn.commit()
                replayed += 1
            except psycopg.errors.UniqueViolation:
                replayed += 1  # already applied by an earlier partial replay
            except Exception:
                logger.warning("trace journal replay stopped: DB still unavailable", exc_info=True)
                remaining.append(entry)
                stop = True
        self._rewrite_journal(remaining)
        if replayed:
            logger.info(
                "trace journal replay progress", extra={"replayed": replayed, "remaining": len(remaining)}
            )
        return replayed

    def _rewrite_journal(self, entries: list[_JournalEntry]) -> None:
        tmp_path = self._journal_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(_journal_entry_to_line(entry) + "\n")
        tmp_path.replace(self._journal_path)

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_EXISTS_PREIMAGE_SQL, {"trace_id": decision_ref})
            row = await cur.fetchone()
        return bool(row[0]) if row else False

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FETCH_RANGE_SQL, {"stream_id": stream_id, "from_seq": from_seq})
            rows = await cur.fetchall()
        return [_row_to_chain_record(row) for row in rows]

    async def stream_ids(self) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STREAM_IDS_SQL)
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def insert_checkpoint(
        self,
        *,
        checkpoint_id: UUID,
        checkpoint_at: datetime,
        stream_heads: dict[str, dict[str, Any]],
        checkpoint_hash_hex: str,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_CHECKPOINT_SQL,
                {
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_at": checkpoint_at,
                    "stream_heads": Jsonb(stream_heads),
                    "checkpoint_hash": checkpoint_hash_hex,
                },
            )
            await conn.commit()

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRUNE_BEFORE_SQL, {"stream_id": stream_id, "keep_from_seq": keep_from_seq})
            deleted = cur.rowcount
            await conn.commit()
        return deleted

    async def retention_days_for(self, event_class: str) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RETENTION_DAYS_SQL, {"event_class": event_class})
            row = await cur.fetchone()
        return int(row[0]) if row else DEFAULT_RETENTION_DAYS


async def run_retention_prune_job(
    pool: AsyncConnectionPool, *, now: datetime | None = None
) -> dict[str, int]:
    """The retention pruning job (BUILD.md S4 "health" row): daily cron and on-demand entry point.

    For every `og.retention_policy` row with `prune_after_checkpoint`, deletes `og.trace` rows of that
    `event_class` older than `retention_days` -- but only rows already covered by a checkpoint (02a
    S8.3's `prune(event_class)` algorithm), so `TraceStore.verify()` still passes from the nearest
    surviving checkpoint forward (K11). Returns `{event_class: rows_deleted}`.
    """
    now = now or datetime.now(UTC)
    deleted_by_class: dict[str, int] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RETENTION_POLICIES_SQL)
            policies = await cur.fetchall()

        for event_class, retention_days in policies:
            cutoff = now - timedelta(days=retention_days)
            async with conn.cursor() as cur:
                await cur.execute(_PRUNE_BY_CLASS_SQL, {"event_class": event_class, "cutoff": cutoff})
                deleted_by_class[event_class] = cur.rowcount
        await conn.commit()

    logger.info("trace retention prune completed", extra={"deleted_by_class": deleted_by_class})
    return deleted_by_class
