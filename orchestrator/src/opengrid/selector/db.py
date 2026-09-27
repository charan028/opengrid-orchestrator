"""Selector's own read-only queries against `og.commitment`/`og.opportunity`/`og.obligation` (02a S2.2,
S3.8). Selector never writes these tables (`ledger`/`contracts` do); it only reads them to assemble a
gate's `ModelInputs`, exactly as `load_frozen_commitments`'s fixed interface already does for
`commitment`. It writes only its own plan-analytics tables (migration 0030: `og.plan_value`,
`og.plan_shadow_obligation`, `og.plan_energy_value`). No migrations exist yet for these tables (architect-owned, BUILD.md S4) -- this module is
correct against the DDL in `02a-mvp-s-spec-engine.md` S1 and will run once they land; until then it is
exercised in tests only through `_get_pool`, which callers/tests may monkeypatch.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.core import geo
from opengrid.core.solar_share import (
    ERCOT_SOLAR_ACTUAL_SERIES,
    ERCOT_SOLAR_FORECAST_SERIES,
    ERCOT_SOLAR_PRODUCT,
    ERCOT_SYSTEM_LOAD_PRODUCT,
    ERCOT_SYSTEM_LOAD_SERIES,
    ercot_solar_share_of_load,
)
from opengrid.core.timeutil import to_utc
from opengrid.health.queries import fetch_degraded_modes
from opengrid.market.availability import BANK_AVAILABILITY_SQL, GRANDFATHERED_SQL
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
_UNFIT_PRICE_SERIES_SQL = """
    SELECT DISTINCT series_key FROM og.forecast
    WHERE kind = 'price' AND firm_fitness = 'NOT_FOR_FIRM'
      AND interval_start_utc >= %(horizon_start)s AND interval_start_utc < %(horizon_end)s
"""
_BANK_ZONES_SQL = "SELECT bank_id, zone FROM og.bank WHERE bank_id = ANY(%(ids)s)"

# The terms of committed obligations the selector needs beyond their frozen kW: service type and product
# duration (capacity holds lock energy instead of draining it), own value (forgone upside), and the
# contract's market (K15 territory). `to_jsonb(c) ->> 'market'` reads migration 0025's columns without
# failing on a database where 0025 is not applied yet (NULL -> FREE, `market.territory.market_of`).
_OBLIGATION_TERMS_SQL = """
    SELECT o.obligation_id, o.service_type, op.value_per_mwh,
           to_jsonb(c) ->> 'market' AS market, to_jsonb(c) ->> 'utility_id' AS utility_id,
           (SELECT MAX(pr.duration_minutes) FROM og.product_rule pr
            WHERE pr.contract_id = o.contract_id) AS duration_minutes
    FROM og.obligation o
    JOIN og.contract c ON c.contract_id = o.contract_id
    LEFT JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
    WHERE o.obligation_id = ANY(%(ids)s::uuid[])
"""

_OFFERED_OPPORTUNITIES_SQL = """
    SELECT o.opportunity_id, ob.obligation_id, o.contract_id, o.window_start, o.window_end,
           o.requested_kw, o.value_per_mwh, c.service_type, c.tier, c.degradation_cost,
           to_jsonb(c) ->> 'market' AS market, to_jsonb(c) ->> 'utility_id' AS utility_id,
           pr.variable_kind, pr.min_qty_kw, pr.increment_kw,
           COALESCE(pr.duration_minutes, (SELECT MAX(p2.duration_minutes) FROM og.product_rule p2
                                          WHERE p2.contract_id = o.contract_id)) AS duration_minutes
    FROM og.opportunity o
    JOIN og.contract c ON c.contract_id = o.contract_id
    JOIN og.obligation ob ON ob.opportunity_id = o.opportunity_id
    LEFT JOIN og.product_rule pr ON pr.product_rule_id = o.product_rule_id
    WHERE o.state = 'OFFERED' AND ob.state = 'OFFERED'
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
    """UTC ISO key for an interval start. Postgres hands `timestamptz` back in the session zone, and
    `gate.load_committed` matches keys against UTC horizon intervals, so the zone must be normalized."""
    if isinstance(interval_start, datetime):
        return to_utc(interval_start).isoformat()
    return str(interval_start)


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


