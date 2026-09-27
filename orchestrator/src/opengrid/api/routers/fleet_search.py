"""The Fleet table at scale (owner review R3): keyset-paged, filtered, sorted hub rows; typeahead search;
server-side "select all matching"; pending safe-stop release requests; and the one-call hub detail the
Fleet drawer renders.

Read-only. Every statement is a SELECT built here from hard-coded fragments, with every caller value
bound as a `%s` parameter, and run through `PgStore.fleet_rows` (the store's single read helper).

Health is derived at query time from `og.hub_state.last_seen_at` against `[health]` thresholds
(`opengrid.health.model.HealthThresholds`, 02b S6.4) -- never from the cached `hub_state.health`
column, which is only as fresh as the last engine flush. Rows are classified with the health module's
own `classify_hub_health`; the SQL `CASE` below is the same rule restated for the WHERE clause only, so
a health filter over millions of rows runs in the database.

Query-plan note (for millions of hubs):

* Paging is keyset, never OFFSET: `WHERE (sort_expr, h.hub_id) > (%s, %s) ORDER BY sort_expr, h.hub_id
  LIMIT n+1`. Sorting by hub is an index range scan on `og.hub`'s primary key; the other sort keys need
  a matching expression index to stay O(page) (`og.hub (bank_id, hub_id)`, `og.hub (zone, hub_id)`,
  `og.hub_state` is deliberately NOT indexed for P or telemetry age (0042 dropped 0039's two: they
  defeated HOT updates on the hottest table), so those two sorts scan and top-N sort the filtered set
  once per page (bounded memory; ~2,500 rows today).
* The total is an estimate: `pg_class.reltuples` with no filter, else the planner's row estimate from
  `EXPLAIN (FORMAT JSON)` -- never a `count(*)` over the fleet.
* Search is a case-insensitive prefix match (`ILIKE 'q%'`). It is an index range scan once
  `lower(hub_id) text_pattern_ops` / `lower(bank_id) text_pattern_ops` indexes exist; until then it is a
  sequential scan of `og.hub` bounded by `LIMIT`.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import json
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_config, get_proposals, get_store
from opengrid.api.proposals import ProposalStore
from opengrid.core import geo
from opengrid.health.model import HealthThresholds
from opengrid.health.rules import classify_hub_health
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api/fleet", tags=["fleet-search"])

PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
SELECTION_MAX_DEFAULT = 5000
SEARCH_LIMIT_MAX = 50
#: Owner-facing health words -> the 02b S6.4 states they cover.
HEALTH_LABELS: dict[str, str] = {
    "online": "OK",
    "stale": "WATCH",
    "degraded": "DEGRADED",
    "quarantined": "QUARANTINED",
    "fault": "FAULT",
    "offline": "OFFLINE",
}
HEALTH_STATES = {label: state for state, label in HEALTH_LABELS.items()}
ACTIVITIES = ("delivering", "serving_home", "charging", "idle")
#: |P| at or under this is idle. Sign convention (interfaces/mqtt/telemetry.schema.json): +charge / -discharge.
IDLE_KW = 0.1
_RELEASE_PROPOSAL_KIND = "safestop-release"  # api.routers.safestop's proposal kind for a release request

SortKey = Literal["hub", "bank", "zone", "soc", "kw", "health", "age", "hw", "fw"]
#: Battery-reported og.hub columns (migration 0036) read through `to_jsonb(h.*)`, so a schema without
#: them yields NULL instead of an error (a guarded read; see the module docstring's query-plan note).
_HW_SQL = "(to_jsonb(h.*) ->> 'hardware_revision')"
_FW_SQL = "(to_jsonb(h.*) ->> 'firmware_version')"
#: sort key -> (SQL expression, cast for the bound cursor value). bank_id/zone/p_kw/last_seen_at are
#: NOT NULL (0001), so the plain columns match migration 0039's (column, hub_id) indexes; the nullable
#: expressions are coalesced so the row-value comparison stays total. "age" sorts on last_seen_at inverted, so ascending age = most recent first.
_SORT_SQL: dict[str, tuple[str, str]] = {
    "hw": (f"coalesce({_HW_SQL}, '')", "text"),
    "fw": (f"coalesce({_FW_SQL}, '')", "text"),
    "hub": ("h.hub_id", "text"),
    "bank": ("h.bank_id", "text"),
    "zone": ("h.zone", "text"),
    "soc": ("coalesce(s.soc_kwh::float8 / nullif(h.e_kwh::float8, 0), -1)", "float8"),
    "kw": ("s.p_kw", "float8"),
    "health": ("{health}", "text"),
    "age": ("s.last_seen_at", "timestamptz"),
}

_HEALTH_SQL = (
    "(CASE WHEN coalesce(s.fault_code, '') <> '' THEN 'fault'"
    " WHEN lower(coalesce(s.health, '')) IN ('degraded', 'quarantined') THEN lower(s.health)"
    " WHEN s.last_seen_at IS NULL OR s.last_seen_at < now() - make_interval(secs => %s) THEN 'offline'"
    " WHEN s.last_seen_at < now() - make_interval(secs => %s) THEN 'stale'"
    " ELSE 'online' END)"
)
_LEASED = "(s.lease_expires_at IS NOT NULL AND s.lease_expires_at > now())"
_ACTIVITY_SQL: dict[str, str] = {
    "delivering": f"(s.p_kw < -{IDLE_KW} AND {_LEASED})",
    "serving_home": f"(s.p_kw < -{IDLE_KW} AND NOT {_LEASED})",
    "charging": f"(s.p_kw > {IDLE_KW})",
    "idle": f"(abs(coalesce(s.p_kw, 0)) <= {IDLE_KW})",
}
_ACTIVITY_CASE = (
    f"(CASE WHEN {_ACTIVITY_SQL['delivering']} THEN 'delivering'"
    f" WHEN {_ACTIVITY_SQL['serving_home']} THEN 'serving_home'"
    f" WHEN {_ACTIVITY_SQL['charging']} THEN 'charging' ELSE 'idle' END)"
)
_FROM = "FROM og.hub h JOIN og.hub_state s ON s.hub_id = h.hub_id"
_ROW_COLUMNS = (
    "h.hub_id, h.bank_id, h.zone, h.e_kwh, h.r_kwh, h.p_kw AS rated_p_kw, s.soc_kwh, s.p_kw,"
    " s.health AS stored_health, s.fault_code, s.last_seen_at, s.lease_epoch, s.lease_expires_at,"
    f" s.last_command_id, {_ACTIVITY_CASE} AS activity,"
    f" {_HW_SQL} AS hardware_revision, {_FW_SQL} AS firmware_version"
)


@dataclass(frozen=True, slots=True)
class HubFilter:
    """What the Fleet table is filtered by; every field is optional (empty = no constraint)."""

    zones: tuple[str, ...] = ()
    bank: str | None = None
    health: tuple[str, ...] = ()  # 02b S6.4 states: online/stale/degraded/quarantined/fault/offline
    activity: tuple[str, ...] = ()
    soc_min: float | None = None  # percent of rated energy
    soc_max: float | None = None
    q: str | None = None  # hub id prefix
    hw: tuple[str, ...] = ()  # hardware revisions
    fw: tuple[str, ...] = ()  # firmware versions
    fw_not: str | None = None  # "FW != version": finds out-of-date hubs
    asset_class: tuple[str, ...] = ()  # HOME / MOBILE / UTILITY_SCALE
    e_kwh_min: float | None = None  # rated energy per hub (og.hub.e_kwh), inclusive bounds
    e_kwh_max: float | None = None
    p_kw_min: float | None = None  # rated power per hub (og.hub.p_kw), inclusive bounds
    p_kw_max: float | None = None
    units: tuple[int, ...] = ()  # battery units per hub (og.hub.units): 2 = a dual-unit home
    availability: tuple[str, ...] = ()  # the hub's bank availability (D-37): AVAILABLE / UNAVAILABLE

    @property
    def empty(self) -> bool:
        return not (
            self.hw
            or self.asset_class
            or self.e_kwh_min is not None
            or self.e_kwh_max is not None
            or self.p_kw_min is not None
            or self.p_kw_max is not None
            or self.units
            or self.availability
            or self.fw
            or self.fw_not
            or self.zones
            or self.bank
            or self.health
            or self.activity
            or self.soc_min is not None
            or self.soc_max is not None
            or self.q
        )


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Per-request context: the health module's own `HealthThresholds` (built by its one constructor,
    `HealthThresholds.from_config`, so `[health].hub_stale_s`/`hub_offline_s` mean exactly what they mean
    to `classify_hub_health`) and the D-31 mobile unit ids."""

    health: HealthThresholds
    mobile: tuple[str, ...] = ()

    @property
    def online_s(self) -> float:
        """Telemetry age past which a hub is WATCH (stale): `[health].hub_stale_s`."""
        return float(self.health.hub_stale_s)

    @property
    def offline_s(self) -> float:
        """Telemetry age past which a hub is OFFLINE: `[health].hub_offline_s`."""
        return float(self.health.hub_offline_s)

    @classmethod
    def from_config(cls, cfg: Config) -> Thresholds:
        return cls(health=HealthThresholds.from_config(cfg), mobile=tuple(sorted(mobile_units())))


