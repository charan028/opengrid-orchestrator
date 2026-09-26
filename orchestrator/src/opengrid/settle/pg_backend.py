"""Postgres implementation of `SettleBackend` (02a S7), using `opengrid.platform.db`'s async pool.

Kept separate from `opengrid.settle.backend` so the Protocol + pure orchestration code has no
`psycopg` import (BUILD.md S5a: "pure logic separated from I/O"), mirroring
`opengrid.trace.store`/`opengrid.trace.pg_backend`'s split.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.market import Utility, UtilityId
from opengrid.core.reasons import ALR_ENERGY_SHORTFALL_RISK
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
    ZoneChargeEnergy,
)

_FETCH_CONTEXT_SQL = """
SELECT
    o.obligation_id, o.contract_id, o.service_type, o.committed_qty_kw,
    c.penalty_alpha, c.penalty_beta, c.penalty_theta, c.degradation_cost, c.customer_id,
    c.market, c.utility_id,
    opp.value_per_mwh,
    COALESCE(
        (SELECT sp.setpoint_source FROM og.service_profile sp
         WHERE sp.contract_id = o.contract_id
         ORDER BY sp.version DESC LIMIT 1) = 'MEASURED_FEEDBACK',
        false
    ) AS is_need_basis,
    -- The reservation's bank_id names either an og.bank (HOME_BANK) or an og.asset (SUBSTATION, 09
    -- D11/migration 0025) -- a substation asset has no og.bank row of its own, so this falls back to
    -- og.asset's zone when og.bank has no match.
    (SELECT z FROM (
        SELECT r.amount, COALESCE(b.zone, a.zone) AS z
        FROM og.reservation r
        LEFT JOIN og.bank b ON b.bank_id = r.bank_id::text
        LEFT JOIN og.asset a ON a.asset_id = r.bank_id::text
        WHERE r.obligation_id = o.obligation_id
     ) sub
     GROUP BY z
     ORDER BY sum(amount) DESC, z
     LIMIT 1) AS zone
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
JOIN og.opportunity opp ON opp.opportunity_id = o.opportunity_id
WHERE o.obligation_id = %(obligation_id)s
"""

_FETCH_CUSTOMER_ID_SQL = """
SELECT c.customer_id FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id
WHERE o.obligation_id = %(obligation_id)s
"""

#: D-18 need-basis settlement: the customer's measured site demand for the interval (migration 0026's
#: `og.customer_site_meter_reading`), summed across the customer's site(s) per timestamp (a customer
#: with more than one DATA_CENTER site would otherwise undercount its true need) then averaged over
#: the interval. `customer_site_meter_reading.customer_id` is text (an external site-ingest key), so
#: this joins against `og.contract.customer_id::text` -- MVP-S has no per-obligation site_id column to
#: join on more precisely (a documented gap, same class as `opengrid.invariants.checks.classify_dip`'s
#: own deferred-scope note).
_FETCH_MEASURED_NEED_SQL = """
WITH per_ts AS (
    SELECT ts, sum(p_kw) AS site_kw
    FROM og.customer_site_meter_reading
    WHERE customer_id = %(customer_id)s
      AND ts >= %(interval_start)s AND ts < %(interval_end)s
      AND quality = 'GOOD'
    GROUP BY ts
)
SELECT avg(site_kw) AS avg_kw, count(*) AS sample_count FROM per_ts
"""

#: The obligation's load zone: banks overlapping the interval first, then the most reserved kW.
#: Shared by both the (informational) discharge-interval SPP lookup and the charging-cost proxy, so
#: the two queries can never disagree about which zone an obligation belongs to.
_ZONE_FOR_OBLIGATION_CTE = """
    SELECT b.zone
    FROM og.reservation r JOIN og.bank b ON b.bank_id = r.bank_id::text
    WHERE r.obligation_id = %(obligation_id)s
    GROUP BY b.zone
    ORDER BY bool_or(r.interval_start < %(interval_start)s + interval '15 minutes'
                     AND r.interval_end > %(interval_start)s) DESC,
             sum(r.amount) DESC, b.zone
    LIMIT 1
