"""Postgres implementation of `StopEventBackend` (02a S1.12 `og.stop_event`), plus the LISTEN/NOTIFY
request intake `main.py` uses to receive stop requests from `og-api` without importing it.

Only `opengrid.platform.db` (a connection pool) is used here -- no engine/guardian/allocator import
(K8 import isolation, see `tests/unit/safestop/test_import_isolation.py`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.safestop.backend import OutboxEntry, RecordedL2Engage, StopEventRow

logger = logging.getLogger(__name__)

# Channel `og-api` (once built) NOTIFYs on with a JSON payload -- see README.md "Request intake".
REQUEST_CHANNEL = "og_safestop_request"

_INSERT_SQL = """
INSERT INTO og.stop_event
    (stop_event_id, scope_kind, scope_ref, action, initiator_kind, initiator_ref, reason,
     approver_ref, signature)
VALUES (%(stop_event_id)s, %(scope_kind)s, %(scope_ref)s, %(action)s, %(initiator_kind)s,
        %(initiator_ref)s, %(reason)s, %(approver_ref)s, %(signature)s)
"""

# Relayed guardian RELEASEs (og-safestop's own "safestop" trace stream) older than the retention window
# whose retained topic has not been cleared yet (no RETAINED_CLEARED housekeeping row for that stop_id).
_RELEASES_DUE_FOR_CLEARING_SQL = """
SELECT t.payload
FROM og.trace t
WHERE t.stream_id = 'safestop' AND t.decision_type = 'SAFE_STOP'
  AND t.payload ->> 'action' = 'RELEASE'
  AND t.created_at < now() - make_interval(secs => %(retain_s)s)
  AND NOT EXISTS (
      SELECT 1 FROM og.trace c
      WHERE c.stream_id = 'safestop' AND c.payload ->> 'housekeeping' = 'RETAINED_CLEARED'
        AND c.payload ->> 'stop_id' = t.payload ->> 'stop_id'
  )
ORDER BY t.seq
LIMIT %(limit)s
"""

_HAS_SIGNATURE_SQL = "SELECT 1 FROM og.stop_event WHERE signature = %(signature)s LIMIT 1"

_LATEST_ACTION_SQL = """
SELECT action FROM og.stop_event
WHERE scope_kind = %(scope_kind)s AND scope_ref = %(scope_ref)s
ORDER BY created_at DESC
LIMIT 1
"""

_HAS_L2_ENGAGE_SQL = """
SELECT 1 FROM og.stop_event
WHERE action = 'ENGAGE' AND scope_kind = 'BANK' AND scope_ref = %(bank_id)s
  AND initiator_kind = 'UTILITY' AND strpos(reason, %(instruction_id)s) > 0
LIMIT 1
"""


# K8 durable publish outbox (migration 0035): queued once per (stop_id, action). Drained ENGAGEs first (a stop is
# never delayed behind a release), each in acceptance order. `attempts` counts PERMANENT failures only (a
# payload that can never be published); at the cap an entry is dead-lettered: skipped by the drain, alerted.
_ENQUEUE_SQL = """
INSERT INTO og.stop_outbox (stop_id, action, topic_suffix, payload)
VALUES (%(stop_id)s, %(action)s, %(topic_suffix)s, %(payload)s)
ON CONFLICT ON CONSTRAINT stop_outbox_once DO NOTHING
"""
_PENDING_SQL = """
SELECT seq, stop_id, action, topic_suffix, payload FROM og.stop_outbox
WHERE published_at IS NULL AND attempts < %(max_attempts)s
ORDER BY (action = 'ENGAGE') DESC, seq
LIMIT %(limit)s
"""
_MARK_PUBLISHED_SQL = "UPDATE og.stop_outbox SET published_at = now() WHERE seq = %(seq)s"
_RECORD_FAILURE_SQL = """
UPDATE og.stop_outbox
SET attempts = attempts + CASE WHEN %(permanent)s THEN 1 ELSE 0 END, last_error = %(error)s
WHERE seq = %(seq)s
RETURNING attempts
"""
#: og-safestop may not import opengrid.health (K8 isolation), so it writes its one alert itself, in the same
#: shape as `health.queries.raise_alert` (structured scope columns, `condition_key` in `detail`), once while open.
DEAD_LETTER_ALERT_RULE = "ALR-STOP-PUBLISH-DEAD-LETTER"
_DEAD_LETTER_ALERT_SQL = """
INSERT INTO og.alert (rule, severity, summary, detail, opened_at, scope_kind, scope_ref)
SELECT %(rule)s, 'critical', %(summary)s, %(detail)s, now(), 'STOP', %(stop_id)s
WHERE NOT EXISTS (
    SELECT 1 FROM og.alert
    WHERE rule = %(rule)s AND cleared_at IS NULL AND detail ->> 'condition_key' = %(condition_key)s
)
"""
_L2_ENGAGE_RECORD_SQL = """
SELECT e.stop_event_id, e.reason, e.initiator_ref,
       EXISTS (SELECT 1 FROM og.stop_outbox o WHERE o.stop_id = e.stop_event_id AND o.action = 'ENGAGE'),
       EXISTS (
           SELECT 1 FROM og.stop_event r
           WHERE r.action = 'RELEASE' AND r.scope_kind = 'BANK' AND r.scope_ref = e.scope_ref
             AND r.created_at > e.created_at
       )