@dataclass(slots=True)
class Sql:
    text: str
    params: list[Any] = field(default_factory=list)


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


#: Owner asset classes (R3.1): a home battery, a D-31 mobile unit (truck), a substation BESS.
ASSET_CLASSES = ("HOME", "MOBILE", "UTILITY_SCALE")
_ASSET_SQL = (
    "(CASE WHEN h.bank_id = ANY(%s) OR h.hub_id = ANY(%s) THEN 'MOBILE'"
    " WHEN EXISTS (SELECT 1 FROM og.asset a WHERE a.asset_class = 'SUBSTATION'"
    " AND (a.asset_id = h.hub_id OR a.bank_id = h.bank_id)) THEN 'UTILITY_SCALE' ELSE 'HOME' END)"
)


#: D-37 bank availability, a hub inheriting its bank's. Read guarded through `to_jsonb(b.*)` so a database
#: before migration 0046 (no column) reads AVAILABLE, the column default; any value other than AVAILABLE
#: reads UNAVAILABLE (fail closed, the rule of `opengrid.market.availability.parse_availability`).
AVAILABILITIES = ("AVAILABLE", "UNAVAILABLE")
AVAILABILITY_SQL = (
    "(CASE WHEN coalesce((SELECT to_jsonb(b.*) ->> 'availability' FROM og.bank b"
    " WHERE b.bank_id = h.bank_id), 'AVAILABLE') = 'AVAILABLE' THEN 'AVAILABLE' ELSE 'UNAVAILABLE' END)"
)
#: Battery units per hub (og.hub.units, 1 or 2), guarded like the HW/FW columns; NULL reads as one unit.
_UNITS_SQL = "coalesce((to_jsonb(h.*) ->> 'units')::int, 1)"


def asset_sql(th: Thresholds) -> Sql:
    """`og.asset` SUBSTATION -> UTILITY_SCALE; SERVICES' [[assignment]] (via `selector.gate`) -> MOBILE."""
    return Sql(_ASSET_SQL, [list(th.mobile), list(th.mobile)])


def _gate_attr(name: str) -> Any:
    """A reader from `opengrid.selector.gate` (OPTIMIZER owns the D-31 registry reader), or None on a
    build that does not have it yet -- then no hub is MOBILE."""
    try:
        module = importlib.import_module("opengrid.selector.gate")
    except ImportError:
        return None
    return getattr(module, name, None)


def mobile_units() -> dict[str, str]:
    """`bank_id -> home-station zone` from `selector.gate.load_mobile_units` (the one D-31 reader)."""
    loader = _gate_attr("load_mobile_units")
    return dict(loader()) if loader is not None else {}


def home_stations() -> list[dict[str, Any]]:
    """The depots and their assigned units for the map and the drawer. Membership comes from
    `load_mobile_units`; the display fields (name, lat/lon, charger) are read from the same registry
    file that `selector.gate.resolve_mobile_home_stations_path` locates."""
    resolve = _gate_attr("resolve_mobile_home_stations_path")
    if resolve is None:
        return []
    path = resolve()
    if not path.exists():
        return []
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    units = mobile_units()
    by_station: dict[str, list[str]] = {}
    for assignment in raw.get("assignment", []):
        if str(assignment.get("bank_id")) in units:
            by_station.setdefault(str(assignment["home_station_id"]), []).append(str(assignment["bank_id"]))
    return [
        {
            "home_station_id": str(s["home_station_id"]),
            "name": s.get("name"),
            "zone": s.get("zone"),
            "utility_id": s.get("utility_id") or None,
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "charger_kw": s.get("charger_kw"),
            "notes": s.get("notes"),
            "units": by_station.get(str(s["home_station_id"]), []),
        }
        for s in raw.get("home_station", [])
    ]


