"""Read models for the operator-console map, ledger and funnel screens (Gitea #19), plus the $/kW
profitability read (09 S11.8).

Split like the rest of `api`: `ExtViewsProtocol` is the interface the #19 routers depend on (`PgExtViews`
in production, a fake in unit tests), and everything below "pure builders" is plain logic with no I/O
so the routers stay thin. `api/store.py` is untouched; these are new, additive reads of tables other
modules own (`og.hub`, `og.hub_state`, `og.grant`, `og.reservation`, `og.commitment`, `og.opportunity`,
`og.feed_obs`, `og.customer_site_meter_reading`, `og.trace`). Nothing here writes.

Shared logic is reused, never re-derived: territory eligibility is `opengrid.market` (K15), the
load-zone -> weather-zone correspondence is `opengrid.forecast.service`'s, and the per-kW economics
are `opengrid.market.pg_backend` + `opengrid.market.view`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol
from uuid import UUID

from fastapi import Depends, Request
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.api.deps import get_config
from opengrid.forecast.service import DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE
from opengrid.market import MarketModel, MarketModelError, load_market_model, market_of
from opengrid.market.economics import PeriodTotals
from opengrid.market.pg_backend import fetch_contract_totals
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

# =====================================================================================================
# Data files (config/grid)
# =====================================================================================================

#: `orchestrator/` (this file is `orchestrator/src/opengrid/api/views_ext.py`).
_ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GRID_DIR: Final = _ORCHESTRATOR_ROOT / "config" / "grid"
GRID_MAP_DATA_FILE: Final = "grid_map_data.json"
ZONE_CENTROIDS_FILE: Final = "zone_centroids.json"
CUSTOMER_SITES_FILE: Final = "customer_sites.json"


def grid_config_dir(cfg: Config) -> Path:
    """`[api.grid].config_dir` wins; else `grid/` next to the loaded `orchestrator.toml`; else the
    checkout's own `orchestrator/config/grid` (API unit tests run with no `OG_CONFIG`)."""
    override = cfg.get("api.grid.config_dir")
    if override:
        return Path(str(override))
    if cfg.source_path is not None:
        candidate = cfg.source_path.parent / "grid"
        if candidate.is_dir():
            return candidate
    return DEFAULT_GRID_DIR


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def as_utc(dt: datetime) -> datetime:
    """Query-string datetimes: an offset-less value is taken as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def tdsp_config_anchor(cfg: Config) -> Path:
    """What `opengrid.market.load_market_model(config_path=...)` resolves `tdsp_tariffs.toml` next to."""
    if cfg.source_path is not None:
        return cfg.source_path
    return _ORCHESTRATOR_ROOT / "config" / "orchestrator.toml"


# =====================================================================================================
# Row shapes returned by the views
# =====================================================================================================


@dataclass(frozen=True, slots=True)
class ContractMarketRow:
    """One distinct (service_type, market, utility_id) among ACTIVE contracts."""

    service_type: str
    market: str | None
    utility_id: str | None


@dataclass(frozen=True, slots=True)
class ReservationSlice:
    obligation_id: UUID
    bank_id: str
    amount_kw: float
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class CommitmentSlice:
    obligation_id: UUID
    service_type: str
    committed_kw: float
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class FunnelRow:
    """One grouped count of opportunities (or admission-time rejections, `opportunity_state=None`)."""

    bucket_start: datetime
    service_type: str
    product: str
    opportunity_state: str | None
    obligation_state: str | None
    reason_code: str | None
    n: int


@dataclass(frozen=True, slots=True)
class MmsFunnelRow:
    """One grouped count from the INTEGRATIONS agent's ERCOT MMS submissions table, when it exists."""

    bucket_start: datetime
    product: str
    status: str
    reason_code: str | None
    n: int


class ExtViewsProtocol(Protocol):
    """Reads for the #19 routers."""

    async def fleet_map_rows(self, *, grant_fresh_s: float) -> list[dict[str, Any]]:
        """One row per registered hub: `hub_id, bank_id, zone, lat, lon, e_kwh, r_kwh, rated_kw, soc_kwh,
        kw, health, last_seen_at, fault_code, home_load_kw, meter_kw, pv_kw, bank_has_grant,
        bank_granted_kw, serving_obligations` (a list of `{obligation_id, service_type, state, tier}`)."""
        ...

    async def active_contract_markets(self) -> list[ContractMarketRow]: ...

    async def critical_alert_scopes(self) -> set[tuple[str, str]]:
        """`(scope_kind, scope_ref)` of every open `critical` alert (lower-cased kind)."""
        ...

    async def customer_site_readings(self, *, max_age_s: float) -> list[dict[str, Any]]:
        """Latest `og.customer_site_meter_reading` per (customer_id, site_id) no older than `max_age_s`."""
        ...

    async def customer_contract_services(self) -> dict[str, list[str]]:
        """`customer_id -> [service_type, ...]` over ACTIVE contracts."""
        ...

    async def weather_zone_load_mw(self) -> dict[str, tuple[float, datetime]]:
        """Latest ERCOT NP6-345-CD actual load per weather-zone series: `series -> (mw, ts)`."""
        ...

    async def bank_scada_load_kw(self) -> dict[str, tuple[float, datetime]]:
        """Latest SCADA `REAL_POWER_KW` per bank (`og.feed_obs`, source `scada`): `bank_id -> (kw, ts)`."""
        ...

    async def ledger_slices(
        self, *, t0: datetime, t1: datetime
    ) -> tuple[list[ReservationSlice], list[CommitmentSlice]]: ...

    async def funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[FunnelRow]: ...

    async def mms_funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[MmsFunnelRow] | None:
        """`None` when the MMS submissions table does not exist (yet) or has an unrecognised shape."""
        ...

    async def contract_totals(self, *, start: datetime, end: datetime) -> list[PeriodTotals]: ...


# =====================================================================================================
# Postgres implementation
# =====================================================================================================

#: The optional hub_state columns migration 0027 adds; selected only when present.
_OPTIONAL_HUB_STATE_COLUMNS: Final = ("home_load_kw", "meter_kw", "pv_kw")

