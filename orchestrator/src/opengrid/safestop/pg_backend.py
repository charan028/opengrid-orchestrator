"""Postgres implementation of `StopEventBackend` (02a S1.12 `og.stop_event`), plus the LISTEN/NOTIFY
request intake `main.py` uses to receive stop requests from `og-api` without importing it.

Only `opengrid.platform.db` (a connection pool) is used here -- no engine/guardian/allocator import
(K8 import isolation, see `tests/unit/safestop/test_import_isolation.py`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

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
