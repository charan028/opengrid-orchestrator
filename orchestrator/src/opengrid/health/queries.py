"""Database I/O for the health evaluator (02b S6.4). Isolated from `health.rules`'s pure logic per
BUILD.md S5a ("pure logic separated from I/O")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import Alert, FeedStatus, Heartbeat
from opengrid.health.model import AlertFinding

_FETCH_HEARTBEATS_SQL = "SELECT process, pid, ts, status FROM og.heartbeat"

_FETCH_HUB_STATES_SQL = """
SELECT h.zone, hs.hub_id, hs.last_seen_at, hs.fault_code
FROM og.hub_state hs JOIN og.hub h ON h.hub_id = hs.hub_id
"""

_UPDATE_HUB_HEALTH_SQL = "UPDATE og.hub_state SET health = %(health)s WHERE hub_id = %(hub_id)s"

_FETCH_FEED_STATUSES_SQL = """
SELECT source, product, last_value_at, last_success_at, consecutive_failures, breaker_open, active_key
FROM og.feed_status
"""

_FETCH_OPEN_ALERTS_SQL = """
SELECT id, rule, severity, summary, detail, opened_at, cleared_at, acked_by
FROM og.alert WHERE cleared_at IS NULL
"""

_INSERT_ALERT_SQL = """
INSERT INTO og.alert (rule, severity, summary, detail, opened_at)
VALUES (%(rule)s, %(severity)s, %(summary)s, %(detail)s, %(opened_at)s)
RETURNING id
"""

_CLEAR_ALERT_SQL = "UPDATE og.alert SET cleared_at = %(cleared_at)s WHERE id = %(alert_id)s"


async def fetch_heartbeats(pool: AsyncConnectionPool) -> list[Heartbeat]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_HEARTBEATS_SQL)
        rows = await cur.fetchall()
    return [Heartbeat(process=r[0], pid=r[1], ts=r[2], status=r[3]) for r in rows]


async def fetch_hub_states(pool: AsyncConnectionPool) -> list[tuple[str, str, datetime | None, str | None]]:
    """Returns `(zone, hub_id, last_seen_at, fault_code)` tuples."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_HUB_STATES_SQL)
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def write_hub_health(pool: AsyncConnectionPool, hub_id: str, health: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_UPDATE_HUB_HEALTH_SQL, {"hub_id": hub_id, "health": health})
        await conn.commit()


async def fetch_feed_statuses(pool: AsyncConnectionPool) -> list[FeedStatus]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_FEED_STATUSES_SQL)
        rows = await cur.fetchall()
    return [
        FeedStatus(
            source=r[0],
            product=r[1],
            last_value_at=r[2],
            last_success_at=r[3],
            consecutive_failures=r[4],
            breaker_open=r[5],
            active_key=r[6],
        )
        for r in rows
    ]


async def fetch_open_alerts(pool: AsyncConnectionPool) -> list[Alert]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_OPEN_ALERTS_SQL)
        rows = await cur.fetchall()
    return [
        Alert(
            id=r[0],
            rule=r[1],
            severity=r[2],
            summary=r[3],
            detail=r[4],
            opened_at=r[5],
            cleared_at=r[6],
            acked_by=r[7],
        )
        for r in rows
    ]


async def raise_alert(pool: AsyncConnectionPool, finding: AlertFinding, *, opened_at: datetime) -> int:
    from psycopg.types.json import Jsonb  # local import: only this write path needs the jsonb adapter

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_ALERT_SQL,
            {
                "rule": finding.rule,
                "severity": finding.severity,
                "summary": finding.summary,
                "detail": Jsonb(finding.detail) if finding.detail else None,
                "opened_at": opened_at,
            },
        )
        row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id yielded no row")
    return int(row[0])


async def clear_alert(
    pool: AsyncConnectionPool, alert_id: int, *, cleared_at: datetime | None = None
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _CLEAR_ALERT_SQL, {"alert_id": alert_id, "cleared_at": cleared_at or datetime.now(UTC)}
        )
        await conn.commit()


def condition_key_for(alert: Alert) -> str:
    """Rebuild the `condition_key` an open `og.alert` row corresponds to, from its stored detail, so a
    fresh evaluation cycle's findings can be matched against it (rule+scope, mirroring `AlertFinding`)."""
    detail: dict[str, Any] = alert.detail or {}
    scope = (
        detail.get("process")
        or detail.get("zone")
        or ":".join(str(v) for v in (detail.get("source"), detail.get("product")) if v)
    )
    return f"{alert.rule}:{scope}" if scope else alert.rule