#: `og.grant.cycle_id` is `"<unix seconds>-<seq>"` (`opengrid.engine._engine_tick`), so the newest cycle is
#: `max(cycle_id)` over `ix_grant_cycle`, and its age is the numeric prefix.
_FLEET_MAP_SQL = """
WITH latest_cycle AS (
    SELECT max(cycle_id) AS cycle_id FROM og.grant
), active_grant AS (
    SELECT g.bank_id, g.obligation_id, g.granted_kw
    FROM og.grant g JOIN latest_cycle l ON g.cycle_id = l.cycle_id
    WHERE split_part(l.cycle_id, '-', 1) ~ '^[0-9]+$'
      AND split_part(l.cycle_id, '-', 1)::bigint >= extract(epoch FROM now())::bigint - {grant_fresh_s}
      AND g.granted_kw > 0
), bank_grant AS (
    SELECT bank_id, sum(granted_kw)::float8 AS granted_kw FROM active_grant GROUP BY bank_id
), serving AS (
    SELECT bank_id, obligation_id FROM active_grant WHERE obligation_id IS NOT NULL
    UNION
    SELECT r.bank_id, r.obligation_id FROM og.reservation r
    WHERE r.released_at IS NULL AND r.interval_start <= now() AND r.interval_end > now()
), bank_obligations AS (
    SELECT s.bank_id,
           jsonb_agg(DISTINCT jsonb_build_object(
               'obligation_id', o.obligation_id, 'service_type', o.service_type, 'state', o.state,
               'tier', o.tier)) AS obligations
    FROM serving s JOIN og.obligation o ON o.obligation_id = s.obligation_id
    WHERE o.state IN ('SELECTED', 'COMMITTED', 'DELIVERING')
    GROUP BY s.bank_id
)
SELECT h.hub_id, h.bank_id, h.zone, h.lat, h.lon, h.e_kwh, h.r_kwh, h.p_kw AS rated_kw,
       s.soc_kwh, s.p_kw AS kw, s.health, s.last_seen_at, s.fault_code, {optional_columns},
       (bg.bank_id IS NOT NULL) AS bank_has_grant, coalesce(bg.granted_kw, 0) AS bank_granted_kw,
       coalesce(bo.obligations, '[]'::jsonb) AS serving_obligations
FROM og.hub h
LEFT JOIN og.hub_state s ON s.hub_id = h.hub_id
LEFT JOIN bank_grant bg ON bg.bank_id = h.bank_id
LEFT JOIN bank_obligations bo ON bo.bank_id = h.bank_id
ORDER BY h.hub_id
"""

_CONTRACT_MARKETS_SQL = """
SELECT DISTINCT service_type, market, utility_id FROM og.contract WHERE status = 'ACTIVE'
"""

#: Migration 0031's structured columns when present, else the scope keys inside `detail` (the key names
#: differ per rule, see 0031's header), resolved by `alert_scopes`.
_CRITICAL_ALERTS_SQL = """
SELECT {scope_columns}, detail FROM og.alert WHERE cleared_at IS NULL AND lower(severity) = 'critical'
"""

_SITE_READINGS_SQL = """
SELECT DISTINCT ON (customer_id, site_id) customer_id, site_id, ts, p_kw, quality
FROM og.customer_site_meter_reading
WHERE ts >= now() - make_interval(secs => %(max_age_s)s)
ORDER BY customer_id, site_id, ts DESC
"""

_CUSTOMER_SERVICES_SQL = """
SELECT customer_id::text AS customer_id, array_agg(DISTINCT service_type ORDER BY service_type) AS services
FROM og.contract WHERE status = 'ACTIVE' GROUP BY customer_id
"""

#: `feeds.ercot` NP6-345-CD rows: one series per ERCOT weather zone (`feeds.normalize._LOAD_ZONE_COLUMNS`).
ERCOT_LOAD_PRODUCT: Final = "np6-345-cd"
_WEATHER_ZONE_LOAD_SQL = """
SELECT DISTINCT ON (series) series, ts, value
FROM og.feed_obs
WHERE source = 'ERCOT' AND product = %(product)s AND ts >= now() - interval '3 days'
ORDER BY series, ts DESC
"""

#: `opengrid.fleet.pg_backend` persists SCADA as `source='scada', product=<bank_id>, series=<signal>`.
_BANK_SCADA_LOAD_SQL = """
SELECT DISTINCT ON (product) product AS bank_id, ts, value
FROM og.feed_obs
WHERE source = 'scada' AND series = 'REAL_POWER_KW' AND ts >= now() - interval '1 hour'
ORDER BY product, ts DESC
"""

_RESERVATIONS_SQL = """
SELECT obligation_id, bank_id::text AS bank_id, amount::float8 AS amount_kw, interval_start,
       least(interval_end, coalesce(released_at, interval_end)) AS interval_end
FROM og.reservation
WHERE kind = 'POWER_KW' AND interval_start < %(t1)s AND interval_end > %(t0)s
  AND (released_at IS NULL OR released_at > interval_start)
"""

#: Active commitment rows: not superseded by a later revision (`og.commitment.supersedes`, 02a S1.6).
_COMMITMENTS_SQL = """
SELECT c.obligation_id, o.service_type, c.committed_kw::float8 AS committed_kw, c.interval_start,
       c.interval_end
FROM og.commitment c JOIN og.obligation o ON o.obligation_id = c.obligation_id
WHERE c.interval_start < %(t1)s AND c.interval_end > %(t0)s
  AND o.state NOT IN ('REJECTED', 'EXPIRED')
  AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)
"""

_FUNNEL_SQL = """
SELECT date_trunc(%(bucket)s, op.admitted_at) AS bucket_start, c.service_type,
       coalesce(pr.product_code, c.service_type) AS product, op.state AS opportunity_state,
       ob.state AS obligation_state,
       CASE WHEN ob.state IN ('REJECTED', 'EXPIRED') THEN coalesce(ob.last_reason_code, op.reason_code)
            ELSE op.reason_code END AS reason_code,
       count(*) AS n
FROM og.opportunity op
JOIN og.contract c ON c.contract_id = op.contract_id
LEFT JOIN og.product_rule pr ON pr.product_rule_id = op.product_rule_id
LEFT JOIN og.obligation ob ON ob.opportunity_id = op.opportunity_id
WHERE op.admitted_at >= %(t0)s AND op.admitted_at < %(t1)s
GROUP BY 1, 2, 3, 4, 5, 6
"""

#: Admission-time rejections never create an opportunity row; `contracts.admission._reject` traces them
#: (event_class ADMISSION, no `opportunity_id` in the payload, the R-ADMIT-* code in `reason_codes`).
_ADMISSION_REJECTS_SQL = """
SELECT date_trunc(%(bucket)s, t.created_at) AS bucket_start, coalesce(c.service_type, 'UNKNOWN') AS service_type,
       coalesce((SELECT min(pr.product_code) FROM og.product_rule pr WHERE pr.contract_id = c.contract_id),
                c.service_type, 'UNKNOWN') AS product,
       t.reason_codes[1] AS reason_code, count(*) AS n
FROM og.trace t
LEFT JOIN og.contract c
       ON c.contract_id::text = t.payload ->> 'contract_id'
WHERE t.event_class = 'ADMISSION' AND t.created_at >= %(t0)s AND t.created_at < %(t1)s
  AND NOT (t.payload ? 'opportunity_id') AND t.reason_codes IS NOT NULL
GROUP BY 1, 2, 3, 4
"""