"""

#: The obligation's zone (see `_ZONE_FOR_OBLIGATION_CTE`) and that zone's ERCOT real-time SPP
#: (NP6-905-CD, `ts` = the 15-minute interval start) for the interval, else the nearest earlier value
#: within 1 h. Informational only (`ObligationSettlementContext.wholesale_price_per_kwh`) -- it no
#: longer prices `energy_cost` (see `_FETCH_CHARGING_COST_PROXY_SQL`).
_FETCH_WHOLESALE_SPP_SQL = f"""
WITH zone AS ({_ZONE_FOR_OBLIGATION_CTE})
SELECT z.zone, f.ts, f.value
FROM zone z
LEFT JOIN LATERAL (
    SELECT ts, value FROM og.feed_obs
    WHERE source = 'ERCOT' AND product = 'np6-905-cd' AND series = z.zone
      AND ts <= %(interval_start)s AND ts > %(interval_start)s - interval '1 hour'
    ORDER BY ts DESC, recorded_at DESC
    LIMIT 1
) f ON true
"""  # noqa: S608 -- _ZONE_FOR_OBLIGATION_CTE is a fixed module-level literal, never interpolated input

#: Documented proxy for "what was paid to charge" (09-optimizer-dispatcher-update.md S0.2 finding G4,
#: settle/profitability.py's module docstring): the trailing 24h off-peak average ERCOT real-time SPP
#: for the obligation's bank zone. MVP-S has no per-obligation charging-interval attribution yet (the
#: allocator does not record which cycles charged which obligation's energy), so this average is the
#: best available stand-in for "the price paid when this energy was actually stored" until that
#: attribution exists -- an assumption, not a measurement, hence "PROXY" in its flag.
#:
#: Off-peak hours (22:00-06:59 America/Chicago, `_OFF_PEAK_HOURS_LOCAL`) approximate typical ERCOT
#: overnight charging windows; not a specific TOU tariff's own definition.
_FETCH_CHARGING_COST_PROXY_SQL = f"""
WITH zone AS ({_ZONE_FOR_OBLIGATION_CTE})
SELECT z.zone, avg(f.value) AS avg_value, count(f.value) AS sample_count
FROM zone z
LEFT JOIN og.feed_obs f
    ON f.source = 'ERCOT' AND f.product = 'np6-905-cd' AND f.series = z.zone
   AND f.ts >= %(interval_start)s - interval '24 hours' AND f.ts < %(interval_start)s
   AND extract(hour FROM f.ts AT TIME ZONE 'America/Chicago')::int = ANY(%(off_peak_hours_local)s)
GROUP BY z.zone
"""  # noqa: S608 -- _ZONE_FOR_OBLIGATION_CTE is a fixed module-level literal, never interpolated input

#: One 1-minute sample per minute of the interval: the kW the obligation actually received. Per bank the
#: hubs' measured discharge (-sum of p_kw, 2 s telemetry averaged per hub per minute; charging counts as
#: 0) is attributed to this obligation by its share of the bank's granted kW that minute (other
#: obligations and headroom exports share the same hubs), then summed over its banks. A minute with no
#: grant for the obligation attributes 0. Replaces a query that averaged raw per-hub 2 s samples as if
#: they were the obligation's 1-minute kW (signed, per hub): every obligation metered ~0 kWh (live
#: 2026-09-26: ERCOT_AS delivering 500 kW metered -0.08 kWh per 15 min).
_FETCH_TELEMETRY_SQL = """
WITH banks AS (
    SELECT DISTINCT bank_id FROM og.reservation
    WHERE obligation_id = %(obligation_id)s
      AND interval_start < %(interval_end)s AND interval_end > %(interval_start)s
),
tel AS (
    SELECT date_trunc('minute', t.ts) AS m, h.bank_id, t.hub_id, avg(t.p_kw) AS p
    FROM og.telemetry t JOIN og.hub h USING (hub_id)
    WHERE h.bank_id IN (SELECT bank_id FROM banks)
      AND t.ts >= %(interval_start)s AND t.ts < %(interval_end)s
    GROUP BY 1, 2, 3
),
bank_kw AS (SELECT m, bank_id, greatest(-sum(p), 0) AS discharge_kw FROM tel GROUP BY 1, 2),
cyc AS (
    SELECT date_trunc('minute', created_at) AS m, cycle_id, bank_id,
           coalesce(sum(granted_kw) FILTER (WHERE obligation_id = %(obligation_id)s), 0) AS ob_kw,
           sum(granted_kw) AS tot_kw
    FROM og.grant
    WHERE bank_id IN (SELECT bank_id FROM banks)
      AND created_at >= %(interval_start)s AND created_at < %(interval_end)s
    GROUP BY 1, 2, 3
),
share AS (SELECT m, bank_id, avg(ob_kw) AS ob_kw, avg(tot_kw) AS tot_kw FROM cyc GROUP BY 1, 2)
SELECT b.m AS ts,
       sum(b.discharge_kw * CASE WHEN s.tot_kw > 0 THEN s.ob_kw / s.tot_kw ELSE 0 END) AS kw