async def load_obligation_terms(obligation_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read-only: `obligation_id -> {service_type, value_per_mwh, market, utility_id, duration_minutes}`
    for the given obligations (see `_OBLIGATION_TERMS_SQL`)."""
    if not obligation_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_OBLIGATION_TERMS_SQL, {"ids": obligation_ids})
        return {str(row["obligation_id"]): dict(row) async for row in cur}


async def load_bank_zones(bank_ids: list[str]) -> dict[str, str]:
    """Read-only: `bank_id -> og.bank.zone` (its ERCOT load zone), so each bank is priced at its own
    zone's forecast path."""
    if not bank_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_BANK_ZONES_SQL, {"ids": bank_ids})
        return {str(row[0]): str(row[1]) async for row in cur}


async def load_bank_availability() -> list[tuple[str, str | None, str | None, Any]]:
    """Read-only (D-37, migration 0046): `(bank_id, availability, availability_reason, availability_since)`
    per bank, `opengrid.market.availability.BANK_AVAILABILITY_SQL`."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(BANK_AVAILABILITY_SQL)
        return [(str(r[0]), r[1], r[2], r[3]) async for r in cur]


async def load_grandfathered_pairs() -> list[tuple[str, str]]:
    """Read-only (D-37, K13): `(obligation_id, bank_id)` pairs grandfathered on an unavailable bank,
    `opengrid.market.availability.GRANDFATHERED_SQL` (the one rule)."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(GRANDFATHERED_SQL)
        return [(str(r[0]), str(r[1])) async for r in cur]


#: A mobile unit is a single-hub bank; its hub's recorded position (device-reported, `og.hub.lat/lon`).
#: Since H4 that is `og.hub.device_lat/device_lon` (a report writes only those; `og.hub.lat/lon` stays the
#: seeded home station), read through `core.geo.DEVICE_POSITIONS_SQL` and accepted only while fresh.
async def load_hub_positions(
    unit_ids: list[str], now: datetime | None = None, *, telemetry_max_age_s: float | None = None
) -> dict[str, tuple[float, float]]:
    """Read-only: `id -> (lat, lon)` of the trusted device-reported position of the hubs whose bank id or hub
    id is in `unit_ids`, keyed by both (D-31 mobile units: the selector's at-home test). The one query and
    freshness rule G-35 uses (`core.geo.DEVICE_POSITIONS_SQL` / `fresh_positions`, with the stationary rule
    when `telemetry_max_age_s` is given); a hub with no trusted position is absent (unknown: away)."""
    if not unit_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(geo.DEVICE_POSITIONS_SQL, {"ids": unit_ids})
        rows = await cur.fetchall()
    return geo.fresh_positions(rows, now or datetime.now(UTC), telemetry_max_age_s=telemetry_max_age_s)


async def load_degraded_modes() -> frozenset[str]:
    """Read-only: the currently-active degraded modes (`og.degraded_mode_state`, health's persisted
    set, read through health's own query). Raises on any DB error: callers fail closed."""
    return frozenset(mode for mode, _since in await fetch_degraded_modes(await get_pool()))


async def load_unfit_price_series(horizon_start: datetime, horizon_end: datetime) -> frozenset[str]:
    """Read-only: price series (`og.forecast.series_key`, a load zone) flagged `NOT_FOR_FIRM` anywhere in
    the horizon -- forecast marks a series so when its live feed is STALE or its history too short
    (02b S3). `forecast.scenarios` does not carry the flag, so it is read from the table directly."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UNFIT_PRICE_SERIES_SQL, {"horizon_start": horizon_start, "horizon_end": horizon_end}
        )
        return frozenset([str(row[0]) async for row in cur])


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


# --- selector-owned plan analytics (migration 0030) -------------------------------------------------

_INSERT_PLAN_VALUE_SQL = """
    INSERT INTO og.plan_value (plan_id, lp_net_value, rule_net_value, value_added, forgone_upside,
                               stage_r_objective, breakdown)
    VALUES (%(plan_id)s, %(lp_net_value)s, %(rule_net_value)s, %(value_added)s, %(forgone_upside)s,
            %(stage_r_objective)s, %(breakdown)s)
    ON CONFLICT (plan_id) DO NOTHING
"""

_INSERT_SHADOW_OBLIGATION_SQL = """
    INSERT INTO og.plan_shadow_obligation (plan_id, obligation_id, interval_start, interval_end, lp_kw,
                                           rule_kw, best_competing_value_per_kwh)
    VALUES (%(plan_id)s, %(obligation_id)s, %(interval_start)s, %(interval_end)s, %(lp_kw)s, %(rule_kw)s,
            %(best_competing_value_per_kwh)s)
    ON CONFLICT (plan_id, obligation_id, interval_start) DO NOTHING