#: The expected shape of the INTEGRATIONS agent's MMS submissions table (requested in the #19 report):
#: `submitted_at timestamptz, product text, status text, reject_reason text NULL`.
DEFAULT_MMS_TABLE: Final = "og.ercot_mms_submission"
_MMS_REQUIRED_COLUMNS: Final = frozenset({"submitted_at", "product", "status"})

_COLUMNS_SQL = """
SELECT column_name FROM information_schema.columns WHERE table_schema = %(schema)s AND table_name = %(table)s
"""

FunnelBucket = Literal["hour", "day"]


class PgExtViews:
    """psycopg 3 implementation of `ExtViewsProtocol` against the `og` schema."""

    def __init__(self, pool: AsyncConnectionPool[Any], *, mms_table: str = DEFAULT_MMS_TABLE) -> None:
        self._pool = pool
        self._mms_table = mms_table
        self._column_cache: dict[str, tuple[float, frozenset[str]]] = {}

    async def _fetch(
        self, query: str | sql.Composed, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall())

    async def _columns(self, qualified: str, *, ttl_s: float = 300.0) -> frozenset[str]:
        """Column names of `schema.table` (empty if absent), cached for `ttl_s` so a migration applied
        while og-api runs is picked up without a restart."""
        cached = self._column_cache.get(qualified)
        now = time.monotonic()
        if cached is not None and now - cached[0] < ttl_s:
            return cached[1]
        schema, _, table = qualified.partition(".")
        rows = await self._fetch(_COLUMNS_SQL, {"schema": schema, "table": table})
        columns = frozenset(str(r["column_name"]) for r in rows)
        self._column_cache[qualified] = (now, columns)
        return columns

    async def fleet_map_rows(self, *, grant_fresh_s: float) -> list[dict[str, Any]]:
        present = await self._columns("og.hub_state")
        optional = sql.SQL(", ").join(
            sql.SQL("s.{col}").format(col=sql.Identifier(col))
            if col in present
            else sql.SQL("NULL::float8 AS {col}").format(col=sql.Identifier(col))
            for col in _OPTIONAL_HUB_STATE_COLUMNS
        )
        query = sql.SQL(_FLEET_MAP_SQL).format(
            optional_columns=optional, grant_fresh_s=sql.Literal(int(grant_fresh_s))
        )
        return await self._fetch(query)

    async def active_contract_markets(self) -> list[ContractMarketRow]:
        rows = await self._fetch(_CONTRACT_MARKETS_SQL)
        return [
            ContractMarketRow(
                service_type=r["service_type"], market=r.get("market"), utility_id=r.get("utility_id")
            )
            for r in rows
        ]

    async def critical_alert_scopes(self) -> set[tuple[str, str]]:
        has_scope_columns = "scope_kind" in await self._columns("og.alert")
        scope_columns = (
            sql.SQL("scope_kind, scope_ref")
            if has_scope_columns
            else sql.SQL("NULL::text AS scope_kind, NULL::text AS scope_ref")
        )
        rows = await self._fetch(sql.SQL(_CRITICAL_ALERTS_SQL).format(scope_columns=scope_columns))
        return alert_scopes(rows)

    async def customer_site_readings(self, *, max_age_s: float) -> list[dict[str, Any]]:
        if not await self._columns("og.customer_site_meter_reading"):
            return []
        return await self._fetch(_SITE_READINGS_SQL, {"max_age_s": max_age_s})

    async def customer_contract_services(self) -> dict[str, list[str]]:
        rows = await self._fetch(_CUSTOMER_SERVICES_SQL)
        return {str(r["customer_id"]): list(r["services"]) for r in rows}

    async def weather_zone_load_mw(self) -> dict[str, tuple[float, datetime]]:
        rows = await self._fetch(_WEATHER_ZONE_LOAD_SQL, {"product": ERCOT_LOAD_PRODUCT})
        return {str(r["series"]): (float(r["value"]), r["ts"]) for r in rows}

    async def bank_scada_load_kw(self) -> dict[str, tuple[float, datetime]]:
        rows = await self._fetch(_BANK_SCADA_LOAD_SQL)
        return {str(r["bank_id"]): (float(r["value"]), r["ts"]) for r in rows}

    async def ledger_slices(
        self, *, t0: datetime, t1: datetime
    ) -> tuple[list[ReservationSlice], list[CommitmentSlice]]:
        params = {"t0": t0, "t1": t1}
        reservations = [
            ReservationSlice(
                obligation_id=r["obligation_id"],
                bank_id=str(r["bank_id"]),
                amount_kw=float(r["amount_kw"]),
                start=r["interval_start"],
                end=r["interval_end"],
            )
            for r in await self._fetch(_RESERVATIONS_SQL, params)
        ]
        commitments = [
            CommitmentSlice(
                obligation_id=r["obligation_id"],
                service_type=str(r["service_type"]),
                committed_kw=float(r["committed_kw"]),
                start=r["interval_start"],
                end=r["interval_end"],
            )
            for r in await self._fetch(_COMMITMENTS_SQL, params)
        ]
        return reservations, commitments

    async def funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[FunnelRow]:
        params = {"t0": t0, "t1": t1, "bucket": bucket}
        rows = [
            FunnelRow(
                bucket_start=r["bucket_start"],
                service_type=str(r["service_type"]),
                product=str(r["product"]),
                opportunity_state=r["opportunity_state"],
                obligation_state=r["obligation_state"],
                reason_code=r["reason_code"],
                n=int(r["n"]),
            )
            for r in await self._fetch(_FUNNEL_SQL, params)
        ]
        rows.extend(
            FunnelRow(
                bucket_start=r["bucket_start"],
                service_type=str(r["service_type"]),
                product=str(r["product"]),
                opportunity_state=None,
                obligation_state=None,
                reason_code=r["reason_code"],
                n=int(r["n"]),
            )
            for r in await self._fetch(_ADMISSION_REJECTS_SQL, params)
        )
        return rows

    async def mms_funnel_rows(self, *, t0: datetime, t1: datetime, bucket: str) -> list[MmsFunnelRow] | None:
        columns = await self._columns(self._mms_table)
        if not columns:
            return None
        if not columns >= _MMS_REQUIRED_COLUMNS:
            logger.warning(
                "MMS submissions table has an unrecognised shape; ignored by the bid funnel",
                extra={"table": self._mms_table, "columns": sorted(columns)},
            )
            return None
        schema, _, table = self._mms_table.partition(".")
        reason = sql.Identifier("reject_reason") if "reject_reason" in columns else sql.SQL("NULL::text")
        query = sql.SQL(
            "SELECT date_trunc(%(bucket)s, submitted_at) AS bucket_start, product, status, {reason} AS reason_code, "
            "count(*) AS n FROM {table} WHERE submitted_at >= %(t0)s AND submitted_at < %(t1)s GROUP BY 1, 2, 3, 4"
        ).format(reason=reason, table=sql.Identifier(schema, table))
        rows = await self._fetch(query, {"t0": t0, "t1": t1, "bucket": bucket})
        return [
            MmsFunnelRow(
                bucket_start=r["bucket_start"],
                product=str(r["product"]),
                status=str(r["status"]).upper(),
                reason_code=r["reason_code"],
                n=int(r["n"]),
            )
            for r in rows
        ]

    async def contract_totals(self, *, start: datetime, end: datetime) -> list[PeriodTotals]:
        return await fetch_contract_totals(self._pool, start, end)