FROM bank_kw b LEFT JOIN share s USING (m, bank_id)
GROUP BY b.m
ORDER BY b.m
"""

#: 09 D5's M1 delivery charge, fleet-level proxy per load zone (review fix 2026-09-26: the previous
#: per-obligation query only counted charging DURING the obligation's own delivery interval, when its banks
#: are discharging, so M1 settled at ~$0). Every charging sample of every hub in the zone over the window
#: (`p_kw > 0` is charging -- `_FETCH_TELEMETRY_SQL` meters discharge as `-p_kw`): the charging power, and
#: the part of it the grid supplied, i.e. beyond the home's PV surplus (`pv_kw - home_load_kw`, migration
#: 0027; NULL PV = no PV, NULL home load = none, so all PV counts as surplus). Summed over the same samples,
#: so `ZoneChargeEnergy.grid_share` is cadence-independent.
_FETCH_ZONE_CHARGE_SQL = """
SELECT
    coalesce(sum(t.p_kw), 0) AS charge_kw_sum,
    coalesce(sum(greatest(
        t.p_kw - greatest(coalesce(t.pv_kw, 0) - greatest(coalesce(t.home_load_kw, 0), 0), 0), 0
    )), 0) AS grid_charge_kw_sum
FROM og.telemetry t JOIN og.hub h USING (hub_id)
WHERE h.zone = %(zone)s AND t.ts >= %(window_start)s AND t.ts < %(window_end)s AND t.p_kw > 0
"""

#: ALR-ENERGY-SHORTFALL-RISK open for the obligation at any time in the interval. The rule's detail
#: carries `obligation_id` (`opengrid.health.rules.evaluate_energy_shortfall_risk_alert`), mirrored into
#: `scope_ref` since migration 0031.
_FETCH_SHORTFALL_RISK_OPEN_SQL = """
SELECT EXISTS (
    SELECT 1 FROM og.alert
    WHERE rule = %(rule)s
      AND coalesce(scope_ref, detail ->> 'obligation_id') = %(obligation_id)s
      AND opened_at < %(interval_end)s
      AND (cleared_at IS NULL OR cleared_at > %(interval_start)s)
)
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
     penalty, delivery_charge, net_value, rule_baseline_value, forgone_upside, version)
VALUES (%(id)s, %(obligation_id)s, %(interval_start)s, %(interval_end)s, %(revenue)s, %(energy_cost)s,
        %(degradation_cost)s, %(penalty)s, %(delivery_charge)s, %(net_value)s, %(rule_baseline_value)s,
        %(forgone_upside)s, %(version)s)
"""

#: `og.utility` (migration 0025), field names matching `opengrid.core.models.market.Utility` exactly.
_FETCH_UTILITY_SQL = """
SELECT utility_id, name, territory_zones, capacity_product, payment_basis, capacity_price_usd_per_kw,
       charging_tariff_kind, off_peak_rate_usd_per_kwh, mid_peak_rate_usd_per_kwh,
       on_peak_rate_usd_per_kwh, charging_adder_usd_per_kwh, solar_cost_usd_per_kwh,
       solar_share_floor, free_access_granted, tariff_ref, source_note