"""

_INSERT_ENERGY_VALUE_SQL = """
    INSERT INTO og.plan_energy_value (plan_id, bank_id, horizon_start, horizon_end, interval_minutes,
                                      water_value_usd_per_mwh, discharge_threshold_usd_per_mwh,
                                      planned_floor_kwh, hold_floor_kwh, solar_share, solar_share_source)
    VALUES (%(plan_id)s, %(bank_id)s, %(horizon_start)s, %(horizon_end)s, %(interval_minutes)s,
            %(water_value_usd_per_mwh)s, %(discharge_threshold_usd_per_mwh)s, %(planned_floor_kwh)s,
            %(hold_floor_kwh)s, %(solar_share)s, %(solar_share_source)s)
    ON CONFLICT (plan_id, bank_id) DO NOTHING
"""

# As `_ENERGY_VALUE_THRESHOLDS_SQL`, for the hard hold floor.
_HOLD_FLOORS_SQL = """
    SELECT DISTINCT ON (bank_id) bank_id,
           hold_floor_kwh[
               1 + floor(extract(epoch FROM (%(at)s::timestamptz - horizon_start)) / (60 * interval_minutes))::int
           ] AS hold_floor
    FROM og.plan_energy_value
    WHERE bank_id = ANY(%(ids)s) AND horizon_start <= %(at)s AND horizon_end > %(at)s
    ORDER BY bank_id, created_at DESC
"""

#: D-28 source 2 for the PLAN: ERCOT system solar (ids in `core.solar_share`) and system load, per
#: America/Chicago hour: the STPPF `solar_forecast` over the next 24 h when published, else the trailing
#: week's `solar_actual`; load is the trailing week's same-hour mean (NP6-345-CD has actuals only). The
#: share itself is `core.solar_share.ercot_solar_share_of_load`. An absent feed yields no rows.
_ERCOT_SOLAR_SHARE_SQL = """
    WITH load AS (
        SELECT extract(hour FROM ts AT TIME ZONE 'America/Chicago')::int AS hour, avg(value) AS mw
        FROM og.feed_obs
        WHERE source = 'ERCOT' AND product = %(load_product)s AND series = %(load_series)s
          AND ts > now() - interval '7 days'
        GROUP BY 1
    ), solar AS (
        SELECT extract(hour FROM ts AT TIME ZONE 'America/Chicago')::int AS hour,
               avg(value) FILTER (WHERE series = %(actual)s AND ts <= now()) AS actual_mw,
               avg(value) FILTER (WHERE series = %(forecast)s AND ts > now()) AS forecast_mw
        FROM og.feed_obs
        WHERE source = 'ERCOT' AND product = %(product)s AND series IN (%(actual)s, %(forecast)s)
          AND ts > now() - interval '7 days' AND ts <= now() + interval '24 hours'
        GROUP BY 1
    )
    SELECT s.hour, coalesce(s.forecast_mw, s.actual_mw) AS solar_mw, l.mw AS load_mw
    FROM solar s JOIN load l USING (hour)
"""

#: Retention of `og.plan_energy_value` (about 40 rows per gate): only the newest plan is ever read.
ENERGY_VALUE_RETENTION_DAYS = 7
_PRUNE_ENERGY_VALUE_SQL = """
    DELETE FROM og.plan_energy_value WHERE created_at < now() - make_interval(days => %(days)s)
"""

# The newest plan's series covering `at`, per bank: the index into the arrays is computed in SQL so the
# dispatcher gets one number per bank (NULL outside the array or for an interval without a dual).
_ENERGY_VALUE_THRESHOLDS_SQL = """
    SELECT DISTINCT ON (bank_id) bank_id,
           discharge_threshold_usd_per_mwh[
               1 + floor(extract(epoch FROM (%(at)s::timestamptz - horizon_start)) / (60 * interval_minutes))::int
           ] AS threshold
    FROM og.plan_energy_value
    WHERE bank_id = ANY(%(ids)s) AND horizon_start <= %(at)s AND horizon_end > %(at)s
    ORDER BY bank_id, created_at DESC
