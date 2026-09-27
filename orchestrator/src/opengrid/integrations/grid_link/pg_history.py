"""L2 levels across og-engine restarts, read back from the link's own audit trail (grid-link.md S5.5).

Every L2 command received over the link is traced BEFORE it takes effect (K10) as decision OPERATOR_ACTION,
event GRID_LINK_COMMAND, on stream `grid_link:<utility_id>` (kept 400 days, `[retention]
"trace.operator_action"`). Replaying those rows in sequence order rebuilds exactly the levels the link held,
with no extra table: the trace IS the persisted state. Refused commands are never traced as
GRID_LINK_COMMAND, so they are never replayed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

__all__ = ["L2_COMMANDS", "load_l2_commands"]

#: The traced command names that change L2 levels (`GridCommand` class names).
L2_COMMANDS = ("L2LimitValue", "L2Limit", "L2Block")

_SQL = (
    "SELECT payload FROM og.trace"
    " WHERE stream_id = %s AND decision_type = 'OPERATOR_ACTION' AND event_class = 'GRID_LINK_COMMAND'"
    " AND payload->>'command' = ANY(%s)"
    " ORDER BY seq"
)


async def load_l2_commands(pool: AsyncConnectionPool, stream_id: str) -> list[dict[str, Any]]:
    """The traced L2 command payloads of `stream_id`, oldest first."""
    async with pool.connection() as conn:
        cursor = await conn.execute(_SQL, (stream_id, list(L2_COMMANDS)))
        rows = await cursor.fetchall()
    return [dict(row[0]) for row in rows if isinstance(row[0], dict)]
