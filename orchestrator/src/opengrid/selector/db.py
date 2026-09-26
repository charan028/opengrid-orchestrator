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

from psycopg.rows import dict_row
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

_BANK_IDS_SQL = "SELECT bank_id FROM og.bank ORDER BY bank_id"

_OFFERED_OPPORTUNITIES_SQL = """
    SELECT o.opportunity_id, ob.obligation_id, o.contract_id, o.window_start, o.window_end,
           o.requested_kw, o.value_per_mwh, c.service_type, c.tier, c.degradation_cost,
           pr.variable_kind, pr.min_qty_kw, pr.increment_kw
    FROM og.opportunity o
    JOIN og.contract c ON c.contract_id = o.contract_id
    JOIN og.obligation ob ON ob.opportunity_id = o.opportunity_id
    LEFT JOIN og.product_rule pr ON pr.product_rule_id = o.product_rule_id
    WHERE o.state = 'OFFERED'
      AND o.window_start < %(horizon_end)s AND o.window_end > %(horizon_start)s
      AND (%(contract_scope)s::uuid IS NULL OR o.contract_id = %(contract_scope)s::uuid)
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


async def load_bank_ids_rows() -> list[str]:
    """Read-only: every real `bank_id` currently in `og.bank` (the fleet topology's source of truth,
    seeded by `opengrid.fleet.seed`), ordered for determinism. `gate._configured_bank_ids` uses this
    instead of fabricating an id list from a count + format guess, so selector can never propose or
    reserve capacity against a bank that does not actually exist in the topology."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_BANK_IDS_SQL)
        return [row[0] async for row in cur]


async def load_offered_opportunities_rows(
    horizon_start: str, horizon_end: str, contract_scope: UUID | None
) -> list[dict[str, Any]]:
    """Raw `OFFERED` opportunity rows overlapping the horizon (02a S1.4/S3.2 O^new), joined to their
    contract (for `service_type`/`degradation_cost`) and cached `product_rule.variable_kind` (02a
    S1.3 -- already derived at admission time, never re-derived here per BUILD.md S1). This module's
    own docstring already scopes selector to read `og.opportunity` directly (contracts/ledger own the
    writes); `contracts` has no public opportunity-listing query yet (`admit`/`product_rules_for` are
    its only fixed interface calls that touch `opportunity`), so `gate.load_candidates` reads this
    table itself rather than staying a permanent placeholder -- see the selector module's final-report
    note asking the merge agent to add a `contracts` query for this instead, once one exists.

    A `product_rule_id`-less opportunity (no row joined) is treated as `CONTINUOUS`, `min_qty_kw=0`,
    `increment_kw=0` -- the review's "else -> CONTINUOUS" default (02a S1.3).
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _OFFERED_OPPORTUNITIES_SQL,
            {
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "contract_scope": str(contract_scope) if contract_scope is not None else None,
            },
        )
        return [dict(row) async for row in cur]
