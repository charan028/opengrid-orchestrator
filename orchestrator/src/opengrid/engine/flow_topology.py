"""The 09 S1.9 F2/F3 registry (migration 0029) for the allocator's flow limits: hub -> service transformer and
meter export limit, transformer ratings, bank -> feeder / substation, and the feeder and substation limits.

Re-read every `REFRESH_S` (static install data, not per cycle). Each level applies only where its rows
exist; with no feeder/substation SCADA in the engine, the discharge budget of a feeder or substation is its
permitted reverse flow (`reverse_kw`), the conservative static bound (the guardian's G-28/G-29 check the
same limits on their own reads). An unreadable registry (0029 not applied yet) keeps the last one read.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.allocator.models import FlowLimits

logger = logging.getLogger(__name__)

REFRESH_S = 60.0

_HUBS_SQL = """
SELECT hub_id, transformer_id, export_limit_kw FROM og.hub
WHERE transformer_id IS NOT NULL OR export_limit_kw IS NOT NULL
"""
_XFMR_SQL = "SELECT transformer_id, rating_kva FROM og.service_transformer"
_BANK_FEEDER_SQL = "SELECT bank_id, feeder_id FROM og.bank WHERE feeder_id IS NOT NULL"
_BANK_SUBSTATION_SQL = """
SELECT bank_id, substation_id FROM og.asset
WHERE asset_class = 'HOME_BANK' AND bank_id IS NOT NULL AND substation_id IS NOT NULL
"""
_FEEDER_SQL = "SELECT feeder_id, reverse_kw FROM og.feeder_limit WHERE reverse_kw IS NOT NULL"
_SUBSTATION_SQL = "SELECT substation_id, reverse_kw FROM og.substation_limit WHERE reverse_kw IS NOT NULL"


def topology_limits(base: FlowLimits, rows: dict[str, list[tuple[Any, ...]]]) -> FlowLimits:
    """Pure: `base` (config) with the registry rows merged in (the registry wins over config tables)."""
    hubs = rows.get("hubs", [])
    return replace(
        base,
        xfmr_by_hub={str(h): str(x) for h, x, _ in hubs if x is not None},
        export_limit_by_hub={str(h): float(e) for h, _, e in hubs if e is not None},
        xfmr_kva={**base.xfmr_kva, **{str(x): float(r) for x, r in rows.get("xfmr", [])}},
        feeder_by_bank={str(b): str(f) for b, f in rows.get("bank_feeder", [])},
        substation_by_bank={str(b): str(s) for b, s in rows.get("bank_substation", [])},
        feeder_budget_kw={**base.feeder_budget_kw, **{str(f): float(r) for f, r in rows.get("feeder", [])}},
        substation_budget_kw={
            **base.substation_budget_kw,
            **{str(s): float(r) for s, r in rows.get("substation", [])},
        },
    )


class FlowTopology:
    def __init__(self, pool: AsyncConnectionPool, base: FlowLimits, *, refresh_s: float = REFRESH_S) -> None:
        self._pool = pool
        self._base = base
        self._limits = base
        self._refresh_s = refresh_s
        self._loaded_at: datetime | None = None

    async def limits(self, now: datetime) -> FlowLimits:
        if not self._base.enabled:
            return self._base
        if self._loaded_at is None or (now - self._loaded_at).total_seconds() >= self._refresh_s:
            self._loaded_at = now
            try:
                self._limits = topology_limits(self._base, await self._read())
            except Exception:
                logger.warning("flow-limit registry unreadable; keeping the last one read", exc_info=True)
        return self._limits

    async def _read(self) -> dict[str, list[tuple[Any, ...]]]:
        rows: dict[str, list[tuple[Any, ...]]] = {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            for key, sql in (
                ("hubs", _HUBS_SQL),
                ("xfmr", _XFMR_SQL),
                ("bank_feeder", _BANK_FEEDER_SQL),
                ("bank_substation", _BANK_SUBSTATION_SQL),
                ("feeder", _FEEDER_SQL),
                ("substation", _SUBSTATION_SQL),
            ):
                await cur.execute(sql)
                rows[key] = [tuple(r) for r in await cur.fetchall()]
        return rows