FROM og.utility
WHERE utility_id = %(utility_id)s
"""


_logger = logging.getLogger(__name__)
_ZONE_CHARGE_CACHE_MAX = 256  # (zone, window) entries; cleared wholesale when full
_ZERO = Decimal("0")
_KWH_PER_MWH = Decimal("1000")

#: 22:00-06:59 America/Chicago -- see `_FETCH_CHARGING_COST_PROXY_SQL`'s docstring.
_OFF_PEAK_HOURS_LOCAL: tuple[int, ...] = (22, 23, 0, 1, 2, 3, 4, 5, 6)


def wholesale_from_spp(spp: Mapping[str, Any] | None, interval_start: datetime | None) -> tuple[Decimal, str]:
    """`(wholesale $/kWh, flag)` from a `_FETCH_WHOLESALE_SPP_SQL` row: "SPP" when the price is for
    `interval_start` itself, "SPP_PRIOR" for an earlier value within 1 h, "MISSING" (0 $/kWh) when
    there is none -- no zone, no interval, or no SPP in the last hour. Informational only -- see
    `charging_cost_from_proxy` for what actually prices `energy_cost`."""
    if spp is None or interval_start is None or spp.get("value") is None:
        return _ZERO, "MISSING"
    price = Decimal(str(spp["value"])) / _KWH_PER_MWH
    return price, "SPP" if spp["ts"] == interval_start else "SPP_PRIOR"


def charging_cost_from_proxy(row: Mapping[str, Any] | None) -> tuple[Decimal, str]:
    """`(charging cost $/kWh, flag)` from a `_FETCH_CHARGING_COST_PROXY_SQL` row:
    "TRAILING_24H_OFFPEAK_PROXY" when at least one off-peak SPP observation exists in the trailing
    24h for the obligation's zone, "MISSING" (0 $/kWh, logged) when there is none -- no zone, or no
    off-peak SPP observation in that window.

    There is deliberately no "OBLIGATION_CHARGE" branch here yet: that requires per-obligation
    charging-interval attribution `opengrid.settle` does not have (09 S0.2's "where known" clause) --
    when it exists, its caller resolves it BEFORE falling back to this proxy, rather than this
    function guessing at it."""
    if row is None or row.get("avg_value") is None:
        return _ZERO, "MISSING"
    price = Decimal(str(row["avg_value"])) / _KWH_PER_MWH
    return price, "TRAILING_24H_OFFPEAK_PROXY"


def measured_need_kwh_from_row(row: Mapping[str, Any] | None, duration_hours: Decimal) -> Decimal | None:
    """D-18: the customer's measured need (kWh) for the interval from a `_FETCH_MEASURED_NEED_SQL`
    row -- average site import kW over the interval x its duration -- or `None` when there is no
    `GOOD`-quality reading in the interval at all (`performance.is_need_basis_compliant` then treats
    the dip as compliant by default, per its own docstring)."""
    if row is None or row.get("avg_kw") is None:
        return None
    return Decimal(str(row["avg_kw"])) * duration_hours


@dataclass(frozen=True, slots=True)
class PgSettleBackend:
    """`SettleBackend` backed by Postgres. Assumes the caller (`settle.main`) provides one pool per
    process, per `opengrid.platform.db.make_pool`."""

    pool: AsyncConnectionPool
    _zone_charge_cache: dict[tuple[str, datetime, datetime], ZoneChargeEnergy] = field(
        default_factory=dict, compare=False, repr=False
    )

    async def fetch_context(
        self, obligation_id: UUID, interval_start: datetime | None = None
    ) -> ObligationSettlementContext:
        spp: dict[str, Any] | None = None
        charging_proxy: dict[str, Any] | None = None
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_CONTEXT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
            if row is not None and interval_start is not None:
                await cur.execute(
                    _FETCH_WHOLESALE_SPP_SQL,
                    {"obligation_id": obligation_id, "interval_start": interval_start},
                )
                spp = await cur.fetchone()
                await cur.execute(
                    _FETCH_CHARGING_COST_PROXY_SQL,
                    {
                        "obligation_id": obligation_id,
                        "interval_start": interval_start,
                        "off_peak_hours_local": list(_OFF_PEAK_HOURS_LOCAL),
                    },
                )
                charging_proxy = await cur.fetchone()
        if row is None:
            raise LookupError(f"obligation not found: {obligation_id}")
        if row["value_per_mwh"] is None and row["service_type"] != "HOME":
            _logger.warning(
                "obligation has no opportunity value_per_mwh: revenue settles as 0 (flagged)",
                extra={"obligation_id": str(obligation_id), "service_type": row["service_type"]},
            )

        penalty = None
        if row["penalty_alpha"] is not None and row["penalty_beta"] is not None:
            penalty = PenaltyParams(
                alpha=row["penalty_alpha"],
                beta=row["penalty_beta"],
                theta=row["penalty_theta"] or Decimal("0"),
            )
        price_per_kwh = (row["value_per_mwh"] or Decimal("0")) / Decimal("1000")
        # Informational only: the bank zone's real-time SPP AT the discharge interval (kept for the
        # settlement trace/audit trail). It no longer prices energy_cost -- see charging_cost below
        # (09-optimizer-dispatcher-update.md S0.2 finding G4).
        wholesale_price_per_kwh, wholesale_flag = wholesale_from_spp(spp, interval_start)
        if wholesale_flag != "SPP":
            _logger.info(
                "settle discharge-interval SPP (informational) is %s for this interval",
                wholesale_flag,
                extra={
                    "obligation_id": str(obligation_id),
                    "interval_start": interval_start.isoformat() if interval_start else None,
                    "zone": spp["zone"] if spp else None,
                },
            )
        # What was actually paid to CHARGE this energy -- the documented proxy until per-obligation
        # charging-interval attribution exists (module/profitability.py docstrings).
        charging_cost_per_kwh, charging_flag = charging_cost_from_proxy(charging_proxy)
        if charging_flag == "MISSING":
            _logger.warning(
                "settle charging-cost proxy has no off-peak SPP observation in the trailing 24h: "
                "energy cost settles as 0 (flagged)",
                extra={
                    "obligation_id": str(obligation_id),
                    "interval_start": interval_start.isoformat() if interval_start else None,
                    "zone": charging_proxy["zone"] if charging_proxy else None,
                },
            )
        today = datetime.now(UTC).date()  # MVP-S: daily billing period, UTC calendar date
        return ObligationSettlementContext(
            obligation_id=row["obligation_id"],
            contract_id=row["contract_id"],
            service_type=row["service_type"],
            committed_kw=row["committed_qty_kw"],
            price_per_kwh=price_per_kwh,
            charging_cost_per_kwh=charging_cost_per_kwh,
            eta_d=Decimal("0.9487"),
            degradation_cost_per_kwh=row["degradation_cost"],
            penalty=penalty,
            period_start=today,
            period_end=today,
            is_need_basis=bool(row["is_need_basis"]),
            charging_cost_flag=charging_flag,
            wholesale_price_per_kwh=wholesale_price_per_kwh,
            wholesale_price_flag=wholesale_flag,
            zone=row["zone"],
            market=row["market"],
            utility_id=row["utility_id"],
        )

    async def fetch_measured_need_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        duration_hours = Decimal(str((interval_end - interval_start).total_seconds())) / Decimal("3600")
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_CUSTOMER_ID_SQL, {"obligation_id": obligation_id})
            customer_row = await cur.fetchone()
            if customer_row is None:
                return None
            await cur.execute(
                _FETCH_MEASURED_NEED_SQL,
                {
                    "customer_id": str(customer_row["customer_id"]),
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                },
            )
            need_row = await cur.fetchone()
        return measured_need_kwh_from_row(need_row, duration_hours)

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
        # One attributed sample per minute (see `_FETCH_TELEMETRY_SQL`), not one per hub.
        return [PowerSample(hub_id="obligation", ts=r["ts"], kw=Decimal(str(r["kw"]))) for r in rows]

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
        delivery_charge: Decimal,
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
                    "delivery_charge": delivery_charge,
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

    async def fetch_zone_charge_energy(
        self, zone: str, window_start: datetime, window_end: datetime
    ) -> ZoneChargeEnergy:
        # Cached per (zone, window): settle asks for the same trailing window for every obligation of a
        # zone in a cycle, and the scan covers a whole day of that zone's charging samples.
        key = (zone, window_start, window_end)
        cached = self._zone_charge_cache.get(key)
        if cached is not None:
            return cached
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _FETCH_ZONE_CHARGE_SQL, {"zone": zone, "window_start": window_start, "window_end": window_end}
            )
            row = await cur.fetchone()
        energy = ZoneChargeEnergy(
            charge_kw_sum=Decimal(str(row["charge_kw_sum"])) if row else Decimal("0"),
            grid_charge_kw_sum=Decimal(str(row["grid_charge_kw_sum"])) if row else Decimal("0"),
        )
        if len(self._zone_charge_cache) >= _ZONE_CHARGE_CACHE_MAX:
            self._zone_charge_cache.clear()
        self._zone_charge_cache[key] = energy
        return energy

    async def fetch_shortfall_risk_open(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> bool:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _FETCH_SHORTFALL_RISK_OPEN_SQL,
                {
                    "rule": ALR_ENERGY_SHORTFALL_RISK,
                    "obligation_id": str(obligation_id),
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                },
            )
            row = await cur.fetchone()
        return bool(row[0]) if row else False

    async def fetch_best_competing_value_per_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        # MVP-S: populated once the selector's "relaxed-commitment" shadow re-solve (02a S7.4) lands;
        # until then settle reports forgone_upside = 0 rather than guess (no silent fallback).
        return None

    async def fetch_pjm_emergency_rate(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        # No live PJM emergency-hour declaration feed yet (services_extra.py's docstring, PJM stays
        # simulated) -- never guess an emergency hour happened; logged, not silent (BUILD.md S5a).
        _logger.info(
            "settle: no PJM emergency-hour declaration feed yet, treating interval as routine",
            extra={"obligation_id": str(obligation_id), "interval_start": interval_start.isoformat()},
        )
        return None

    async def fetch_utility(self, utility_id: UtilityId) -> Utility | None:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_UTILITY_SQL, {"utility_id": utility_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return Utility(**row)

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