_SUBSTATION_KEYS = "SELECT asset_id, bank_id FROM og.asset WHERE asset_class = 'SUBSTATION'"


async def substation_keys(store: Any) -> set[str]:
    """Ids (asset_id and bank_id) of every substation BESS in `og.asset`; empty on a store without the
    fleet read helper or a schema without `og.asset` (a guarded read)."""
    if not isinstance(store, FleetRowsStore):
        return set()
    rows = await _optional_rows(store, _SUBSTATION_KEYS, ())
    return {str(v) for r in rows for v in (r.get("asset_id"), r.get("bank_id")) if v}


def classify_asset(hub_id: str, bank_id: str | None, *, mobile: set[str], substations: set[str]) -> str:
    """The in-process form of `asset_sql` (same rule): a D-31 unit is MOBILE, a hub or bank that is an
    `og.asset` SUBSTATION is UTILITY_SCALE, anything else is a HOME battery."""
    if hub_id in mobile or (bank_id or "") in mobile:
        return "MOBILE"
    if hub_id in substations or (bank_id or "") in substations:
        return "UTILITY_SCALE"
    return "HOME"


def health_sql(th: Thresholds) -> Sql:
    return Sql(_HEALTH_SQL, [th.offline_s, th.online_s])


def where_clause(flt: HubFilter, th: Thresholds) -> Sql:
    """`WHERE ...` for `flt`: hard-coded fragments only, every value bound."""
    out = Sql("1=1")
    parts: list[str] = ["1=1"]
    if flt.zones:
        parts.append("h.zone = ANY(%s)")
        out.params.append(list(flt.zones))
    if flt.bank:
        parts.append("h.bank_id = %s")
        out.params.append(flt.bank)
    if flt.health:
        hs = health_sql(th)
        parts.append(f"{hs.text} = ANY(%s)")
        out.params.extend([*hs.params, list(flt.health)])
    if flt.activity:
        parts.append("(" + " OR ".join(_ACTIVITY_SQL[a] for a in flt.activity) + ")")
    if flt.soc_min is not None:
        parts.append("s.soc_kwh::float8 * 100 >= %s * nullif(h.e_kwh::float8, 0)")
        out.params.append(flt.soc_min)
    if flt.soc_max is not None:
        parts.append("s.soc_kwh::float8 * 100 <= %s * nullif(h.e_kwh::float8, 0)")
        out.params.append(flt.soc_max)
    if flt.q:
        # lower(...) LIKE 'prefix%' is an index range scan on lower(hub_id) text_pattern_ops (0039)
        parts.append("lower(h.hub_id) LIKE %s")
        out.params.append(_like_prefix(flt.q.lower()))
    if flt.hw:
        parts.append(f"{_HW_SQL} = ANY(%s)")
        out.params.append(list(flt.hw))
    if flt.fw:
        parts.append(f"{_FW_SQL} = ANY(%s)")
        out.params.append(list(flt.fw))
    if flt.fw_not:
        parts.append(f"{_FW_SQL} IS DISTINCT FROM %s")
        out.params.append(flt.fw_not)
    if flt.asset_class:
        a = asset_sql(th)
        parts.append(f"{a.text} = ANY(%s)")
        out.params.extend([*a.params, list(flt.asset_class)])
    rated = (("h.e_kwh", flt.e_kwh_min, flt.e_kwh_max), ("h.p_kw", flt.p_kw_min, flt.p_kw_max))
    for column, low, high in rated:
        if low is not None:
            parts.append(f"{column} >= %s")
            out.params.append(low)
        if high is not None:
            parts.append(f"{column} <= %s")
            out.params.append(high)
    if flt.units:
        parts.append(f"{_UNITS_SQL} = ANY(%s)")
        out.params.append(list(flt.units))
    if flt.availability:
        parts.append(f"{AVAILABILITY_SQL} = ANY(%s)")
        out.params.append(list(flt.availability))
    out.text = " AND ".join(parts)
    return out


def _sort_expr(sort: str, th: Thresholds) -> Sql:
    return health_sql(th) if sort == "health" else Sql(_SORT_SQL[sort][0])


def page_query(
    flt: HubFilter,
    th: Thresholds,
    *,
    sort: str,
    descending: bool,
    cursor: tuple[str, Any, str] | None,
    limit: int,
) -> Sql:
    """One keyset page (limit+1 rows, so the caller knows whether there is a next page). `cursor` is
    `("after"|"before", sort value, hub_id)`; a "before" page is read in reverse and flipped back."""
    where = where_clause(flt, th)
    key = _sort_expr(sort, th)
    cast = _SORT_SQL[sort][1]
    # "age" is last_seen_at inverted: ascending age = newest first.
    effective_desc = descending != (sort == "age")
    backwards = cursor is not None and cursor[0] == "before"
    order_desc = effective_desc != backwards
    asset = asset_sql(th)
    params: list[Any] = [*asset.params, *key.params, *where.params]
    text = where.text
    if cursor is not None:
        op = "<" if order_desc else ">"
        text += f" AND ({key.text}, h.hub_id) {op} (%s::{cast}, %s)"
        params.extend([*key.params, cursor[1], cursor[2]])
    direction = "DESC" if order_desc else "ASC"
    sql = (
        f"SELECT {_ROW_COLUMNS}, {asset.text} AS asset_class, {key.text} AS sort_value {_FROM} WHERE {text}"
        f" ORDER BY sort_value {direction}, h.hub_id {direction} LIMIT %s"
    )
    # the key is selected once (sort_value) and compared once in the cursor clause
    params.append(limit + 1)
    return Sql(sql, params)


def selection_query(flt: HubFilter, th: Thresholds, *, limit: int) -> Sql:
    where = where_clause(flt, th)
    return Sql(
        f"SELECT h.hub_id {_FROM} WHERE {where.text} ORDER BY h.hub_id LIMIT %s", [*where.params, limit]
    )


