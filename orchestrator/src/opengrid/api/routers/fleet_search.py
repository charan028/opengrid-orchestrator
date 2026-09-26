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
  `og.hub_state (last_seen_at, hub_id)`, `og.hub_state (p_kw, hub_id)`); without one Postgres sorts the
  filtered set once per page (top-N heapsort, bounded memory).
* The total is an estimate: `pg_class.reltuples` with no filter, else the planner's row estimate from
  `EXPLAIN (FORMAT JSON)` -- never a `count(*)` over the fleet.
* Search is a case-insensitive prefix match (`ILIKE 'q%'`). It is an index range scan once
  `lower(hub_id) text_pattern_ops` / `lower(bank_id) text_pattern_ops` indexes exist; until then it is a
  sequential scan of `og.hub` bounded by `LIMIT`.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_config, get_proposals, get_store
from opengrid.api.proposals import ProposalStore
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
#: |P| at or under this is idle; above it the sign says discharging (+) or charging (-).
IDLE_KW = 0.1
_RELEASE_PROPOSAL_KIND = "safestop-release"  # api.routers.safestop's proposal kind for a release request

SortKey = Literal["hub", "bank", "zone", "soc", "kw", "health", "age"]
#: sort key -> (SQL expression, cast for the bound cursor value). NULLs are coalesced so the row-value
#: comparison is total. "age" sorts on last_seen_at inverted, so ascending age = most recent first.
_SORT_SQL: dict[str, tuple[str, str]] = {
    "hub": ("h.hub_id", "text"),
    "bank": ("coalesce(h.bank_id, '')", "text"),
    "zone": ("coalesce(h.zone, '')", "text"),
    "soc": ("coalesce(s.soc_kwh::float8 / nullif(h.e_kwh::float8, 0), -1)", "float8"),
    "kw": ("coalesce(s.p_kw::float8, -1e18)", "float8"),
    "health": ("{health}", "text"),
    "age": ("coalesce(s.last_seen_at, 'epoch'::timestamptz)", "timestamptz"),
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
    "delivering": f"(s.p_kw > {IDLE_KW} AND {_LEASED})",
    "serving_home": f"(s.p_kw > {IDLE_KW} AND NOT {_LEASED})",
    "charging": f"(s.p_kw < -{IDLE_KW})",
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
    f" s.last_command_id, {_ACTIVITY_CASE} AS activity"
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

    @property
    def empty(self) -> bool:
        return not (
            self.zones
            or self.bank
            or self.health
            or self.activity
            or self.soc_min is not None
            or self.soc_max is not None
            or self.q
        )


@dataclass(frozen=True, slots=True)
class Thresholds:
    online_s: float
    offline_s: float

    @classmethod
    def from_config(cls, cfg: Config) -> Thresholds:
        t = HealthThresholds.from_config(cfg)
        return cls(online_s=float(t.hub_online_s), offline_s=float(t.hub_offline_s))


@dataclass(slots=True)
class Sql:
    text: str
    params: list[Any] = field(default_factory=list)


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


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
        parts.append("h.hub_id ILIKE %s")
        out.params.append(_like_prefix(flt.q))
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
    params: list[Any] = [*key.params, *where.params]
    text = where.text
    if cursor is not None:
        op = "<" if order_desc else ">"
        text += f" AND ({key.text}, h.hub_id) {op} (%s::{cast}, %s)"
        params.extend([*key.params, cursor[1], cursor[2]])
    direction = "DESC" if order_desc else "ASC"
    sql = (
        f"SELECT {_ROW_COLUMNS}, {key.text} AS sort_value {_FROM} WHERE {text}"
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


def search_query(kind: str, q: str, *, limit: int) -> Sql:
    like = _like_prefix(q)
    if kind == "hub":
        return Sql(
            "SELECT h.hub_id AS id, h.bank_id, h.zone, h.p_kw AS rated_p_kw, s.fault_code, s.last_seen_at,"
            " s.health AS stored_health FROM og.hub h LEFT JOIN og.hub_state s ON s.hub_id = h.hub_id"
            " WHERE h.hub_id ILIKE %s ORDER BY h.hub_id LIMIT %s",
            [like, limit],
        )
    if kind == "bank":
        return Sql(
            "SELECT b.bank_id AS id, b.zone FROM og.bank b WHERE b.bank_id ILIKE %s ORDER BY b.bank_id LIMIT %s",
            [like, limit],
        )
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
) -> HubFilter:
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
    )


def shape_hub(row: dict[str, Any], th: Thresholds, *, now: datetime) -> dict[str, Any]:
    """A table row: live health (from last_seen_at), its owner-facing label, SoC %, and age."""
    last_seen = row.get("last_seen_at")
    stored = str(row.get("stored_health") or "").lower()
    health: str = classify_hub_health(
        last_seen_at=last_seen,
        fault_code=row.get("fault_code") or None,
        now=now,
        thresholds=HealthThresholds(telemetry_interval_s=th.online_s / 2, hub_offline_s=th.offline_s),
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
    kind: Literal["hub", "bank", "zone"],
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
ASSET_KEYS = ("install_date", "last_serviced_at", "units", "feeder_id", "service_transformer_id")
#: Battery-reported DEVICE-INFO columns (og.hub, FOLLOWUPS migration 0036; names provisional).
DEVICE_INFO_KEYS = (
    "serial_number",
    "manufacturer",
    "model",
    "firmware_version",
    "hardware_rev",
    "commissioned_at",
    "inverter_model",
    "reserve_pct",
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
    return {
        "hub_id": hub_id,
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
            "install_date": merged.get("install_date"),
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
    if p_kw < 0:
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
