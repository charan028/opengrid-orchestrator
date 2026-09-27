"""I/O for the ERCOT AS instruction poller (D-35): the award read, alerts, the shared-core gateway and the
builder og-feeds calls. The poller's logic is `opengrid.contracts.as_deployment_poll` (no I/O there).

- `PgAwardLookup`: read-only -- which ERCOT_AS obligation of a contract covers an instant.
- `PgPollAlerts`: raise-once / clear through health's single `og.alert` writer (`opengrid.health.queries`),
  matched by `detail.condition_key` so an alert raised before a restart is found again. Health never
  auto-clears these rules (they are not in `HEALTH_OWNED_ALERT_RULES`).
- `CoreCallGateway`: the operator route's own core (`opengrid.calls`) with origin ERCOT_POLL; nothing about
  deployability is decided here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from psycopg_pool import AsyncConnectionPool

from opengrid.contracts.as_deployment_poll import CallOutcome, ErcotAsPoller, settings_from
from opengrid.health.model import AlertFinding
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert
from opengrid.integrations.ercot_mms.client import ErcotMmsClient
from opengrid.integrations.interfaces import DispatchInstruction
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

_AWARDS_COVERING_SQL = """
    SELECT o.obligation_id FROM og.obligation o
    WHERE o.contract_id = %(contract_id)s AND o.service_type = 'ERCOT_AS'
      AND o.window_start <= %(at)s AND o.window_end > %(at)s
      AND o.state NOT IN ('REJECTED', 'EXPIRED')
    ORDER BY o.window_start, o.obligation_id
"""


class PgAwardLookup:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def awards_covering(self, contract_id: UUID, at: datetime) -> list[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_AWARDS_COVERING_SQL, {"contract_id": contract_id, "at": at})
            rows = await cur.fetchall()
        return [UUID(str(r[0])) for r in rows]


class PgPollAlerts:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _open_ids(self, rule: str, condition_key: str) -> list[int]:
        return [
            a.id
            for a in await fetch_open_alerts(self._pool)
            if a.rule == rule and a.id is not None and (a.detail or {}).get("condition_key") == condition_key
        ]

    async def raise_once(self, rule: str, condition_key: str, summary: str, detail: dict[str, Any]) -> None:
        if await self._open_ids(rule, condition_key):
            return
        finding = AlertFinding(
            rule=rule,
            severity="critical" if rule.endswith("STALE") else "warning",
            summary=summary,
            condition_key=condition_key,
            detail={**detail, "condition_key": condition_key},
        )
        await raise_alert(self._pool, finding, opened_at=datetime.now(UTC))

    async def clear(self, rule: str, condition_key: str) -> None:
        for alert_id in await self._open_ids(rule, condition_key):
            await clear_alert(self._pool, alert_id)


def build_as_deployment_poller(
    cfg: object, pool: AsyncConnectionPool, *, trace: TraceStore, http_client: httpx.AsyncClient | None = None
) -> ErcotAsPoller | None:
    """The og-feeds poller, or None while `[feeds.ercot_as_poll].enabled` is false (the default)."""
    settings = settings_from(cfg)
    if not settings.enabled or settings.mms is None:
        return None
    source = ErcotMmsClient(settings.mms, client=http_client)
    logger.info(
        "ERCOT AS instruction poller enabled",
        extra={
            "endpoint": settings.mms.endpoint,
            "interval_s": settings.interval_s,
            "awards": len(settings.awards),
        },
    )
    return ErcotAsPoller(
        source=source,
        calls=CoreCallGateway(pool, trace, cfg),
        awards=PgAwardLookup(pool),
        alerts=PgPollAlerts(pool),
        trace=trace,
        settings=settings,
    )


class CoreCallGateway:
    """`AsCallGateway` over the shared AS-deployment core (`opengrid.calls`)."""

    def __init__(self, pool: AsyncConnectionPool, trace: TraceStore, cfg: object) -> None:
        self._pool = pool
        self._trace = trace
        self._cfg = cfg

    async def deploy(
        self, instruction: DispatchInstruction, *, obligation_id: UUID, principal: str, now: datetime
    ) -> CallOutcome:
        raise NotImplementedError  # wired to opengrid.calls.issue_call (integ/utility-api)

    async def recall(self, deployment_instruction_id: str, *, principal: str, now: datetime) -> CallOutcome:
        raise NotImplementedError  # wired to opengrid.calls.cancel_call

    async def has_call(self, instruction_id: str, *, principal: str) -> bool:
        raise NotImplementedError  # wired to opengrid.calls.find_call_by_key
