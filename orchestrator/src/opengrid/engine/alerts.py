"""Clearing the alerts og-engine raises itself (`ALR-SELECTOR-GATE-FAILED`, `ALR-ENERGY-SHORTFALL-RISK`).

Health auto-clears only its own rules (`HEALTH_OWNED_ALERT_RULES`); an alert another module raised stays
open until that module clears it. The raiser clears through health's single `og.alert` writer
(`opengrid.health.queries`), matching open alerts of its rule by their stored `detail`, so an alert raised
before a restart is still found and cleared.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.health.queries import clear_alert, fetch_open_alerts

logger = logging.getLogger(__name__)


async def open_alert_details(pool: AsyncConnectionPool, rule: str) -> list[dict[str, Any]]:
    """The stored `detail` of every open `rule` alert (to adopt alerts a previous process raised)."""
    return [a.detail or {} for a in await fetch_open_alerts(pool) if a.rule == rule]


async def clear_open_alerts(
    pool: AsyncConnectionPool, rule: str, matches: Callable[[dict[str, Any]], bool]
) -> int:
    """Clear every open `rule` alert whose detail satisfies `matches`. Returns how many were cleared."""
    cleared = 0
    now = datetime.now(UTC)
    for alert in await fetch_open_alerts(pool):
        if alert.rule != rule or alert.id is None or not matches(alert.detail or {}):
            continue
        await clear_alert(pool, alert.id, cleared_at=now)
        cleared += 1
    if cleared:
        logger.info("owner cleared resolved alerts", extra={"rule": rule, "cleared": cleared})
    return cleared