def estimate_query(flt: HubFilter, th: Thresholds) -> Sql:
    if flt.empty:
        return Sql("SELECT greatest(reltuples, 0)::bigint AS n FROM pg_class WHERE oid = 'og.hub'::regclass")
    where = where_clause(flt, th)
    return Sql(f"EXPLAIN (FORMAT JSON) SELECT 1 {_FROM} WHERE {where.text}", where.params)


#: What a fleet summary can be broken down by, and the SQL key over `summary_query`'s filtered rows.
#: SoC buckets are 20 % wide; a hub with no SoC reading is `unknown`, never counted as empty.
_GROUP_SQL: dict[str, str] = {
    "none": "'all'",
    "zone": "zone",
    "availability": "availability",
    "health": "health",
    "asset_class": "asset_class",
    "soc_bucket": (
        "(CASE WHEN soc_pct IS NULL THEN 'unknown' WHEN soc_pct < 20 THEN '0-20'"
        " WHEN soc_pct < 40 THEN '20-40' WHEN soc_pct < 60 THEN '40-60' WHEN soc_pct < 80 THEN '60-80'"
        " ELSE '80-100' END)"
    ),
}
SUMMARY_GROUPS: tuple[str, ...] = tuple(_GROUP_SQL)
SummaryGroup = Literal["none", "zone", "availability", "health", "asset_class", "soc_bucket"]
#: Every group key above has a small closed domain (zones, states, buckets); this is a backstop only.
SUMMARY_GROUPS_MAX = 50
SUMMARY_TOP_MAX = 25
#: A hub counts toward "available" kW/kWh when it can be dispatched now: live health online or stale
#: (02b S6.4: WATCH still takes commands) and its bank AVAILABLE (D-37). Available kWh is the energy
#: above the hub's reserve floor (`og.hub.r_kwh`), never the reserve itself.
_DISPATCHABLE = "(health IN ('online', 'stale') AND availability = 'AVAILABLE')"
SUMMARY_METRICS = (
    "hubs",
    "rated_kwh",
    "rated_kw",
    "soc_kwh",
    "available_hubs",
    "available_kw",
    "available_kwh",
)


def summary_query(flt: HubFilter, th: Thresholds, *, group_by: str, limit: int) -> Sql:
    """Exact aggregates over the hubs matching `flt`, grouped by `group_by` (one row for "none").

    Unlike the table's `approx_total` this is a real count: it scans the filtered set once (bounded
    memory, one row per group). Health and availability are the same live expressions the filters use.
    """
    where = where_clause(flt, th)
    health = health_sql(th)
    asset = asset_sql(th)
    rows = (
        "SELECT h.zone, h.e_kwh, h.r_kwh, h.p_kw, s.soc_kwh,"
        " s.soc_kwh::float8 * 100 / nullif(h.e_kwh::float8, 0) AS soc_pct,"
        f" {health.text} AS health, {AVAILABILITY_SQL} AS availability, {asset.text} AS asset_class"
        f" {_FROM} WHERE {where.text}"
    )
    metrics = (
        "count(*) AS hubs, coalesce(sum(e_kwh), 0) AS rated_kwh,"
        " coalesce(sum(p_kw), 0) AS rated_kw, coalesce(sum(soc_kwh), 0) AS soc_kwh,"
        f" count(*) FILTER (WHERE {_DISPATCHABLE}) AS available_hubs,"
        f" coalesce(sum(p_kw) FILTER (WHERE {_DISPATCHABLE}), 0) AS available_kw,"
        " coalesce(sum(greatest(coalesce(soc_kwh, 0) - coalesce(r_kwh, 0), 0))"
        f" FILTER (WHERE {_DISPATCHABLE}), 0) AS available_kwh"
    )
    key = _GROUP_SQL[group_by]
    # every fragment is one of this module's fixed strings; every caller value is a bound %s
    sql = f"SELECT {key} AS grp, {metrics} FROM ({rows}) f GROUP BY 1 ORDER BY 1 LIMIT %s"  # noqa: S608
    return Sql(sql, [*health.params, *asset.params, *where.params, limit])


def _shape_group(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"key": str(row.get("grp"))}
    for metric in SUMMARY_METRICS:
        value = _num(row.get(metric)) or 0.0
        out[metric] = int(value) if metric.endswith("hubs") else round(value, 1)
    return out


async def fleet_summary(
    store: FleetRowsStore,
    flt: HubFilter,
    th: Thresholds,
    *,
    group_by: str = "none",
    top: int = 5,
    sort: str = "hub",
    descending: bool = False,
) -> dict[str, Any]:
    """Counts and totals for the hubs matching `flt`, an optional breakdown, and the first `top` rows.

    `{total: {hubs, rated_kwh, rated_kw, soc_kwh, available_hubs, available_kw, available_kwh},
    groups: [{key, ...same}], group_by, rows: [table rows], rows_sort}`. Read-only and bounded: two
    aggregate SELECTs (one row per group) and one keyset page of at most `SUMMARY_TOP_MAX` rows."""
    total_q = summary_query(flt, th, group_by="none", limit=1)
    totals = await store.fleet_rows(total_q.text, tuple(total_q.params))
    empty_total = {"grp": "all", **dict.fromkeys(SUMMARY_METRICS, 0)}
    total = _shape_group(totals[0] if totals else empty_total)
    total.pop("key", None)
    groups: list[dict[str, Any]] = []
    if group_by != "none":
        grouped_q = summary_query(flt, th, group_by=group_by, limit=SUMMARY_GROUPS_MAX)
        groups = [_shape_group(g) for g in await store.fleet_rows(grouped_q.text, tuple(grouped_q.params))]
    rows: list[dict[str, Any]] = []
    top = max(0, min(top, SUMMARY_TOP_MAX))
    if top:
        page = page_query(flt, th, sort=sort, descending=descending, cursor=None, limit=top)
        now = datetime.now(UTC)
        rows = [
            shape_hub(r, th, now=now) for r in (await store.fleet_rows(page.text, tuple(page.params)))[:top]
        ]
    return {"total": total, "groups": groups, "group_by": group_by, "rows": rows, "rows_sort": sort}


def home_station_sites() -> dict[str, tuple[float, float]]:
    """`unit id -> (lat, lon)` of its D-31 home station (`selector.gate.load_mobile_home_station_sites`)."""
    loader = _gate_attr("load_mobile_home_station_sites")
    return dict(loader()) if loader is not None else {}