"""


async def insert_plan_analytics(
    value_row: dict[str, Any],
    shadow_rows: list[dict[str, Any]],
    energy_rows: list[dict[str, Any]],
) -> None:
    """Write one plan's analytics (og.plan_value, og.plan_shadow_obligation, og.plan_energy_value) in one
    transaction. Selector-owned tables; idempotent per plan."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(_INSERT_PLAN_VALUE_SQL, value_row)
        if shadow_rows:
            await cur.executemany(_INSERT_SHADOW_OBLIGATION_SQL, shadow_rows)
        if energy_rows:
            await cur.executemany(_INSERT_ENERGY_VALUE_SQL, energy_rows)


async def load_energy_value_thresholds(bank_ids: list[str], at: datetime) -> dict[str, float]:
    """DISPATCH read API (DB): `bank_id -> headroom-discharge break-even price ($/MWh)` at `at`, from the
    newest plan covering `at` (`og.plan_energy_value`). Banks without a value are absent: keep the
    fallback threshold for them. Same figure as `selector.energy_value.discharge_threshold_usd_per_mwh`."""
    if not bank_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ENERGY_VALUE_THRESHOLDS_SQL, {"ids": bank_ids, "at": at})
        return {str(row[0]): float(row[1]) async for row in cur if row[1] is not None}


async def load_hold_floors_kwh(bank_ids: list[str], at: datetime) -> dict[str, float]:
    """DISPATCH/guardian read API (DB): `bank_id -> hard SoC floor (kWh)` at `at` from the newest plan
    covering `at`. Same figure as `selector.energy_value.hold_floor_kwh`."""
    if not bank_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_HOLD_FLOORS_SQL, {"ids": bank_ids, "at": at})
        return {str(row[0]): float(row[1]) async for row in cur if row[1] is not None}


#: D-30: the owner's grid-charging windows, edited from the Fleet page (FOLLOWUPS' migration 0038).
_OWNER_CHARGE_WINDOWS_SQL = "SELECT scope_kind, scope_ref, windows FROM og.owner_charge_window"


async def load_owner_charge_window_rows() -> dict[tuple[str, str], list[str]]:
    """Read-only: `(scope_kind, scope_ref) -> windows` (`"22:00-06:00"` strings, America/Chicago) from
    `og.owner_charge_window` (scope kinds FLEET, PROVIDER, ZONE, SUBSTATION, FEEDER, BANK, HUB). Raises
    when the table is absent (migration 0038 not applied): the caller falls back to config."""
    rows: dict[tuple[str, str], list[str]] = {}
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_OWNER_CHARGE_WINDOWS_SQL)
        async for scope_kind, scope_ref, windows in cur:
            rows[str(scope_kind), str(scope_ref)] = [str(w) for w in (windows or [])]
    return rows


async def load_ercot_solar_share_by_hour() -> dict[int, float]:
    """D-28 source 2: `local hour -> ERCOT solar share of system load` (trailing week); empty when the
    feed is absent. Callers treat an error as no ERCOT source."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _ERCOT_SOLAR_SHARE_SQL,
            {
                "product": ERCOT_SOLAR_PRODUCT,
                "actual": ERCOT_SOLAR_ACTUAL_SERIES,
                "forecast": ERCOT_SOLAR_FORECAST_SERIES,
                "load_product": ERCOT_SYSTEM_LOAD_PRODUCT,
                "load_series": ERCOT_SYSTEM_LOAD_SERIES,
            },
        )
        rows = [row async for row in cur]
    return ercot_shares_from_rows(rows)


def ercot_shares_from_rows(rows: list[tuple[Any, Any, Any]]) -> dict[int, float]:
    """`(hour, solar_mw, load_mw)` rows -> `hour -> share`, by the one formula in core; an hour without
    a measurable share (missing value, no positive load) is left out."""
    shares: dict[int, float] = {}
    for hour, solar_mw, load_mw in rows:
        if solar_mw is None or load_mw is None:
            continue
        share = ercot_solar_share_of_load(Decimal(str(solar_mw)), Decimal(str(load_mw)))
        if share is not None:
            shares[int(hour)] = float(share)
    return shares


async def prune_plan_energy_value(keep_days: int = ENERGY_VALUE_RETENTION_DAYS) -> int:
    """Delete `og.plan_energy_value` rows older than `keep_days`; returns how many."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_PRUNE_ENERGY_VALUE_SQL, {"days": keep_days})
        return int(cur.rowcount or 0)