FROM og.stop_event e
WHERE e.action = 'ENGAGE' AND e.scope_kind = 'BANK' AND e.scope_ref = %(bank_id)s
  AND e.initiator_kind = 'UTILITY' AND strpos(e.reason, %(instruction_id)s) > 0
ORDER BY e.created_at DESC
LIMIT 1
"""


def _row_params(row: StopEventRow) -> dict[str, Any]:
    return {
        "stop_event_id": row.stop_event_id,
        "scope_kind": row.scope_kind,
        "scope_ref": row.scope_ref,
        "action": row.action,
        "initiator_kind": row.initiator_kind,
        "initiator_ref": row.initiator_ref,
        "reason": row.reason,
        "approver_ref": row.approver_ref,
        "signature": row.signature,
    }


@dataclass
class PgStopEventBackend:
    """Real `StopEventBackend` backed by the shared connection pool (`opengrid.platform.db.make_pool`)."""

    pool: AsyncConnectionPool

    async def insert_stop_event(
        self,
        *,
        stop_event_id: UUID,
        scope_kind: Literal["BANK", "ZONE", "FLEET"],
        scope_ref: str,
        action: Literal["ENGAGE", "RELEASE"],
        initiator_kind: str,
        initiator_ref: str,
        reason: str,
        approver_ref: str | None,
        signature: str,
    ) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_SQL,
                {
                    "stop_event_id": stop_event_id,
                    "scope_kind": scope_kind,
                    "scope_ref": scope_ref,
                    "action": action,
                    "initiator_kind": initiator_kind,
                    "initiator_ref": initiator_ref,
                    "reason": reason,
                    "approver_ref": approver_ref,
                    "signature": signature,
                },
            )

    async def releases_due_for_clearing(self, *, retain_s: float, limit: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RELEASES_DUE_FOR_CLEARING_SQL, {"retain_s": retain_s, "limit": limit})
            rows = await cur.fetchall()
        return [dict(row[0]) for row in rows]

    async def has_signature(self, signature: str) -> bool:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HAS_SIGNATURE_SQL, {"signature": signature})
            return await cur.fetchone() is not None

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LATEST_ACTION_SQL, {"scope_kind": scope_kind, "scope_ref": scope_ref})
            row = await cur.fetchone()
            return None if row is None else str(row[0])

    async def enqueue_publication(
        self,
        *,
        stop_id: UUID,
        action: Literal["ENGAGE", "RELEASE"],
        topic_suffix: str,
        payload: dict[str, Any],
    ) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _ENQUEUE_SQL,
                {
                    "stop_id": stop_id,
                    "action": action,
                    "topic_suffix": topic_suffix,
                    "payload": Jsonb(payload),
                },
            )

    async def record_and_enqueue(
        self, row: StopEventRow, *, stop_id: UUID, topic_suffix: str, payload: dict[str, Any]
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_INSERT_SQL, _row_params(row))
            await cur.execute(
                _ENQUEUE_SQL,
                {
                    "stop_id": stop_id,
                    "action": row.action,
                    "topic_suffix": topic_suffix,
                    "payload": Jsonb(payload),
                },
            )

    async def pending_publications(self, *, limit: int, max_attempts: int) -> list[OutboxEntry]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PENDING_SQL, {"limit": limit, "max_attempts": max_attempts})
            rows = await cur.fetchall()
        return [
            OutboxEntry(seq=int(r[0]), stop_id=r[1], action=r[2], topic_suffix=str(r[3]), payload=dict(r[4]))
            for r in rows
        ]

    async def mark_published(self, seq: int) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_MARK_PUBLISHED_SQL, {"seq": seq})

    async def record_publish_failure(self, seq: int, error: str, *, permanent: bool) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RECORD_FAILURE_SQL, {"seq": seq, "error": error[:500], "permanent": permanent})
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def raise_dead_letter_alert(self, entry: OutboxEntry, error: str) -> None:
        condition_key = f"{DEAD_LETTER_ALERT_RULE}:{entry.stop_id}:{entry.action}"
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _DEAD_LETTER_ALERT_SQL,
                {
                    "rule": DEAD_LETTER_ALERT_RULE,
                    "summary": f"stop {entry.action} {entry.stop_id} could not be published: {error[:200]}",
                    "detail": Jsonb(
                        {
                            "condition_key": condition_key,
                            "stop_id": str(entry.stop_id),
                            "action": entry.action,
                            "topic_suffix": entry.topic_suffix,
                            "error": error[:500],
                        }
                    ),
                    "stop_id": str(entry.stop_id),
                    "condition_key": condition_key,
                },
            )

    async def l2_engage_record(self, instruction_id: UUID, bank_id: str) -> RecordedL2Engage | None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _L2_ENGAGE_RECORD_SQL, {"bank_id": bank_id, "instruction_id": str(instruction_id)}
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return RecordedL2Engage(
            stop_id=row[0],
            bank_id=bank_id,
            reason=str(row[1]),
            initiator_ref=str(row[2]),
            has_publication=bool(row[3]),
            released=bool(row[4]),
        )

    async def has_l2_engage(self, instruction_id: UUID, bank_id: str) -> bool:
        """Whether a UTILITY-initiated BANK ENGAGE for this utility L2 instruction is already recorded
        (the L2 intake writes the instruction id into `reason`, see `opengrid.safestop.l2_intake`). This
        is the restart-proof half of the L2 idempotency check."""
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HAS_L2_ENGAGE_SQL, {"bank_id": bank_id, "instruction_id": str(instruction_id)})
            return await cur.fetchone() is not None


async def retry_trace_conflict[T](op: Callable[[], Awaitable[T]], *, attempts: int = 3) -> T:
    """Run `op` (a whole `SafestopService.engage` call), retrying on a Postgres unique violation.

    og-safestop's own tasks (API request intake, L2 intake) and the host CLI all append to the one
    `"safestop"` trace stream; two concurrent appends race on `(stream_id, seq)` and the loser fails with
    `UniqueViolation` *at the trace write* -- which `engage()` does first, before any row or publish, so
    the whole call is safe to redo. Anything else propagates unchanged."""
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except psycopg.errors.UniqueViolation:
            if attempt == attempts:
                raise
            logger.warning("safestop trace append conflict; retrying", extra={"attempt": attempt})
    raise AssertionError("unreachable")  # pragma: no cover


async def listen_for_requests(pool: AsyncConnectionPool) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON payloads NOTIFYd on `REQUEST_CHANNEL`, forever, until cancelled.

    A dedicated connection is taken from the pool for the lifetime of the LISTEN (psycopg's
    `conn.notifies()` requires holding one connection); this is the only long-lived connection
    `og-safestop` opens. Malformed (non-JSON) notifications are logged and skipped by the caller,
    never raised past this generator -- one bad request must not kill the listener (K7).
    """
    async with pool.connection() as conn:
        await conn.execute(f"LISTEN {REQUEST_CHANNEL}")
        await conn.commit()
        async for notify in conn.notifies():
            try:
                payload: dict[str, Any] = json.loads(notify.payload)
            except json.JSONDecodeError:
                continue  # malformed request: skip, don't kill the listener (K7 degrade, don't trip)
            yield payload