def unit_at_home(
    hub_id: str, bank_id: str | None, lat: Any, lon: Any, sites: dict[str, tuple[float, float]]
) -> bool | None:
    """The ONE D-31 at-home rule (`core.geo.at_home_station`, as G-35 and the selector apply it): True at
    the station, False away, None when the position or the station is unknown."""
    site = sites.get(hub_id) or sites.get(bank_id or "")
    la, lo = _num(lat), _num(lon)
    return geo.at_home_station((la, lo) if la is not None and lo is not None else None, site)


_MOBILE_POSITIONS = "SELECT h.hub_id, h.bank_id, h.lat, h.lon {from_} WHERE {where} AND {asset} = 'MOBILE'"


async def mobile_positions_at_home(
    store: FleetRowsStore, flt: HubFilter, th: Thresholds
) -> list[dict[str, Any]]:
    """Every mobile unit matching `flt`, each with `at_home` (True/False/None). Bounded by the D-31
    registry (a few trucks). The positions stay here: callers get the verdict, never the coordinates."""
    if not th.mobile:
        return []
    where = where_clause(flt, th)
    asset = asset_sql(th)
    sql = _MOBILE_POSITIONS.format(from_=_FROM, where=where.text, asset=asset.text)
    sql += " ORDER BY h.hub_id LIMIT %s"
    rows = await store.fleet_rows(sql, (*where.params, *asset.params, 2 * len(th.mobile)))
    sites = home_station_sites()
    return [
        {
            "hub_id": str(r["hub_id"]),
            "bank_id": r.get("bank_id"),
            "at_home": unit_at_home(str(r["hub_id"]), r.get("bank_id"), r.get("lat"), r.get("lon"), sites),
        }
        for r in rows
    ]


def search_query(kind: str, q: str, *, limit: int) -> Sql:
    like = _like_prefix(q.lower())
    if kind == "hub":
        return Sql(
            "SELECT h.hub_id AS id, h.bank_id, h.zone, h.p_kw AS rated_p_kw, s.fault_code, s.last_seen_at,"
            " s.health AS stored_health FROM og.hub h LEFT JOIN og.hub_state s ON s.hub_id = h.hub_id"
            " WHERE lower(h.hub_id) LIKE %s ORDER BY h.hub_id LIMIT %s",
            [like, limit],
        )
    if kind == "bank":
        return Sql(
            "SELECT b.bank_id AS id, b.zone FROM og.bank b WHERE lower(b.bank_id) LIKE %s"
            " ORDER BY b.bank_id LIMIT %s",
            [like, limit],
        )
    if kind in ("firmware", "hardware"):
        column = _FW_SQL if kind == "firmware" else _HW_SQL
        where = f"{column} IS NOT NULL AND lower({column}) LIKE %s"
        sql = f"SELECT DISTINCT {column} AS id FROM og.hub h WHERE {where} ORDER BY 1 LIMIT %s"  # noqa: S608
        return Sql(sql, [like, limit])  # the column is one of two hard-coded fragments; values are bound
    return Sql(
        "SELECT DISTINCT b.zone AS id FROM og.bank b WHERE b.zone ILIKE %s ORDER BY b.zone LIMIT %s",
        [like, limit],
    )


# -- cursor -----------------------------------------------------------------------------------------


def encode_cursor(direction: str, sort: str, desc: bool, value: Any, hub_id: str) -> str:
    if isinstance(value, datetime):
        value = value.isoformat()
    raw = json.dumps([direction, sort, desc, value, hub_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str, *, sort: str, desc: bool) -> tuple[str, Any, str]:
    """A cursor is only valid for the sort it was issued under; anything else is a 400, never a guess."""
    try:
        padded = token + "=" * (-len(token) % 4)
        direction, c_sort, c_desc, value, hub_id = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid cursor") from exc
    if c_sort != sort or bool(c_desc) != desc or direction not in ("after", "before"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="cursor does not match this sort")
    return str(direction), value, str(hub_id)


# -- store port ---------------------------------------------------------------------------------------


@runtime_checkable
class FleetRowsStore(Protocol):
    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]: ...


def _rows_store(store: Any = Depends(get_store)) -> FleetRowsStore:  # noqa: B008 -- FastAPI dependency
    if not isinstance(store, FleetRowsStore):
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail="store has no fleet read helper")
    return store


def _thresholds(cfg: Annotated[Config, Depends(get_config)]) -> Thresholds:
    return Thresholds.from_config(cfg)


def _filter(
    zone: Annotated[list[str] | None, Query()] = None,
    bank: str | None = None,
    health: Annotated[list[str] | None, Query()] = None,
    activity: Annotated[list[str] | None, Query()] = None,
    soc_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    soc_max: Annotated[float | None, Query(ge=0, le=100)] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    hw: Annotated[list[str] | None, Query()] = None,
    fw: Annotated[list[str] | None, Query()] = None,
    fw_not: Annotated[str | None, Query(max_length=64)] = None,
    asset_class: Annotated[list[str] | None, Query()] = None,
    e_kwh_min: Annotated[float | None, Query(ge=0)] = None,
    e_kwh_max: Annotated[float | None, Query(ge=0)] = None,
    p_kw_min: Annotated[float | None, Query(ge=0)] = None,
    p_kw_max: Annotated[float | None, Query(ge=0)] = None,
    units: Annotated[list[int] | None, Query()] = None,
    availability: Annotated[list[str] | None, Query()] = None,
) -> HubFilter:
    avail = tuple(dict.fromkeys(v.upper() for v in availability or [] if v))
    if any(v not in AVAILABILITIES for v in avail):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"availability must be one of {AVAILABILITIES}"
        )
    states: list[str] = []
    for value in health or []:
        state = HEALTH_STATES.get(value.upper(), value.lower())
        if state not in HEALTH_LABELS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown health {value!r}")
        states.append(state)
    acts = [a.lower() for a in activity or []]
    if any(a not in ACTIVITIES for a in acts):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"activity must be one of {ACTIVITIES}"
        )
    return HubFilter(
        zones=tuple(z for z in zone or [] if z),
        bank=bank or None,
        health=tuple(states),
        activity=tuple(acts),
        soc_min=soc_min,
        soc_max=soc_max,
        q=(q or "").strip() or None,
        hw=tuple(v for v in hw or [] if v),
        fw=tuple(v for v in fw or [] if v),
        fw_not=(fw_not or "").strip() or None,
        asset_class=_asset_classes(asset_class),
        e_kwh_min=e_kwh_min,
        e_kwh_max=e_kwh_max,
        p_kw_min=p_kw_min,
        p_kw_max=p_kw_max,
        units=tuple(sorted({u for u in units or [] if u > 0})),
        availability=avail,
    )


