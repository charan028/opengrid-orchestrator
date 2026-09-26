"""Postgres implementation of `SettleBackend` (02a S7), using `opengrid.platform.db`'s async pool.

Kept separate from `opengrid.settle.backend` so the Protocol + pure orchestration code has no
`psycopg` import (BUILD.md S5a: "pure logic separated from I/O"), mirroring
`opengrid.trace.store`/`opengrid.trace.pg_backend`'s split.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.settle.backend import (
    ExistingInvoiceLineRow,
    ExistingMeterInterval,
    ExistingPnl,
    InvoiceLineExportRow,
    MeterIntervalExportRow,
)
from opengrid.settle.models import (
    InvoiceLineDraft,
    ObligationSettlementContext,
    PenaltyParams,
    PowerSample,
    QualityFlag,
)

_FETCH_CONTEXT_SQL = """
SELECT
    o.obligation_id, o.contract_id, o.service_type, o.committed_qty_kw,
    c.penalty_alpha, c.penalty_beta, c.penalty_theta, c.degradation_cost,
    opp.value_per_mwh
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
JOIN og.opportunity opp ON opp.opportunity_id = o.opportunity_id
WHERE o.obligation_id = %(obligation_id)s
"""

_FETCH_TELEMETRY_SQL = """
SELECT hub_id, ts, p_kw
FROM og.telemetry
WHERE hub_id IN (SELECT hub_id FROM og.hub WHERE bank_id IN (
        SELECT bank_id FROM og.reservation WHERE obligation_id = %(obligation_id)s
    ))
  AND ts >= %(interval_start)s AND ts < %(interval_end)s
ORDER BY ts
"""

_FETCH_ACTIVE_METER_SQL = """
SELECT meter_interval_id, delivered_kwh, version
FROM og.meter_interval
WHERE obligation_id = %(obligation_id)s AND interval_start = %(interval_start)s
  AND superseded_by IS NULL
"""

_INSERT_METER_SQL = """
INSERT INTO og.meter_interval
    (meter_interval_id, obligation_id, interval_start, interval_end, delivered_kwh, baseline_kwh,
     source, quality_flag, version)
VALUES (%(id)s, %(obligation_id)s, %(interval_start)s, %(interval_end)s, %(delivered_kwh)s,
        %(baseline_kwh)s, %(source)s, %(quality_flag)s, %(version)s)
"""

_SUPERSEDE_METER_SQL = """
UPDATE og.meter_interval SET superseded_by = %(new_id)s WHERE meter_interval_id = %(old_id)s
"""

_INSERT_PERFORMANCE_SQL = """
INSERT INTO og.performance
    (performance_id, obligation_id, interval_start, interval_end, compliance_pct, passed_threshold)
VALUES (%(id)s, %(obligation_id)s, %(interval_start)s, %(interval_end)s, %(compliance_pct)s,
        %(passed_threshold)s)
"""

_FETCH_ACTIVE_INVOICE_LINE_SQL = """
SELECT invoice_line_id, amount, version
FROM og.invoice_line
WHERE contract_id = %(contract_id)s AND obligation_id = %(obligation_id)s
  AND period_start = %(period_start)s AND period_end = %(period_end)s AND line_type = %(line_type)s
ORDER BY version DESC LIMIT 1
"""

_INSERT_INVOICE_LINE_SQL = """
INSERT INTO og.invoice_line
    (invoice_line_id, contract_id, obligation_id, period_start, period_end, line_type, quantity,
     unit, rate, amount, status, supersedes, version)
VALUES (%(id)s, %(contract_id)s, %(obligation_id)s, %(period_start)s, %(period_end)s, %(line_type)s,
        %(quantity)s, %(unit)s, %(rate)s, %(amount)s, %(status)s, %(supersedes)s, %(version)s)
"""

_FETCH_ACTIVE_PNL_SQL = """
SELECT pnl_id, net_value, version
FROM og.pnl
WHERE obligation_id = %(obligation_id)s AND interval_start = %(interval_start)s
  AND superseded_by IS NULL
