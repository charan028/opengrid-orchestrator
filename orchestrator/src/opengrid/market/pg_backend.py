"""Postgres read side of the $/kW view: per-contract period totals from settled data (`og.pnl`,
`og.obligation`, `og.contract`). Kept apart from the pure modules (BUILD.md S5a "pure logic separated
from I/O"); `contract_totals_from_row` is the pure mapping and is unit-tested on its own.

Until settle's 09 S4 tables exist (`og.charge_ledger`, `og.asset_finance`, `og.econ_rollup`), two
figures are PLANNING attributions, flagged in each row's `notes`:
- capex = kW basis x the hardware view ($7,000 / 11 kW);
- O&M = 3% of that capex per year, pro-rated to the period (08 S3c).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.market.capacity import HOURS_PER_YEAR
from opengrid.market.config import HOME_UNIT_CAPEX_USD, HOME_UNIT_KW
from opengrid.market.economics import ILLUSTRATIVE_OM_FRAC, PeriodTotals
from opengrid.market.territory import MarketModelError, market_of

#: Services whose revenue is a capacity payment (08 S3.4 "$/kW-out: capacity payments").
CAPACITY_SERVICE_TYPES = frozenset(
    {"REGULATED_CAPACITY", "DIST_DEFERRAL", "PARTNER_CAPACITY", "ERCOT_AS", "DATA_CENTER", "PJM_CAPACITY"}
)
PLANNING_NOTE = "PLANNING capex/O&M: hardware view $7,000/11 kW, O&M 3%/yr (no og.asset_finance yet)"

_ZERO = Decimal("0")

_CONTRACT_TOTALS_SQL = """
WITH pnl AS (
    SELECT o.contract_id,
           sum(pn.revenue) AS revenue, sum(pn.energy_cost) AS energy_cost,
           sum(pn.degradation_cost) AS degradation_cost, sum(pn.penalty) AS penalty,
           sum(pn.delivery_charge) AS delivery_charge
    FROM og.pnl pn
    JOIN og.obligation o ON o.obligation_id = pn.obligation_id
    WHERE pn.superseded_by IS NULL
      AND pn.interval_start >= %(start)s AND pn.interval_start < %(end)s
    GROUP BY o.contract_id
),
held AS (
    SELECT o.contract_id,
           sum(o.committed_qty_kw * extract(epoch FROM
               (least(o.window_end, %(end)s) - greatest(o.window_start, %(start)s))) / 3600) AS kwh_held,
           sum(extract(epoch FROM
               (least(o.window_end, %(end)s) - greatest(o.window_start, %(start)s))) / 3600) AS hours_held
    FROM og.obligation o
    WHERE o.window_end > %(start)s AND o.window_start < %(end)s
      AND o.state NOT IN ('OFFERED', 'REJECTED', 'EXPIRED')
    GROUP BY o.contract_id
)
SELECT c.contract_id, c.service_type, c.market, c.utility_id,
       coalesce(pnl.revenue, 0) AS revenue, coalesce(pnl.energy_cost, 0) AS energy_cost,
       coalesce(pnl.degradation_cost, 0) AS degradation_cost, coalesce(pnl.penalty, 0) AS penalty,
       coalesce(pnl.delivery_charge, 0) AS delivery_charge,
       coalesce(held.kwh_held, 0) AS kwh_held, coalesce(held.hours_held, 0) AS hours_held
FROM og.contract c
LEFT JOIN pnl ON pnl.contract_id = c.contract_id
LEFT JOIN held ON held.contract_id = c.contract_id
WHERE pnl.contract_id IS NOT NULL OR held.contract_id IS NOT NULL
ORDER BY c.contract_id
"""


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def contract_totals_from_row(row: Mapping[str, Any], period_hours: Decimal) -> PeriodTotals:
    """One contract's `PeriodTotals` from a `_CONTRACT_TOTALS_SQL` row.

    kW basis (09 S4): time-weighted committed kW while active = committed kW-h / active hours. An
    inconsistent market row is reported with `market=None` (fleet only) and a note, never guessed."""
    notes = [PLANNING_NOTE]
    try:
        market = market_of(
            market=row.get("market"), utility_id=row.get("utility_id"), service_type=row.get("service_type")
        ).market
    except MarketModelError as exc:
        market = None
        notes.append(f"market unresolved: {exc}")
    hours_held = _dec(row["hours_held"])
    kw_basis = _dec(row["kwh_held"]) / hours_held if hours_held > _ZERO else _ZERO
    capex = kw_basis * HOME_UNIT_CAPEX_USD / HOME_UNIT_KW
    revenue = _dec(row["revenue"])
    is_capacity = row.get("service_type") in CAPACITY_SERVICE_TYPES
    return PeriodTotals(
        scope_kind="CONTRACT",
        scope_ref=str(row["contract_id"]),
        market=market,
        kw_basis=kw_basis,
        hours=period_hours,
        charging_energy_usd=_dec(row["energy_cost"]),
        delivery_charge_usd=_dec(row["delivery_charge"]),
        capacity_revenue_usd=revenue if is_capacity else _ZERO,
        energy_revenue_usd=_ZERO if is_capacity else revenue,
        penalty_usd=_dec(row["penalty"]),
        wear_usd=_dec(row["degradation_cost"]),
        om_usd=capex * ILLUSTRATIVE_OM_FRAC * period_hours / HOURS_PER_YEAR,
        capex_usd=capex,
        notes=tuple(notes),
    )


async def fetch_contract_totals(
    pool: AsyncConnectionPool[Any], start: datetime, end: datetime
) -> list[PeriodTotals]:
    """Per-contract `PeriodTotals` for settled intervals starting in `[start, end)`."""
    if end <= start:
        raise ValueError("end must be after start")
    period_hours = Decimal(str((end - start).total_seconds())) / Decimal("3600")
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONTRACT_TOTALS_SQL, {"start": start, "end": end})
        rows = await cur.fetchall()
    return [contract_totals_from_row(r, period_hours) for r in rows]
