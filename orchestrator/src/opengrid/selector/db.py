"""Selector's own read-only queries against `og.commitment`/`og.opportunity`/`og.obligation` (02a S2.2,
S3.8). Selector never writes these tables (`ledger`/`contracts` do); it only reads them to assemble a
gate's `ModelInputs`, exactly as `load_frozen_commitments`'s fixed interface already does for
`commitment`. No migrations exist yet for these tables (architect-owned, BUILD.md S4) -- this module is
correct against the DDL in `02a-mvp-s-spec-engine.md` S1 and will run once they land; until then it is
exercised in tests only through `_get_pool`, which callers/tests may monkeypatch.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool

_pool: AsyncConnectionPool | None = None

_FROZEN_COMMITMENTS_SQL = """
    SELECT obligation_id, interval_start, committed_kw
    FROM og.commitment
    WHERE supersedes IS NULL
      AND interval_start < %(horizon_end)s AND interval_end > %(horizon_start)s
"""


async def get_pool() -> AsyncConnectionPool:
    """Lazily creates the module-level pool from `OG_CONFIG` (BUILD.md S5). Tests never call this --
    they monkeypatch the functions below directly, since local unit/property tests run with no DB."""
    global _pool
    if _pool is None:
        _pool = await make_pool(load_config())
    return _pool


def _interval_key(interval_start: Any) -> str:
    return interval_start.isoformat() if hasattr(interval_start, "isoformat") else str(interval_start)


async def load_frozen_commitments_rows(horizon_start: str, horizon_end: str) -> list[tuple[UUID, Any, float]]:
    """Raw `(obligation_id, interval_start, committed_kw)` rows for `og.selector.load_frozen_commitments`
    (02a S2.2's exact query). Isolated from the dict-shaping so it is the one function an integration
    test needs to monkeypatch to exercise `load_frozen_commitments` without a live database."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _FROZEN_COMMITMENTS_SQL, {"horizon_start": horizon_start, "horizon_end": horizon_end}
        )
        return [(row[0], row[1], float(row[2])) async for row in cur]


async def load_frozen_commitments(horizon_start: str, horizon_end: str) -> dict[UUID, dict[str, float]]:
    """Read-only: the equality/lower-bound parameters for every committed obligation overlapping the
    horizon (02a S2.2). Never mutates `commitment`. Keys of the inner dict are ISO interval-start
    strings (the fixed interface leaves the string format to the implementation)."""
    rows = await load_frozen_commitments_rows(horizon_start, horizon_end)
    frozen: dict[UUID, dict[str, float]] = defaultdict(dict)
    for obligation_id, interval_start, committed_kw in rows:
        frozen[obligation_id][_interval_key(interval_start)] = committed_kw
    return dict(frozen)