def alert_scopes(rows: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """`(kind, ref)` pairs an alert row is about: its structured scope, plus any `hub_id`/`bank_id`/
    `scope_kind`+`scope_ref` in `detail`. Kinds are lower-case (`hub`, `bank`, `zone`, ...)."""
    scopes: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("scope_kind") and row.get("scope_ref"):
            scopes.add((str(row["scope_kind"]).lower(), str(row["scope_ref"])))
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                detail = None
        if not isinstance(detail, dict):
            continue
        if detail.get("scope_kind") and detail.get("scope_ref"):
            scopes.add((str(detail["scope_kind"]).lower(), str(detail["scope_ref"])))
        for key, kind in (("hub_id", "hub"), ("bank_id", "bank"), ("zone", "zone")):
            if detail.get(key):
                scopes.add((kind, str(detail[key])))
    return scopes


def get_ext_views(request: Request, cfg: Annotated[Config, Depends(get_config)]) -> ExtViewsProtocol:
    """One `PgExtViews` per app, built lazily over the lifespan's pool (`app.py` needs no change beyond
    the router mounts). Tests override this dependency with a fake."""
    views: ExtViewsProtocol | None = getattr(request.app.state, "ext_views", None)
    if views is None:
        mms_table = str(cfg.get("api.bid_funnel.mms_submission_table", DEFAULT_MMS_TABLE))
        views = PgExtViews(request.app.state.pool, mms_table=mms_table)
        request.app.state.ext_views = views
    return views


# =====================================================================================================
# Pure builders: coordinates, activity, service eligibility, fleet map
# =====================================================================================================

Activity = Literal["DELIVERING", "IDLE", "HOME_USE", "CHARGING", "FAULT", "OFFLINE"]
ACTIVITIES: Final[tuple[Activity, ...]] = ("DELIVERING", "IDLE", "HOME_USE", "CHARGING", "FAULT", "OFFLINE")
DEFAULT_ACTIVITY_EPSILON_KW: Final = 0.5

_KM_PER_DEG_LAT: Final = 111.32


@dataclass(frozen=True, slots=True)
class ZoneCentroid:
    lat: float
    lon: float
    radius_km: float


def load_zone_centroids(path: Path) -> dict[str, ZoneCentroid]:
    """`zone_centroids.json`'s `zones` table. A missing file yields `{}` (hubs without coordinates are
    then returned unlocated, never guessed)."""
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        logger.warning("zone centroid table unavailable", extra={"path": str(path)})
        return {}
    zones = data.get("zones", {}) if isinstance(data, dict) else {}
    return {
        str(zone): ZoneCentroid(
            lat=float(v["lat"]), lon=float(v["lon"]), radius_km=float(v.get("radius_km", 20))
        )
        for zone, v in zones.items()
    }


def derived_coordinates(hub_id: str, centroid: ZoneCentroid) -> tuple[float, float]:
    """A deterministic point uniformly inside the zone's disc: two uniforms from SHA-256(hub_id) give
    radius (sqrt for uniform area) and bearing. Same hub, same point, on every call and every host."""
    digest = hashlib.sha256(hub_id.encode()).digest()
    u_radius = int.from_bytes(digest[:4], "big") / 2**32
    u_angle = int.from_bytes(digest[4:8], "big") / 2**32
    r_km = centroid.radius_km * math.sqrt(u_radius)
    theta = 2 * math.pi * u_angle
    dlat = r_km * math.cos(theta) / _KM_PER_DEG_LAT
    dlon = r_km * math.sin(theta) / (_KM_PER_DEG_LAT * max(math.cos(math.radians(centroid.lat)), 0.1))
    return round(centroid.lat + dlat, 6), round(centroid.lon + dlon, 6)


def derive_activity(
    *, health: str | None, kw: float | None, bank_has_grant: bool, epsilon_kw: float
) -> Activity:
    """#19 activity rule. Sign convention: `p_kw` > 0 discharges (delivers), < 0 charges.

    - `fault` -> FAULT; no state row, `offline` or `stale` -> OFFLINE (a stale hub's kW is not current, so
      it is never shown as delivering);
    - kW < -eps -> CHARGING; kW > eps with an active grant on its bank -> DELIVERING; kW > eps without one ->
      HOME_USE (the home self-serving its own load); otherwise IDLE."""
    state = (health or "").lower()
    if state == "fault":
        return "FAULT"
    if state != "online" or kw is None:
        return "OFFLINE"
    if kw < -epsilon_kw:
        return "CHARGING"
    if kw > epsilon_kw:
        return "DELIVERING" if bank_has_grant else "HOME_USE"
    return "IDLE"


def service_eligibility_by_bank(
    model: MarketModel | None, contract_markets: Sequence[ContractMarketRow], bank_ids: Iterable[str]
) -> dict[str, list[str]]:
    """Which ACTIVE contract service types each bank may serve, by the K15 territory predicate
    (`MarketModel.bank_eligible`, the one implementation). No market model -> nothing is eligible
    (fail closed, as `opengrid.market` does)."""
    result: dict[str, set[str]] = {bank_id: set() for bank_id in bank_ids}
    if model is None:
        return {bank_id: [] for bank_id in result}
    for row in contract_markets:
        try:
            ref = market_of(market=row.market, utility_id=row.utility_id, service_type=row.service_type)
        except MarketModelError:
            continue
        for bank_id, services in result.items():
            if model.bank_eligible(bank_id, ref):
                services.add(row.service_type)
    return {bank_id: sorted(services) for bank_id, services in result.items()}


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def build_fleet_map_hubs(
    rows: Sequence[Mapping[str, Any]],
    *,
    centroids: Mapping[str, ZoneCentroid],
    eligibility: Mapping[str, Sequence[str]],
    epsilon_kw: float,
) -> list[dict[str, Any]]:
    """The `hubs[]` of `GET /og/api/fleet/map` from `ExtViewsProtocol.fleet_map_rows`."""
    hubs: list[dict[str, Any]] = []
    for row in rows:
        hub_id = str(row["hub_id"])
        kw = _float_or_none(row.get("kw"))
        activity = derive_activity(
            health=row.get("health"),
            kw=kw,
            bank_has_grant=bool(row.get("bank_has_grant")),
            epsilon_kw=epsilon_kw,
        )
        lat, lon = _float_or_none(row.get("lat")), _float_or_none(row.get("lon"))
        coord_source = "hub"
        if lat is None or lon is None:
            centroid = centroids.get(str(row["zone"]))
            if centroid is None:
                lat, lon, coord_source = None, None, "unlocated"
            else:
                lat, lon = derived_coordinates(hub_id, centroid)
                coord_source = "zone_centroid"
        e_kwh = _float_or_none(row.get("e_kwh")) or 0.0
        soc_kwh = _float_or_none(row.get("soc_kwh"))
        obligations = row.get("serving_obligations") or []
        if isinstance(obligations, str):
            obligations = json.loads(obligations)
        hubs.append(
            {
                "hub_id": hub_id,
                "bank_id": str(row["bank_id"]),
                "zone": str(row["zone"]),
                "lat": lat,
                "lon": lon,
                "coord_source": coord_source,
                "health": row.get("health") or "offline",
                "activity": activity,
                "kw": kw,
                "soc_kwh": soc_kwh,
                "soc_pct": round(100.0 * soc_kwh / e_kwh, 1) if soc_kwh is not None and e_kwh > 0 else None,
                "reserve_kwh": _float_or_none(row.get("r_kwh")),
                "rated_kw": _float_or_none(row.get("rated_kw")),
                "home_load_kw": _float_or_none(row.get("home_load_kw")),
                "meter_kw": _float_or_none(row.get("meter_kw")),
                "pv_kw": _float_or_none(row.get("pv_kw")),
                "fault_code": row.get("fault_code"),
                "last_seen_at": row["last_seen_at"].isoformat() if row.get("last_seen_at") else None,
                "serving_obligations": [
                    {
                        "obligation_id": str(o["obligation_id"]),
                        "service_type": o.get("service_type"),
                        "state": o.get("state"),
                        "tier": o.get("tier"),
                    }
                    for o in obligations
                ],
                "can_serve_services": []
                if activity in ("FAULT", "OFFLINE")
                else list(eligibility.get(str(row["bank_id"]), [])),
            }
        )
    return hubs


@dataclass(slots=True)
class FleetMapSnapshot:
    generated_at: datetime
    hubs: list[dict[str, Any]]
    #: This cycle's granted kW per bank (`og.grant`), for the grid heat cells' "served".
    bank_granted_kw: dict[str, float] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by_id:
            self.by_id = {h["hub_id"]: h for h in self.hubs}


class FleetMapService:
    """The fleet map snapshot, cached for `[api.fleet_map].cache_ttl_s` (default 2 s) behind one lock so
    concurrent callers share one query; service eligibility is refreshed on its own slower TTL. Reused by
    the grid heat cells, the ledger's capacity and the bulk-command risk check."""

    def __init__(self, views: ExtViewsProtocol, cfg: Config) -> None:
        self._views = views
        self._ttl_s = float(cfg.get("api.fleet_map.cache_ttl_s", 2.0))
        self._eligibility_ttl_s = float(cfg.get("api.fleet_map.eligibility_ttl_s", 60.0))
        self._epsilon_kw = float(cfg.get("api.fleet_map.activity_epsilon_kw", DEFAULT_ACTIVITY_EPSILON_KW))
        self._grant_fresh_s = float(cfg.get("api.fleet_map.grant_fresh_s", 10.0))
        self._centroids = load_zone_centroids(grid_config_dir(cfg) / ZONE_CENTROIDS_FILE)
        self._tdsp_anchor = tdsp_config_anchor(cfg)
        self._lock = asyncio.Lock()
        self._snapshot: FleetMapSnapshot | None = None
        self._snapshot_at = -math.inf
        self._eligibility: dict[str, list[str]] = {}
        self._eligibility_at = -math.inf
        self.warnings: list[str] = []

    async def snapshot(self, *, now: float | None = None) -> FleetMapSnapshot:
        clock = time.monotonic() if now is None else now
        async with self._lock:
            if self._snapshot is not None and clock - self._snapshot_at < self._ttl_s:
                return self._snapshot
            rows = await self._views.fleet_map_rows(grant_fresh_s=self._grant_fresh_s)
            eligibility = await self._eligibility_for(rows, clock)
            hubs = build_fleet_map_hubs(
                rows, centroids=self._centroids, eligibility=eligibility, epsilon_kw=self._epsilon_kw
            )
            granted = {str(r["bank_id"]): float(r.get("bank_granted_kw") or 0.0) for r in rows}
            self._snapshot = FleetMapSnapshot(
                generated_at=datetime.now(UTC),
                hubs=hubs,
                bank_granted_kw={b: kw for b, kw in granted.items() if kw},
            )
            self._snapshot_at = clock
            return self._snapshot

    async def _eligibility_for(self, rows: Sequence[Mapping[str, Any]], clock: float) -> dict[str, list[str]]:
        if clock - self._eligibility_at < self._eligibility_ttl_s:
            return self._eligibility
        banks = {str(r["bank_id"]): str(r["zone"]) for r in rows}
        model: MarketModel | None
        self.warnings = []
        try:
            model = load_market_model(banks=banks.items(), config_path=self._tdsp_anchor)
        except (OSError, ValueError) as exc:
            logger.warning(
                "market model unavailable; can_serve_services left empty", extra={"error": str(exc)}
            )
            self.warnings.append(f"market model unavailable ({exc}); can_serve_services is empty")
            model = None
        contract_markets = await self._views.active_contract_markets()
        self._eligibility = service_eligibility_by_bank(model, contract_markets, banks)
        self._eligibility_at = clock
        return self._eligibility


def get_fleet_map_service(
    request: Request,
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    cfg: Annotated[Config, Depends(get_config)],
) -> FleetMapService:
    service: FleetMapService | None = getattr(request.app.state, "fleet_map_service", None)
    if service is None:
        service = FleetMapService(views, cfg)
        request.app.state.fleet_map_service = service
    return service


def activity_counts(hubs: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(ACTIVITIES, 0)
    for hub in hubs:
        counts[hub["activity"]] += 1
    return counts


# =====================================================================================================
# Pure builders: customers map
# =====================================================================================================


def load_customer_sites(path: Path) -> list[dict[str, Any]]:
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        logger.warning("customer site table unavailable", extra={"path": str(path)})
        return []
    sites = data.get("sites", []) if isinstance(data, dict) else []
    return [dict(s) for s in sites if isinstance(s, dict)]


def build_customer_sites(
    configured: Sequence[Mapping[str, Any]],
    readings: Sequence[Mapping[str, Any]],
    services: Mapping[str, Sequence[str]],
    *,
    epsilon_kw: float,
) -> list[dict[str, Any]]:
    """Configured sites first (their coordinates), then any metered site the table does not know
    (unlocated). `kw` is the site meter's `p_kw` (+ import from the grid); `consuming` is kW > eps.
    `service_type` is the configured one, else the customer's only ACTIVE contract service."""
    latest = {(str(r["customer_id"]), str(r["site_id"])): r for r in readings}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _entry(customer_id: str, site_id: str, site: Mapping[str, Any] | None) -> dict[str, Any]:
        reading = latest.get((customer_id, site_id))
        kw = _float_or_none(reading["p_kw"]) if reading is not None else None
        customer_services = list(services.get(customer_id, []))
        service_type = (site or {}).get("service_type") or (
            customer_services[0] if len(customer_services) == 1 else None
        )
        lat = _float_or_none((site or {}).get("lat"))
        lon = _float_or_none((site or {}).get("lon"))
        return {
            "customer_id": customer_id,
            "site_id": site_id,
            "name": (site or {}).get("name") or site_id,
            "service_type": service_type,
            "contract_services": customer_services,
            "lat": lat,
            "lon": lon,
            "coord_source": "placeholder"
            if site and site.get("placeholder")
            else ("config" if site else "unlocated"),
            "kw": kw,
            "consuming": kw is not None and kw > epsilon_kw,
            "reading_ts": reading["ts"].isoformat() if reading is not None else None,
            "reading_quality": reading.get("quality") if reading is not None else None,
            "has_active_contract": bool(customer_services),
        }

    for site in configured:
        key = (str(site["customer_id"]), str(site["site_id"]))
        seen.add(key)
        out.append(_entry(key[0], key[1], site))
    for key in sorted(latest):
        if key not in seen:
            out.append(_entry(key[0], key[1], None))
    return out


# =====================================================================================================
# Pure builders: grid layers
# =====================================================================================================


@dataclass(frozen=True, slots=True)
class GridStatic:
    """`grid_map_data.json`, parsed once and pre-grouped (the transmission layer is ~7k polylines)."""

    note: str
    weather_zones: list[dict[str, Any]]
    storage: list[dict[str, Any]]
    residential_centroid: dict[str, Any] | None
    grid_entry_point: dict[str, Any] | None
    corridors: list[dict[str, Any]]
    lines_by_kv: dict[int, list[dict[str, Any]]]
    sha256: str


def load_grid_static(path: Path) -> GridStatic:
    """Raises `OSError`/`ValueError` if the data file is missing or malformed; the router reports that
    as a 503 rather than serving an empty map."""
    raw = path.read_bytes()
    data = json.loads(raw)
    lines_by_kv: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in data.get("transmission_lines", []):
        kv = round(float(line.get("voltage_kv") or 0))
        lines_by_kv[kv].append(
            {"path": line.get("path", []), "owner": line.get("owner"), "status": line.get("status")}
        )
    corridors = [
        {k: v for k, v in c.items() if k != "hourly"}
        for c in data.get("corridors", [])
        if isinstance(c, dict)
    ]
    return GridStatic(
        note=str(data.get("generated_note", "")),
        weather_zones=[dict(z) for z in data.get("zones", [])],
        storage=[dict(s) for s in data.get("storage", [])],
        residential_centroid=data.get("residential_centroid"),
        grid_entry_point=data.get("grid_entry_point"),
        corridors=corridors,
        lines_by_kv=dict(sorted(lines_by_kv.items(), reverse=True)),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def weather_zones_by_load_zone(cfg: Config) -> dict[str, tuple[str, ...]]:
    """The forecast module's load-zone -> weather-zone correspondence, with the same config override
    (`[forecast].weather_zones_by_load_zone`), so both screens sum a zone's load identically."""
    mapping = cfg.get("forecast.weather_zones_by_load_zone", DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE)
    return {str(lz): tuple(str(w) for w in wzs) for lz, wzs in mapping.items()}


def build_zone_load(
    weather_live: Mapping[str, tuple[float, datetime]],
    static: GridStatic,
    mapping: Mapping[str, Sequence[str]],
    centroids: Mapping[str, ZoneCentroid],
    fleet_zones: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(load zones, weather zones). A load zone's load is the sum of its mapped weather zones' latest
    live MW; `live=false` with the export snapshot when no live row exists for any of them."""
    static_mw = {str(z.get("zone_key")): float(z.get("load_mw") or 0) for z in static.weather_zones}
    weather = []
    for z in static.weather_zones:
        key = str(z.get("zone_key"))
        live = weather_live.get(key)
        weather.append(
            {
                "zone_key": key,
                "name": z.get("zone"),
                "lat": z.get("lat"),
                "lon": z.get("lon"),
                "load_mw": live[0] if live else z.get("load_mw"),
                "load_ts": live[1].isoformat() if live else None,
                "live": live is not None,
                "nearest_storage": z.get("nearest_storage"),
            }
        )
    zones = []
    for lz in sorted(set(fleet_zones) | set(mapping)):
        wzs = list(mapping.get(lz, ()))
        live_parts = [weather_live[w] for w in wzs if w in weather_live]
        centroid = centroids.get(lz)
        if live_parts:
            load_mw: float | None = sum(p[0] for p in live_parts)
            ts: str | None = max(p[1] for p in live_parts).isoformat()
        else:
            load_mw = sum(static_mw[w] for w in wzs if w in static_mw) if wzs else None
            ts = None
        zones.append(
            {
                "zone": lz,
                "lat": centroid.lat if centroid else None,
                "lon": centroid.lon if centroid else None,
                "weather_zones": wzs,
                "load_mw": load_mw,
                "load_ts": ts,
                "live": bool(live_parts),
                "approximate_mapping": True,
            }
        )
    return zones, weather


def build_heat_cells(
    hubs: Sequence[Mapping[str, Any]],
    bank_scada_kw: Mapping[str, tuple[float, datetime]],
    zone_load: Sequence[Mapping[str, Any]],
    granted_kw_by_bank: Mapping[str, float],
) -> list[dict[str, Any]]:
    """Demand cells at bank and zone level. Bank demand = its latest SCADA `REAL_POWER_KW`; zone demand
    = the zone's ERCOT load (MW -> kW). Served = the fleet's granted delivery (this cycle's `og.grant`)
    inside the cell; unserved = max(demand - served, 0). A cell is placed at the mean of its hubs."""
    bank_points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    bank_zone: dict[str, str] = {}
    zone_points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for hub in hubs:
        bank_zone[hub["bank_id"]] = hub["zone"]
        if hub.get("lat") is not None and hub.get("lon") is not None:
            bank_points[hub["bank_id"]].append((hub["lat"], hub["lon"]))
            zone_points[hub["zone"]].append((hub["lat"], hub["lon"]))

    def _centre(points: Sequence[tuple[float, float]]) -> tuple[float | None, float | None]:
        if not points:
            return None, None
        return round(sum(p[0] for p in points) / len(points), 6), round(
            sum(p[1] for p in points) / len(points), 6
        )

    cells: list[dict[str, Any]] = []
    for bank_id in sorted(bank_zone):
        scada = bank_scada_kw.get(bank_id)
        served = float(granted_kw_by_bank.get(bank_id, 0.0))
        demand = scada[0] if scada else None
        lat, lon = _centre(bank_points[bank_id])
        cells.append(
            {
                "level": "bank",
                "id": bank_id,
                "zone": bank_zone[bank_id],
                "lat": lat,
                "lon": lon,
                "demand_kw": demand,
                "demand_source": "scada" if scada else None,
                "demand_ts": scada[1].isoformat() if scada else None,
                "served_kw": served,
                "unserved_kw": max(demand - served, 0.0) if demand is not None else None,
            }
        )
    served_by_zone: dict[str, float] = defaultdict(float)
    for bank_id, kw in granted_kw_by_bank.items():
        if bank_id in bank_zone:
            served_by_zone[bank_zone[bank_id]] += float(kw)
    for zone in zone_load:
        zid = str(zone["zone"])
        if zid not in zone_points:
            continue
        demand_kw = float(zone["load_mw"]) * 1000.0 if zone.get("load_mw") is not None else None
        served = served_by_zone.get(zid, 0.0)
        lat, lon = _centre(zone_points[zid])
        cells.append(
            {
                "level": "zone",
                "id": zid,
                "zone": zid,
                "lat": lat,
                "lon": lon,
                "demand_kw": demand_kw,
                "demand_source": "ercot_load" if zone.get("live") else "ercot_load_snapshot",
                "demand_ts": zone.get("load_ts"),
                "served_kw": served,
                "unserved_kw": max(demand_kw - served, 0.0) if demand_kw is not None else None,
            }
        )
    return cells


# =====================================================================================================
# Pure builders: dispatch ledger
# =====================================================================================================

LedgerLevel = Literal["fleet", "zone", "bank", "hub"]
UNCOMMITTED_LABEL: Final = "Uncommitted capacity"
_CHILD_LEVEL: Final[dict[str, LedgerLevel | None]] = {
    "fleet": "zone",
    "zone": "bank",
    "bank": "hub",
    "hub": None,
}


@dataclass(frozen=True, slots=True)
class HubCapacity:
    hub_id: str
    bank_id: str
    zone: str
    rated_kw: float
    available: bool


def hub_capacities(hubs: Iterable[Mapping[str, Any]]) -> list[HubCapacity]:
    return [
        HubCapacity(
            hub_id=h["hub_id"],
            bank_id=h["bank_id"],
            zone=h["zone"],
            rated_kw=float(h.get("rated_kw") or 0.0),
            available=h["activity"] not in ("FAULT", "OFFLINE"),
        )
        for h in hubs
    ]


def scope_bank_factors(
    level: LedgerLevel, scope_id: str | None, hubs: Sequence[HubCapacity]
) -> dict[str, float]:
    """How much of each bank's reserved/committed kW belongs to the scope: 1.0 for a whole bank in the
    scope; for a hub, its share of the bank's rated kW (reservations are bank-level, 02a S1.9)."""
    if level == "fleet":
        return {h.bank_id: 1.0 for h in hubs}
    if level == "zone":
        return {h.bank_id: 1.0 for h in hubs if h.zone == scope_id}
    if level == "bank":
        return {scope_id: 1.0} if scope_id is not None and any(h.bank_id == scope_id for h in hubs) else {}
    hub = next((h for h in hubs if h.hub_id == scope_id), None)
    if hub is None:
        return {}
    bank_total = sum(h.rated_kw for h in hubs if h.bank_id == hub.bank_id)
    return {hub.bank_id: hub.rated_kw / bank_total if bank_total > 0 else 0.0}


def scope_hubs(level: LedgerLevel, scope_id: str | None, hubs: Sequence[HubCapacity]) -> list[HubCapacity]:
    if level == "fleet":
        return list(hubs)
    attr = {"zone": "zone", "bank": "bank_id", "hub": "hub_id"}[level]
    return [h for h in hubs if getattr(h, attr) == scope_id]


def _covers(start: datetime, end: datetime, at: datetime) -> bool:
    return start <= at < end


def ledger_point(
    at: datetime,
    *,
    level: LedgerLevel,
    factors: Mapping[str, float],
    capacity_kw: float,
    reservations: Sequence[ReservationSlice],
    commitments: Sequence[CommitmentSlice],
) -> dict[str, Any]:
    """Reserved / committed / uncommitted capacity at one instant for one scope.

    Commitments carry no bank (02a S1.6); below fleet level an obligation's committed kW is placed on
    banks in proportion to its live reservations at `at`. At fleet level the full committed kW counts,
    and the part with no reservation is reported as `unallocated_committed_kw`."""
    live = [r for r in reservations if _covers(r.start, r.end, at)]
    reserved = sum(r.amount_kw * factors.get(r.bank_id, 0.0) for r in live)
    by_obligation: dict[UUID, list[ReservationSlice]] = defaultdict(list)
    for r in live:
        by_obligation[r.obligation_id].append(r)
    committed = 0.0
    unallocated = 0.0
    by_service: dict[str, float] = defaultdict(float)
    for c in commitments:
        if not _covers(c.start, c.end, at):
            continue
        slices = by_obligation.get(c.obligation_id, [])
        total = sum(s.amount_kw for s in slices)
        if total > 0:
            share = sum(s.amount_kw * factors.get(s.bank_id, 0.0) for s in slices) / total
        else:
            share = 1.0 if level == "fleet" else 0.0
            if level == "fleet":
                unallocated += c.committed_kw
        kw = c.committed_kw * share
        committed += kw
        if kw:
            by_service[c.service_type] += kw
    used = max(reserved, committed)
    point = {
        "t": at.isoformat(),
        "capacity_kw": round(capacity_kw, 3),
        "reserved_kw": round(reserved, 3),
        "committed_kw": round(committed, 3),
        "uncommitted_capacity_kw": round(max(capacity_kw - used, 0.0), 3),
        "over_committed_kw": round(max(used - capacity_kw, 0.0), 3),
        "committed_by_service": {k: round(v, 3) for k, v in sorted(by_service.items())},
    }
    if level == "fleet":
        point["unallocated_committed_kw"] = round(unallocated, 3)
    return point


def build_ledger(
    *,
    level: LedgerLevel,
    scope_id: str | None,
    hubs: Sequence[HubCapacity],
    reservations: Sequence[ReservationSlice],
    commitments: Sequence[CommitmentSlice],
    t0: datetime,
    t1: datetime,
    bucket: timedelta,
    now: datetime,
) -> dict[str, Any]:
    """The aggregate-first ledger: a bucketed timeline for the scope plus one "now" row per child
    scope (fleet -> zones -> banks -> hubs) for drill-down. Capacity = rated kW of the scope's hubs that
    are not FAULT/OFFLINE right now (a present-tense figure applied across the window)."""
    in_scope = scope_hubs(level, scope_id, hubs)
    factors = scope_bank_factors(level, scope_id, hubs)
    capacity = sum(h.rated_kw for h in in_scope if h.available)
    timeline = []
    at = t0
    while at < t1:
        timeline.append(
            ledger_point(
                at,
                level=level,
                factors=factors,
                capacity_kw=capacity,
                reservations=reservations,
                commitments=commitments,
            )
        )
        at += bucket
    child_level = _CHILD_LEVEL[level]
    children: list[dict[str, Any]] = []
    if child_level is not None:
        attr = {"zone": "zone", "bank": "bank_id", "hub": "hub_id"}[child_level]
        for child_id in sorted({getattr(h, attr) for h in in_scope}):
            child_hubs = scope_hubs(child_level, child_id, hubs)
            point = ledger_point(
                now,
                level=child_level,
                factors=scope_bank_factors(child_level, child_id, hubs),
                capacity_kw=sum(h.rated_kw for h in child_hubs if h.available),
                reservations=reservations,
                commitments=commitments,
            )
            children.append({"level": child_level, "id": child_id, "hub_count": len(child_hubs), **point})
    return {
        "level": level,
        "id": scope_id,
        "from": t0.isoformat(),
        "to": t1.isoformat(),
        "bucket_minutes": int(bucket.total_seconds() // 60),
        "hub_count": len(in_scope),
        "available_hub_count": sum(1 for h in in_scope if h.available),
        "labels": {"uncommitted_capacity_kw": UNCOMMITTED_LABEL},
        "basis": "hub" if level == "hub" else "bank",
        "notes": ["hub-level values are the bank's pro-rata share by rated kW"] if level == "hub" else [],
        "now": ledger_point(
            now,
            level=level,
            factors=factors,
            capacity_kw=capacity,
            reservations=reservations,
            commitments=commitments,
        ),
        "timeline": timeline,
        "children": children,
    }


# =====================================================================================================
# Pure builders: bid funnel
# =====================================================================================================

#: Obligation states that mean the gate selected (submitted) it.
SUBMITTED_OBLIGATION_STATES: Final = frozenset(
    {"SELECTED", "COMMITTED", "DELIVERING", "FULFILLED", "SHORTFALL", "SETTLED"}
)
#: Obligation states reached only through a commitment (awarded).
AWARDED_OBLIGATION_STATES: Final = frozenset({"COMMITTED", "DELIVERING", "FULFILLED", "SHORTFALL", "SETTLED"})
_FUNNEL_STAGES: Final = ("available", "submitted", "awarded", "rejected", "expired")
_MMS_ACCEPTED: Final = frozenset({"ACCEPTED", "AWARDED", "CLEARED"})
_MMS_REJECTED: Final = frozenset({"REJECTED", "INVALID", "FAILED"})


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(_FUNNEL_STAGES, 0)


def classify_funnel_row(row: FunnelRow) -> tuple[dict[str, int], str | None]:
    """Stage counts for one grouped row and, if it is a rejection, its reason code."""
    counts = _zero_counts()
    counts["available"] = row.n
    if row.opportunity_state is None:  # admission-time rejection: offered, never became an opportunity
        counts["rejected"] = row.n
        return counts, row.reason_code or "UNSPECIFIED"
    obligation = row.obligation_state
    if row.opportunity_state == "SELECTED" or obligation in SUBMITTED_OBLIGATION_STATES:
        counts["submitted"] = row.n
    if obligation in AWARDED_OBLIGATION_STATES:
        counts["awarded"] = row.n
    if row.opportunity_state == "REJECTED" or obligation == "REJECTED":
        counts["rejected"] = row.n
        return counts, row.reason_code or "UNSPECIFIED"
    if row.opportunity_state == "EXPIRED" or obligation == "EXPIRED":
        counts["expired"] = row.n
    return counts, None


def build_funnel(rows: Sequence[FunnelRow], mms_rows: Sequence[MmsFunnelRow] | None) -> dict[str, Any]:
    totals = _zero_counts()
    by_product: dict[tuple[str, str], dict[str, int]] = defaultdict(_zero_counts)
    series: dict[tuple[datetime, str, str], dict[str, int]] = defaultdict(_zero_counts)
    reasons: dict[str, dict[str, Any]] = {}
    for row in rows:
        counts, reason = classify_funnel_row(row)
        product_key = (row.service_type, row.product)
        for stage, n in counts.items():
            totals[stage] += n
            by_product[product_key][stage] += n
            series[(row.bucket_start, *product_key)][stage] += n
        if reason is not None:
            entry = reasons.setdefault(
                reason, {"reason_code": reason, "count": 0, "by_product": defaultdict(int)}
            )
            entry["count"] += row.n
            entry["by_product"][row.product] += row.n
    mms_totals: dict[str, int] | None = None
    if mms_rows is not None:
        mms_totals = {"submitted": 0, "accepted": 0, "rejected": 0}
        for m in mms_rows:
            mms_totals["submitted"] += m.n
            if m.status in _MMS_ACCEPTED:
                mms_totals["accepted"] += m.n
            if m.status in _MMS_REJECTED:
                mms_totals["rejected"] += m.n
                code = m.reason_code or "MMS_REJECTED"
                entry = reasons.setdefault(
                    code, {"reason_code": code, "count": 0, "by_product": defaultdict(int)}
                )
                entry["count"] += m.n
                entry["by_product"][m.product] += m.n
    return {
        "totals": totals,
        "by_product": [
            {"service_type": st, "product": product, **counts}
            for (st, product), counts in sorted(by_product.items())
        ],
        "series": [
            {"bucket_start": b.isoformat(), "service_type": st, "product": product, **counts}
            for (b, st, product), counts in sorted(series.items())
        ],
        "rejection_reasons": sorted(
            ({**e, "by_product": dict(sorted(e["by_product"].items()))} for e in reasons.values()),
            key=lambda e: (-e["count"], e["reason_code"]),
        ),
        "mms": mms_totals,
    }