def _asset_classes(values: list[str] | None) -> tuple[str, ...]:
    out = tuple(dict.fromkeys(v.upper() for v in values or [] if v))
    if any(v not in ASSET_CLASSES for v in out):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"asset_class must be one of {ASSET_CLASSES}"
        )
    return out


def shape_hub(row: dict[str, Any], th: Thresholds, *, now: datetime) -> dict[str, Any]:
    """A table row: live health (from last_seen_at), its owner-facing label, SoC %, and age."""
    last_seen = row.get("last_seen_at")
    stored = str(row.get("stored_health") or "").lower()
    health: str = classify_hub_health(
        last_seen_at=last_seen,
        fault_code=row.get("fault_code") or None,
        now=now,
        thresholds=th.health,
    )
    if health != "fault" and stored in ("degraded", "quarantined"):
        health = stored
    e_kwh = _num(row.get("e_kwh"))
    soc_kwh = _num(row.get("soc_kwh"))
    out = {k: v for k, v in row.items() if k not in ("sort_value", "stored_health")}
    out.update(
        health=health,
        health_label=HEALTH_LABELS.get(health, health.upper()),
        soc_pct=round(soc_kwh / e_kwh * 100, 1) if soc_kwh is not None and e_kwh else None,
        age_s=round((now - last_seen).total_seconds(), 1) if isinstance(last_seen, datetime) else None,
    )
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "is_finite"):  # Decimal
            out[key] = float(value)
    return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _estimate(store: FleetRowsStore, flt: HubFilter, th: Thresholds) -> int | None:
    query = estimate_query(flt, th)
    rows = await store.fleet_rows(query.text, tuple(query.params))
    if not rows:
        return None
    row = rows[0]
    if "n" in row:
        return int(row["n"])
    plan = next(iter(row.values()))
    if isinstance(plan, str):
        plan = json.loads(plan)
    try:
        return int(plan[0]["Plan"]["Plan Rows"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# -- endpoints ----------------------------------------------------------------------------------------


@router.get("/table")
async def hub_table(
    store: Annotated[FleetRowsStore, Depends(_rows_store)],
    th: Annotated[Thresholds, Depends(_thresholds)],
    flt: Annotated[HubFilter, Depends(_filter)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    sort: SortKey = "hub",
    dir: Literal["asc", "desc"] = "asc",
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One keyset page of hubs: `{items, next_cursor, prev_cursor, approx_total, limit, sort, dir}`."""
    if limit not in PAGE_SIZES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"limit must be one of {PAGE_SIZES}")
    desc = dir == "desc"
    parsed = decode_cursor(cursor, sort=sort, desc=desc) if cursor else None
    query = page_query(flt, th, sort=sort, descending=desc, cursor=parsed, limit=limit)
    rows = await store.fleet_rows(query.text, tuple(query.params))
    more = len(rows) > limit
    rows = rows[:limit]
    backwards = parsed is not None and parsed[0] == "before"
    if backwards:
        rows.reverse()
    now = datetime.now(UTC)
    items = [shape_hub(r, th, now=now) for r in rows]
    next_cursor = prev_cursor = None
    if rows:
        first, last = rows[0], rows[-1]
        if more or backwards:
            next_cursor = encode_cursor("after", sort, desc, last.get("sort_value"), str(last["hub_id"]))
        if parsed is not None and (more or not backwards):
            prev_cursor = encode_cursor("before", sort, desc, first.get("sort_value"), str(first["hub_id"]))
    return {
        "items": items,
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "approx_total": await _estimate(store, flt, th),
        "total_is_estimate": True,
        "limit": limit,
        "sort": sort,
        "dir": dir,
    }


@router.get("/summary")
async def summary(
    store: Annotated[FleetRowsStore, Depends(_rows_store)],
    th: Annotated[Thresholds, Depends(_thresholds)],
    flt: Annotated[HubFilter, Depends(_filter)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    group_by: SummaryGroup = "none",
    top: Annotated[int, Query(ge=0, le=SUMMARY_TOP_MAX)] = 5,
    sort: SortKey = "hub",
    dir: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    """Exact counts and totals for the hubs matching the Fleet filters (`fleet_summary`): how many, rated
    kWh/kW, SoC kWh, and what is dispatchable now (available kW / kWh above reserve), optionally by zone,
    availability, health, asset class or SoC bucket, plus the first `top` rows."""
    return await fleet_summary(
        store, flt, th, group_by=group_by, top=top, sort=sort, descending=dir == "desc"
    )


@router.get("/selection")
async def select_matching(
    store: Annotated[FleetRowsStore, Depends(_rows_store)],
    th: Annotated[Thresholds, Depends(_thresholds)],
    cfg: Annotated[Config, Depends(get_config)],
    flt: Annotated[HubFilter, Depends(_filter)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    max: Annotated[int | None, Query(gt=0)] = None,
) -> dict[str, Any]:
    """Every hub id matching the filter, capped at `[api].fleet_selection_max` (default 5,000):
    `{hub_ids, count, capped, max}`. `capped` means more hubs match than were returned."""
    cap = int(cfg.get("api.fleet_selection_max", SELECTION_MAX_DEFAULT))
    cap = min(max, cap) if max else cap
    query = selection_query(flt, th, limit=cap + 1)
    rows = await store.fleet_rows(query.text, tuple(query.params))
    ids = [str(r["hub_id"]) for r in rows[:cap]]
    return {"hub_ids": ids, "count": len(ids), "capped": len(rows) > cap, "max": cap}


@router.get("/search")
async def search(
    store: Annotated[FleetRowsStore, Depends(_rows_store)],
    th: Annotated[Thresholds, Depends(_thresholds)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    kind: Literal["hub", "bank", "zone", "firmware", "hardware"],
    q: Annotated[str, Query(max_length=64)] = "",
    limit: Annotated[int, Query(gt=0, le=SEARCH_LIMIT_MAX)] = 20,
) -> dict[str, Any]:
    """Typeahead: `{items: [{id, ...}]}` by case-insensitive prefix. Hubs carry bank, zone, live health
    and rated kW (the command form's setpoint hint); banks carry their zone."""
    query = search_query(kind, q.strip(), limit=limit)
    rows = await store.fleet_rows(query.text, tuple(query.params))
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {"id": row["id"]}
        if kind in ("hub", "bank"):
            item["zone"] = row.get("zone")
        if kind == "hub":
            shaped = shape_hub({**row, "hub_id": row["id"]}, th, now=now)
            item.update(
                bank_id=row.get("bank_id"),
                health=shaped["health"],
                health_label=shaped["health_label"],
                rated_p_kw=_num(row.get("rated_p_kw")),
            )
        items.append(item)
    return {"kind": kind, "q": q, "items": items}


@router.get("/release-requests")
async def pending_release_requests(
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """PENDING safe-stop release requests (operator A has filed, no second operator has approved yet):
    `{items: [{proposal_id, scope, scope_ref, requested_by, reason, age_s}]}`, newest first. Expired
    requests are not listed (`ProposalStore.peek` drops them)."""
    now = time.monotonic()
    items: list[dict[str, Any]] = []
    for proposal in list(proposals._proposals.values()):
        if proposal.kind != _RELEASE_PROPOSAL_KIND or proposal.expired(now=now):
            continue
        body = proposal.body
        items.append(
            {
                "proposal_id": str(proposal.proposal_id),
                "scope": getattr(body, "scope_kind", None),
                "scope_ref": getattr(body, "scope_ref", None),
                "requested_by": getattr(body, "requested_by", proposal.proposer),
                "reason": getattr(body, "reason", None),
                "age_s": round(now - proposal.created_at, 1),
            }
        )
    items.sort(key=lambda i: i["age_s"])
    return {"items": items}


_DETAIL_HUB = (
    "SELECT to_jsonb(h.*) AS hub, to_jsonb(s.*) AS state, to_jsonb(b.*) AS bank"
    " FROM og.hub h LEFT JOIN og.hub_state s ON s.hub_id = h.hub_id"
    " LEFT JOIN og.bank b ON b.bank_id = h.bank_id WHERE h.hub_id = %s"
)
_DETAIL_TELEMETRY = (
    "SELECT to_jsonb(t.*) AS row FROM og.telemetry t WHERE t.hub_id = %s ORDER BY t.ts DESC LIMIT 1"
)
_DETAIL_ALERTS = (
    "SELECT to_jsonb(a.*) AS row FROM og.alert a"
    " WHERE a.scope_ref IN (%s, %s) ORDER BY a.opened_at DESC LIMIT 5"
)
_DETAIL_OBLIGATIONS = (
    "SELECT o.obligation_id::text AS obligation_id, o.service_type, o.contract_id::text AS contract_id"
    " FROM og.reservation r JOIN og.obligation o ON o.obligation_id = r.obligation_id"
    " WHERE r.bank_id = %s AND o.state = 'DELIVERING' AND now() BETWEEN r.interval_start AND r.interval_end"
    " LIMIT 3"
)
_DETAIL_CALIBRATION = (
    "SELECT to_jsonb(c.*) AS row FROM og.calibration_command c WHERE c.hub_id = %s"
    " ORDER BY c.created_at DESC NULLS LAST LIMIT 1"
)
_DETAIL_SUBSTATION = (
    "SELECT to_jsonb(a.*) AS row FROM og.asset a WHERE a.asset_class = 'SUBSTATION'"
    " AND (a.asset_id = %s OR a.bank_id = %s) LIMIT 1"
)


async def _asset_detail(
    store: FleetRowsStore, th: Thresholds, hub_id: str, hub: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """(asset_class, truck block, substation block) for the drawer. A truck's charging is allowed only
    at its home station (D-31); the gate has no deployment schedule yet, so it fails closed."""
    bank_id = str(hub.get("bank_id") or "")
    if hub_id in th.mobile or bank_id in th.mobile:
        station = next((s for s in home_stations() if hub_id in s["units"] or bank_id in s["units"]), None)
        lat, lon = _num(hub.get("lat")), _num(hub.get("lon"))
        site: dict[str, tuple[float, float]] = {}
        if station is not None and station.get("lat") is not None and station.get("lon") is not None:
            site[hub_id] = (float(station["lat"]), float(station["lon"]))
        at_home = unit_at_home(hub_id, bank_id, lat, lon, site) is True
        return (
            "MOBILE",
            {
                "home_station": station,
                "location": {"lat": lat, "lon": lon},
                "status": "AT_HOME_STATION" if at_home else "AWAY",
                "charging_allowed": False,
                "charging_note": "Charges only at its home station (D-31); held off until a deployment "
                "schedule exists (the gate fails closed).",
                "next_return": None,
            },
            None,
        )
    rows = await _optional_rows(store, _DETAIL_SUBSTATION, (hub_id, bank_id))
    if rows:
        a = dict(rows[0]["row"])
        p_kw, e_kwh = _num(a.get("p_kw")), _num(a.get("e_kwh"))
        return (
            "UTILITY_SCALE",
            None,
            {
                "asset_id": a.get("asset_id"),
                "mw": round(p_kw / 1000, 3) if p_kw is not None else None,
                "mwh": round(e_kwh / 1000, 3) if e_kwh is not None else None,
                "poi_import_kva": _num(a.get("poi_import_kva")),
                "poi_export_kva": _num(a.get("poi_export_kva")),
                "feeder_id": a.get("feeder_id"),
                "substation_id": a.get("substation_id"),
                "status": a.get("status"),
            },
        )
    return "HOME", None, None


_DETAIL_COMMAND = (
    "SELECT to_jsonb(v.*) AS verdict FROM og.verdict v WHERE v.command_batch_id::text = %s LIMIT 1"
)
#: Telemetry fields the drawer shows first; the rest stays in the raw JSON.
TELEMETRY_KEYS = (
    "ts",
    "p_kw",
    "soc_kwh",
    "cell_temp_c",
    "p_dis_max_kw",
    "p_ch_max_kw",
    "meter_kw",
    "pv_kw",
    "home_load_kw",
    "charge_pv_kw",
    "charge_grid_kw",
    "fault_code",
)
#: Asset columns that may not exist yet (FOLLOWUPS migration 0035); read if present.
ASSET_KEYS = ("installed_at", "last_serviced_at", "units", "feeder_id", "service_transformer_id")
#: Battery-reported DEVICE-INFO columns (og.hub, FOLLOWUPS migration 0036); HW/FW first.
DEVICE_INFO_KEYS = (
    "hardware_revision",
    "firmware_version",
    "serial_number",
    "manufacturer",
    "model",
    "commissioned_at",
    "inverter_model",
)


async def _optional_rows(store: FleetRowsStore, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """A guarded read: a table this deployment does not have yet yields no rows, not a 500."""
    try:
        return await store.fleet_rows(sql, params)
    except Exception:
        return []


@router.get("/hubs/{hub_id}/detail")
async def hub_detail(
    hub_id: str,
    store: Annotated[FleetRowsStore, Depends(_rows_store)],
    th: Annotated[Thresholds, Depends(_thresholds)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """Everything the Fleet drawer shows for one hub in one call: status (live health from
    last_seen_at), the last telemetry message, recent alerts for the hub and its bank, location, asset
    and control. Columns that do not exist yet come back `null` ("not recorded")."""
    base = await store.fleet_rows(_DETAIL_HUB, (hub_id,))
    if not base:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub not found")
    hub = dict(base[0].get("hub") or {})
    state = dict(base[0].get("state") or {})
    bank = dict(base[0].get("bank") or {})
    last_seen = _parse_ts(state.get("last_seen_at"))
    now = datetime.now(UTC)
    shaped = shape_hub(
        {
            "hub_id": hub_id,
            "e_kwh": hub.get("e_kwh"),
            "soc_kwh": state.get("soc_kwh"),
            "fault_code": state.get("fault_code"),
            "stored_health": state.get("health"),
            "last_seen_at": last_seen,
        },
        th,
        now=now,
    )
    p_kw = _num(state.get("p_kw"))
    leased = _parse_ts(state.get("lease_expires_at"))
    activity = _activity(p_kw, leased is not None and leased > now, shaped["health"])
    obligations: list[dict[str, Any]] = []
    if activity == "delivering" and hub.get("bank_id"):
        obligations = await _optional_rows(store, _DETAIL_OBLIGATIONS, (str(hub["bank_id"]),))
    telemetry = await _optional_rows(store, _DETAIL_TELEMETRY, (hub_id,))
    raw_telemetry = dict(telemetry[0]["row"]) if telemetry else None
    alerts = await _optional_rows(store, _DETAIL_ALERTS, (hub_id, str(hub.get("bank_id") or "")))
    calibration = await _optional_rows(store, _DETAIL_CALIBRATION, (hub_id,))
    verdict: dict[str, Any] | None = None
    if state.get("last_command_id"):
        found = await _optional_rows(store, _DETAIL_COMMAND, (str(state["last_command_id"]),))
        verdict = dict(found[0]["verdict"]) if found else None
    e_kwh, r_kwh, soc_kwh = _num(hub.get("e_kwh")), _num(hub.get("r_kwh")), _num(state.get("soc_kwh"))
    merged = {**bank, **hub}
    asset_class, mobile, utility = await _asset_detail(store, th, hub_id, hub)
    return {
        "hub_id": hub_id,
        "asset_class": asset_class,
        "mobile": mobile,
        "utility_scale": utility,
        "status": {
            "health": shaped["health"],
            "health_label": shaped["health_label"],
            "online": shaped["health"] in ("online", "stale"),
            "last_seen_at": last_seen.isoformat() if last_seen else None,
            "age_s": shaped["age_s"],
            "activity": activity,
            "serving": obligations,
            "p_kw": p_kw,
            "soc_kwh": soc_kwh,
            "soc_pct": shaped["soc_pct"],
            "reserve_kwh": r_kwh,
            "reserve_pct": round(r_kwh / e_kwh * 100, 1) if r_kwh is not None and e_kwh else None,
            "above_reserve_kwh": round(soc_kwh - r_kwh, 2)
            if soc_kwh is not None and r_kwh is not None
            else None,
            "fault_code": state.get("fault_code"),
        },
        "telemetry": {
            "ts": raw_telemetry.get("ts") if raw_telemetry else None,
            "fields": {k: raw_telemetry.get(k) for k in TELEMETRY_KEYS if k in raw_telemetry}
            if raw_telemetry
            else {},
            "raw": raw_telemetry,
        },
        "alerts": [dict(a["row"]) for a in alerts],
        "location": {
            "lat": _num(hub.get("lat")),
            "lon": _num(hub.get("lon")),
            "zone": hub.get("zone"),
            "bank_id": hub.get("bank_id"),
            "feeder_id": merged.get("feeder_id"),
            "service_transformer_id": merged.get("service_transformer_id"),
        },
        "asset": {
            "installed_at": merged.get("installed_at"),
            "last_serviced_at": merged.get("last_serviced_at"),
            "units": merged.get("units"),
            "rated_p_kw": _num(hub.get("p_kw")),
            "rated_e_kwh": e_kwh,
            "reserve_kwh": r_kwh,
            "last_calibration": dict(calibration[0]["row"]) if calibration else None,
            # battery-reported DEVICE-INFO (og.hub, migration 0036) when present; `null` until then
            "device_info": {k: merged.get(k) for k in DEVICE_INFO_KEYS if merged.get(k) is not None},
            "device_info_at": merged.get("device_info_at"),
        },
        "control": {
            "lease_epoch": state.get("lease_epoch"),
            "lease_expires_at": state.get("lease_expires_at"),
            "last_command_id": state.get("last_command_id"),
            "last_command_verdict": verdict,
        },
    }


def _activity(p_kw: float | None, leased: bool, health: str) -> str:
    if health == "fault":
        return "fault"
    if p_kw is None or abs(p_kw) <= IDLE_KW:
        return "idle"
    if p_kw > 0:  # +charge / -discharge
        return "charging"
    return "delivering" if leased else "serving_home"


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@router.get("/home-stations")
async def list_home_stations(_identity: Annotated[Identity, Depends(require_viewer)]) -> dict[str, Any]:
    """D-31 depots with their assigned mobile units (for the map's depot layer and truck lines)."""
    return {"items": home_stations()}
