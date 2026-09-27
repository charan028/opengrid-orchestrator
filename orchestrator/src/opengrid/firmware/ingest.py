"""Ingest of the hub's timestamped firmware status (`<root>/ack/fw/<hub_id>`, firmware_status.schema.json).

The engine's MQTT ingest calls `record_firmware_status(pool, payload)` for every message on that topic
(after `validate_payload("firmware_status", payload)`). The status is written onto the matching
`og.firmware_command` row -- only when it is for that hub and newer than what is stored, so a late or
replayed message never moves a job backwards -- and every message is also kept as a timestamped
`og.firmware_job_event` (the battery's own progress notifications, owner addition 1). The executor reads
the row on its next step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.timeutil import to_utc

_UPDATE_SQL: Final = """
UPDATE og.firmware_command SET hub_state = %(state)s, hub_reason = %(reason)s, hub_version = %(version)s,
    hub_ts = %(ts)s
WHERE command_id = %(command_id)s AND hub_id = %(hub_id)s AND (hub_ts IS NULL OR hub_ts <= %(ts)s)
RETURNING job_id, campaign_id, attempt
"""

_EVENT_SQL: Final = """
INSERT INTO og.firmware_job_event (campaign_id, job_id, hub_id, event, reason, attempt, detail, ts)
VALUES (%(campaign_id)s, %(job_id)s, %(hub_id)s, %(event)s, %(reason)s, %(attempt)s, %(detail)s, %(ts)s)
"""


def parse_status(payload: dict[str, Any]) -> dict[str, Any]:
    """The fields the orchestrator stores, normalised (ts to UTC). Raises KeyError/ValueError on a message
    missing required fields -- the caller has already schema-validated it."""
    ts = payload["ts"]
    parsed_ts = to_utc(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
    return {
        "hub_id": str(payload["hub_id"]),
        "command_id": str(payload["command_id"]),
        "state": str(payload["state"]),
        "reason": payload.get("reason"),
        "version": str(payload["firmware_version"]),
        "progress_pct": payload.get("progress_pct"),
        "ts": parsed_ts,
    }


async def record_firmware_status(pool: AsyncConnectionPool, payload: dict[str, Any]) -> bool:
    """Store one hub status. Returns False when it matched no command of that hub (ignored)."""
    status = parse_status(payload)
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(_UPDATE_SQL, status)
        row = await cur.fetchone()
        if row is None:
            return False
        job_id, campaign_id, attempt = row
        await cur.execute(
            _EVENT_SQL,
            {
                "campaign_id": campaign_id,
                "job_id": job_id,
                "hub_id": status["hub_id"],
                "event": f"HUB_{status['state']}",
                "reason": status["reason"],
                "attempt": attempt,
                "detail": Jsonb(
                    {
                        "firmware_version": status["version"],
                        "progress_pct": status["progress_pct"],
                        "command_id": status["command_id"],
                    }
                ),
                "ts": status["ts"],
            },
        )
    return True