"""

_SUPERSEDE_PNL_SQL = """
UPDATE og.pnl SET superseded_by = %(new_id)s WHERE pnl_id = %(old_id)s
"""

_FETCH_INVOICE_LINES_FOR_PERIOD_SQL = """
SELECT invoice_line_id, contract_id, obligation_id, period_start, period_end, line_type, quantity,
       unit, rate, amount, status, supersedes, version
FROM og.invoice_line
WHERE contract_id = %(contract_id)s AND period_start >= %(period_start)s AND period_end <= %(period_end)s
ORDER BY obligation_id, line_type, version
"""

_FETCH_METER_INTERVALS_FOR_PERIOD_SQL = """
SELECT meter_interval_id, obligation_id, interval_start, interval_end, delivered_kwh, baseline_kwh,
       source, quality_flag, version, superseded_by
FROM og.meter_interval
WHERE obligation_id = %(obligation_id)s AND interval_start >= %(period_start)s
  AND interval_start < %(period_end)s
ORDER BY interval_start, version
"""

_FETCH_PENDING_INTERVALS_SQL = """
SELECT o.obligation_id, gs.interval_start, gs.interval_start + interval '15 minutes' AS interval_end
FROM og.obligation o
CROSS JOIN LATERAL generate_series(
    date_trunc('hour', o.window_start)
        + (floor(extract(minute FROM o.window_start) / 15) * interval '15 minutes'),
    LEAST(now(), o.window_end) - interval '15 minutes',
    interval '15 minutes'
) AS gs(interval_start)
LEFT JOIN og.meter_interval mi
    ON mi.obligation_id = o.obligation_id AND mi.interval_start = gs.interval_start
   AND mi.superseded_by IS NULL
WHERE o.state IN ('DELIVERING', 'FULFILLED', 'SHORTFALL') AND mi.meter_interval_id IS NULL
ORDER BY o.obligation_id, gs.interval_start
LIMIT 500
"""

_FETCH_SETTLEABLE_SQL = """
SELECT o.obligation_id
FROM og.obligation o
WHERE o.state IN ('FULFILLED', 'SHORTFALL')
  AND NOT EXISTS (
      SELECT 1
      FROM generate_series(
          date_trunc('hour', o.window_start)
              + (floor(extract(minute FROM o.window_start) / 15) * interval '15 minutes'),
          o.window_end - interval '15 minutes',
          interval '15 minutes'
      ) AS gs(interval_start)
      WHERE NOT EXISTS (
          SELECT 1 FROM og.meter_interval mi
          WHERE mi.obligation_id = o.obligation_id AND mi.interval_start = gs.interval_start
            AND mi.superseded_by IS NULL
      )
  )
  AND EXISTS (SELECT 1 FROM og.pnl p WHERE p.obligation_id = o.obligation_id)
  AND (o.service_type = 'HOME'
       OR EXISTS (SELECT 1 FROM og.invoice_line il WHERE il.obligation_id = o.obligation_id))
LIMIT 200
"""

_INSERT_PNL_SQL = """
INSERT INTO og.pnl
    (pnl_id, obligation_id, interval_start, interval_end, revenue, energy_cost, degradation_cost,
     penalty, net_value, rule_baseline_value, forgone_upside, version)
VALUES (%(id)s, %(obligation_id)s, %(interval_start)s, %(interval_end)s, %(revenue)s, %(energy_cost)s,
        %(degradation_cost)s, %(penalty)s, %(net_value)s, %(rule_baseline_value)s, %(forgone_upside)s,
        %(version)s)
"""


@dataclass(frozen=True, slots=True)
class PgSettleBackend:
    """`SettleBackend` backed by Postgres. Assumes the caller (`settle.main`) provides one pool per
    process, per `opengrid.platform.db.make_pool`."""

    pool: AsyncConnectionPool

    async def fetch_context(self, obligation_id: UUID) -> ObligationSettlementContext:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_CONTEXT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"obligation not found: {obligation_id}")

        penalty = None
        if row["penalty_alpha"] is not None and row["penalty_beta"] is not None:
            penalty = PenaltyParams(
                alpha=row["penalty_alpha"],
                beta=row["penalty_beta"],
                theta=row["penalty_theta"] or Decimal("0"),
            )
        price_per_kwh = (row["value_per_mwh"] or Decimal("0")) / Decimal("1000")
        # MVP-S simplification: wholesale price basis mirrors the contract's own value_per_mwh until
        # `feeds`/`forecast` wiring lands a per-bank market price lookup here (02a S7.4 Open points).
        wholesale_price_per_kwh = price_per_kwh
        today = datetime.now(UTC).date()  # MVP-S: daily billing period, UTC calendar date
        return ObligationSettlementContext(
            obligation_id=row["obligation_id"],
            contract_id=row["contract_id"],
            service_type=row["service_type"],
            committed_kw=row["committed_qty_kw"],
            price_per_kwh=price_per_kwh,
            wholesale_price_per_kwh=wholesale_price_per_kwh,
            eta_d=Decimal("0.9487"),
            degradation_cost_per_kwh=row["degradation_cost"],
            penalty=penalty,
            period_start=today,
            period_end=today,
        )

    async def fetch_power_samples(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> list[PowerSample]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_TELEMETRY_SQL,
                {
                    "obligation_id": obligation_id,
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                },
            )
            rows = await cur.fetchall()
        return [PowerSample(hub_id=r["hub_id"], ts=r["ts"], kw=Decimal(str(r["p_kw"]))) for r in rows]

    async def fetch_active_meter_interval(
        self, obligation_id: UUID, interval_start: datetime
    ) -> ExistingMeterInterval | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_ACTIVE_METER_SQL, {"obligation_id": obligation_id, "interval_start": interval_start}
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ExistingMeterInterval(
            meter_interval_id=row["meter_interval_id"],
            delivered_kwh=row["delivered_kwh"],
            version=row["version"],
        )

    async def insert_meter_interval(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        delivered_kwh: Decimal,
        baseline_kwh: Decimal | None,
        source: str,
        quality_flag: QualityFlag,
        version: int,
        supersedes: UUID | None = None,
    ) -> UUID:
        new_id = uuid4()
        async with self.pool.connection() as conn, conn.cursor() as cur:
            # Retire the old row FIRST (see the Protocol docstring): the deferred FK (migration 0007)
            # lets `superseded_by` point at `new_id` before that row exists, validated only when this
            # `async with` block commits the transaction on exit.
            if supersedes is not None:
                await cur.execute(_SUPERSEDE_METER_SQL, {"old_id": supersedes, "new_id": new_id})
            await cur.execute(
                _INSERT_METER_SQL,
                {
                    "id": new_id,
                    "obligation_id": obligation_id,
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                    "delivered_kwh": delivered_kwh,
                    "baseline_kwh": baseline_kwh,
                    "source": source,
                    "quality_flag": quality_flag,
                    "version": version,
                },
            )
        return new_id

    async def insert_performance(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        compliance_pct: Decimal | None,
        passed_threshold: bool,
    ) -> UUID:
        new_id = uuid4()
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_PERFORMANCE_SQL,
                {
                    "id": new_id,
                    "obligation_id": obligation_id,
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                    "compliance_pct": compliance_pct,
                    "passed_threshold": passed_threshold,
                },
            )
        return new_id

    async def fetch_active_invoice_line(
        self,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        line_type: str,
    ) -> ExistingInvoiceLineRow | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_ACTIVE_INVOICE_LINE_SQL,
                {
                    "contract_id": contract_id,
                    "obligation_id": obligation_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "line_type": line_type,
                },
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ExistingInvoiceLineRow(
            invoice_line_id=row["invoice_line_id"], amount=row["amount"], version=row["version"]
        )

    async def insert_invoice_line(
        self,
        *,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        draft: InvoiceLineDraft,
        status: Literal["PROVISIONAL", "FINAL", "CORRECTED"],
        version: int,
        supersedes: UUID | None,
    ) -> UUID:
        new_id = uuid4()
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_INVOICE_LINE_SQL,
                {
                    "id": new_id,
                    "contract_id": contract_id,
                    "obligation_id": obligation_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "line_type": draft.line_type,
                    "quantity": draft.quantity,
                    "unit": draft.unit,
                    "rate": draft.rate,
                    "amount": draft.amount,
                    "status": status,
                    "supersedes": supersedes,
                    "version": version,
                },
            )
        return new_id

    async def fetch_active_pnl(self, obligation_id: UUID, interval_start: datetime) -> ExistingPnl | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_ACTIVE_PNL_SQL, {"obligation_id": obligation_id, "interval_start": interval_start}
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return ExistingPnl(pnl_id=row["pnl_id"], net_value=row["net_value"], version=row["version"])

    async def insert_pnl(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        revenue: Decimal,
        energy_cost: Decimal,
        degradation_cost: Decimal,
        penalty: Decimal,
        net_value: Decimal,
        rule_baseline_value: Decimal | None,
        forgone_upside: Decimal,
        version: int,
        supersedes: UUID | None = None,
    ) -> UUID:
        new_id = uuid4()
        async with self.pool.connection() as conn, conn.cursor() as cur:
            # Retire-then-insert, atomically -- see `insert_meter_interval`'s docstring/comment.
            if supersedes is not None:
                await cur.execute(_SUPERSEDE_PNL_SQL, {"old_id": supersedes, "new_id": new_id})
            await cur.execute(
                _INSERT_PNL_SQL,
                {
                    "id": new_id,
                    "obligation_id": obligation_id,
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                    "revenue": revenue,
                    "energy_cost": energy_cost,
                    "degradation_cost": degradation_cost,
                    "penalty": penalty,
                    "net_value": net_value,
                    "rule_baseline_value": rule_baseline_value,
                    "forgone_upside": forgone_upside,
                    "version": version,
                },
            )
        return new_id

    async def fetch_rule_baseline_delivered_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        # MVP-S: the rule-baseline shadow allocator (ES05-S07) is not wired up yet; until it
        # publishes its own shadow-grant table, settle reports no LP-vs-rule-baseline comparison for
        # this interval rather than fabricate one (BUILD.md S5a: "no silent fallbacks").
        return None

    async def fetch_best_competing_value_per_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        # MVP-S: populated once the selector's "relaxed-commitment" shadow re-solve (02a S7.4) lands;
        # until then settle reports forgone_upside = 0 rather than guess (no silent fallback).
        return None

    async def fetch_invoice_lines_for_period(
        self, contract_id: UUID, period_start: date, period_end: date
    ) -> list[InvoiceLineExportRow]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_INVOICE_LINES_FOR_PERIOD_SQL,
                {"contract_id": contract_id, "period_start": period_start, "period_end": period_end},
            )
            rows = await cur.fetchall()
        return [InvoiceLineExportRow(**r) for r in rows]

    async def fetch_meter_intervals_for_period(
        self, obligation_id: UUID, period_start: datetime, period_end: datetime
    ) -> list[MeterIntervalExportRow]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_METER_INTERVALS_FOR_PERIOD_SQL,
                {"obligation_id": obligation_id, "period_start": period_start, "period_end": period_end},
            )
            rows = await cur.fetchall()
        return [MeterIntervalExportRow(**r) for r in rows]

    async def fetch_settleable_obligations(self) -> list[UUID]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FETCH_SETTLEABLE_SQL)
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def fetch_pending_intervals(self) -> list[tuple[UUID, datetime, datetime]]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_PENDING_INTERVALS_SQL)
            rows = await cur.fetchall()
        return [(r["obligation_id"], r["interval_start"], r["interval_end"]) for r in rows]
