"""Screen 2: Fleet monitoring & control (`/og/fleet`, 02b S8 row 2). Owner: ui-a (BUILD.md S4).

Bank/hub table with drill-down (SoC, P, health, lease, last command). Manual command and scoped safe
stop are two-step confirmations *mediated* by this module (BUILD.md code-review round items 1-2): a
`<form>` posts to a UI-owned `.../propose` route below, which relays the proposal to `opengrid.api`
server-side (`opengrid.ui.api_client.post_json`) and renders `_partials/confirm_dialog.html` populated
with the real `proposal_id`/`summary`/`expires_in_s` from the API's response; only the dialog's own
confirm button then posts to a second UI-owned `.../{proposal_id}/confirm` route, which relays the
confirmation and renders a pass/veto/timeout/expired result fragment. The template never talks to
`opengrid.api` directly any more -- see `templates/fleet.html` and the base contract README.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from opengrid.core.timeutil import to_utc
from opengrid.ui.api_client import ApiUnavailable, delete_json, get_json, post_json, put_json
from opengrid.ui.render import render_stale_badge, render_status_badge
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/fleet")

_SAFESTOP_PROPOSE_PATH = "/og/api/safestop"
_COMMAND_PROPOSE_PATH = "/og/api/fleet/command"
_BULK_COMMAND_PATH = "/og/api/fleet/commands/bulk"
_MAP_PATH = "/og/api/fleet/map"
# owner review R3: the table at scale (api `routers.fleet_search`)
_TABLE_PATH = "/og/api/fleet/table"
_SEARCH_PATH = "/og/api/fleet/search"
_SELECTION_PATH = "/og/api/fleet/selection"
_RELEASES_PATH = "/og/api/fleet/release-requests"
_SUMMARY_PATH = "/og/api/fleet/summary"
_LEGACY_HUBS_PATH = "/og/api/fleet/hubs"
# R3.1: operator manual targets (ramped by the engine) and the grid-charging schedule (FOLLOWUPS' API)
_TARGETS_PATH = "/og/api/fleet/manual-targets"
_CHARGE_WINDOWS_PATH = "/og/api/fleet/charge-windows"
_HOME_STATIONS_PATH = "/og/api/fleet/home-stations"
#: Owner asset classes (R3.1): shape + colour on the map, a column, a filter and a drawer badge.
ASSET_LABELS: dict[str, str] = {"HOME": "Home battery", "MOBILE": "Truck", "UTILITY_SCALE": "Substation BESS"}
DEFAULT_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 240
#: D-30: most specific wins, Fleet < Provider < Zone < Substation < Feeder < Bank < Hub.
CHARGE_SCOPES: tuple[tuple[str, str], ...] = (
    ("FLEET", "Fleet"),
    ("PROVIDER", "Provider"),
    ("ZONE", "Zone"),
    ("SUBSTATION", "Substation"),
    ("FEEDER", "Feeder"),
    ("BANK", "Bank"),
    ("HUB", "Hub"),
)
_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_WINDOWS = 4
PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
#: "Select all N matching" cap; the API applies its own `[api].fleet_selection_max` on top.
SELECTION_MAX = 5000
HEALTH_CHOICES = ("OK", "WATCH", "DEGRADED", "QUARANTINED", "FAULT", "OFFLINE")
ACTIVITY_LABELS: dict[str, str] = {
    "delivering": "Delivering",
    "serving_home": "Serving home",
    "charging": "Charging",
    "idle": "Idle",
}
#: sortable column key -> header label (unit in the header, UI-UX spec S5.8)
SORT_COLUMNS: dict[str, str] = {
    "hub": "Hub",
    "bank": "Bank",
    "zone": "Zone",
    "soc": "SoC (%)",
    "kw": "P (kW, +chg/\u2212dis)",
    "health": "Health",
    "age": "Telemetry age",
    "hw": "HW rev",
    "fw": "FW version",
}
_HUB_STALE_AFTER_S = 10.0
#: The API holds the approval open up to 10 s waiting for the guardian (api `routers.safestop`).
_RELEASE_APPROVE_TIMEOUT_S = 15.0
_SAFESTOP_SCOPES = ("fleet", "zone", "bank")


def safestop_prefill(scope: str | None, scope_id: str | None) -> dict[str, str] | None:
    """Values for the safe-stop form when an operator follows a guardian safe-stop request
    (`opengrid.ui.routes.health.guardian_attention`). Only fills the step-1 form: the operator still
    proposes and then confirms the API's summary (K8); an unknown scope prefills nothing."""
    if scope not in _SAFESTOP_SCOPES:
        return None
    return {
        "scope": scope,
        "scope_id": (scope_id or "") if scope != "fleet" else "",
        "reason": "Guardian escalation: safe stop requested",
    }


def _require_operator(request: Request) -> None:
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (datetime.now(UTC) - to_utc(parsed)).total_seconds()


def map_hub(hub: dict[str, Any]) -> dict[str, Any]:
    """The compact hub shape `static/og-map.js` draws. Mirrors `GET /og/api/fleet/map`'s documented
    fields (CR #19) so the map needs no change when that endpoint lands: until it does, `lat`/`lon` are
    absent and the module scatters the hub inside its real load zone, and `activity` is derived from
    health and the sign of `p_kw`."""
    return {
        "hub_id": hub.get("hub_id"),
        "bank_id": hub.get("bank_id"),
        "zone": hub.get("zone"),
        "health": hub.get("health"),
        "activity": hub.get("activity"),
        "kw": hub.get("kw", hub.get("p_kw")),
        "soc_kwh": hub.get("soc_kwh"),
        "soc_pct": hub.get("soc_pct"),
        "lat": hub.get("lat"),
        "lon": hub.get("lon"),
        "serving_obligations": hub.get("serving_obligations") or [],
        "can_serve_services": hub.get("can_serve_services") or [],
        "asset_class": hub.get("asset_class") or "HOME",
        "rated_p_kw": hub.get("rated_p_kw", hub.get("rated_kw")),
    }


def _serving_label(obligation: dict[str, Any]) -> str:
    """ "DATA_CENTER c6" -- the service and who it is for, the way CR #19 words the warning."""
    service = str(obligation.get("service_type") or "an obligation")
    customer = obligation.get("customer_id") or obligation.get("obligation_id")
    return f"{service} {str(customer)[:8]}" if customer else service


def bulk_risk_reasons(hubs: list[dict[str, Any]], hub_ids: list[str]) -> list[str]:
    """Why a bulk manual command over `hub_ids` needs the second confirmation (CR #19 item 2): any
    selected hub that is serving a customer, or that is in a critical/failure state. Returns one plain
    sentence per group, e.g. `3 hubs serving DATA_CENTER c6`; an empty list means the ordinary two-step
    confirm is enough. The API's own `requires_double_confirm` is honoured on top of this -- this is the
    console's independent read of the same rule, so the operator sees the reason even before proposing."""
    selected = set(hub_ids)
    chosen = [h for h in hubs if h.get("hub_id") in selected]
    serving: dict[str, int] = {}
    faulted = 0
    delivering = 0
    for hub in chosen:
        obligations = hub.get("serving_obligations") or []
        if obligations:
            for obligation in obligations:
                label = _serving_label(obligation)
                serving[label] = serving.get(label, 0) + 1
        elif float(hub.get("p_kw") or hub.get("kw") or 0) < -0.1:  # +charge / -discharge
            delivering += 1
        if str(hub.get("health") or "").lower() in ("fault", "offline"):
            faulted += 1
    reasons = [
        f"{count} hub{'' if count == 1 else 's'} serving {label}"
        for label, count in sorted(serving.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if delivering:
        reasons.append(f"{delivering} hub{'' if delivering == 1 else 's'} currently delivering power")
    if faulted:
        reasons.append(f"{faulted} hub{'' if faulted == 1 else 's'} in a fault or offline state")
    return reasons


def map_hubs_from(payload: Any) -> list[dict[str, Any]]:
    """Hubs of a `GET /og/api/fleet/map` body (`{"hubs": [...], "activity_counts", ...}`), in the compact
    shape the map draws; `[]` for anything else (the caller then draws from the hub list)."""
    items = payload.get("hubs", payload.get("items")) if isinstance(payload, dict) else payload
    return [map_hub(h) for h in items if isinstance(h, dict)] if isinstance(items, list) else []


#: `POST /og/api/fleet/commands/bulk` double-confirm reason codes (api `routers.fleet_bulk`), in words.
_BULK_REASON_TEXT: dict[str, str] = {
    "SERVES_COMMITTED_OBLIGATION": "serving a committed obligation",
    "HUB_FAULT": "in a fault state",
    "CRITICAL_ALERT": "under a critical alert",
    "AT_OR_BELOW_RESERVE": "at or below its backup reserve",
}


def api_double_confirm_reasons(body: dict[str, Any] | None) -> list[str]:
    """The API's own double-confirm reasons (`double_confirm_reasons`, per hub) as plain sentences, e.g.
    `1 hub serving a committed obligation (ERCOT_ENERGY)`. These are authoritative; the console's
    `bulk_risk_reasons` is only OR-ed on top, never used in their place."""
    counts: dict[str, int] = {}
    services: dict[str, set[str]] = {}
    for entry in (body or {}).get("double_confirm_reasons") or []:
        for code in entry.get("reasons") or []:
            counts[code] = counts.get(code, 0) + 1
            if code == "SERVES_COMMITTED_OBLIGATION":
                for obligation in entry.get("obligations") or []:
                    if obligation.get("service_type"):
                        services.setdefault(code, set()).add(str(obligation["service_type"]))
    out = []
    for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        detail = f" ({', '.join(sorted(services[code]))})" if code in services else ""
        out.append(f"{n} hub{'' if n == 1 else 's'} {_BULK_REASON_TEXT.get(code, code)}{detail}")
    return out


def api_error_result(exc: ApiUnavailable) -> dict[str, Any] | None:
    """The result carried by an API error body. FastAPI wraps `HTTPException(409, detail=result)` as
    `{"detail": result}`, so a guardian veto arrives nested; unwrap it so the fragment renders the veto
    (VETOED / PARTLY_VETOED and its rule ids) instead of a generic failure."""
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("detail"), dict):
        return dict(detail["detail"])
    return detail if isinstance(detail, dict) and ("outcome" in detail or "status" in detail) else None


def parse_hub_ids(raw: str | None) -> list[str]:
    """The selection posted by the Fleet map/table: comma-separated hub ids, de-duplicated, order kept."""
    seen: dict[str, None] = {}
    for part in (raw or "").split(","):
        hub_id = part.strip()
        if hub_id:
            seen.setdefault(hub_id, None)
    return list(seen)


def _to_table_row(hub: dict[str, Any]) -> dict[str, Any]:
    health = hub.get("health", "unknown")
    last_seen_at = hub.get("last_seen_at")
    return {
        "hub_id": hub.get("hub_id", "-"),
        "bank_id": hub.get("bank_id", "-"),
        "zone": hub.get("zone", "-"),
        "health_badge": render_status_badge(health),
        "soc_kwh": hub.get("soc_kwh", "-"),
        "p_kw": hub.get("p_kw", "-"),
        "age_badge": render_stale_badge(
            _age_s(last_seen_at), since_iso=last_seen_at, stale_after_s=_HUB_STALE_AFTER_S
        ),
    }


def _confirm_dialog_context(
    *,
    dialog_id: str,
    title: str,
    proposal: dict[str, Any],
    confirm_url: str,
    confirm_label: str,
    variant: str,
    target: str,
) -> dict[str, Any]:
    """Shared shape for a step-1 `ProposalAccepted` response rendered as an already-open
    `_partials/confirm_dialog.html` fragment (BUILD.md code-review round item 3: real proposal data, not
    a static hand-built summary)."""
    return {
        "dialog_id": dialog_id,
        "open_default": True,
        "show_trigger": False,
        "title": title,
        "summary": proposal.get("summary", ""),
        "confirm_url": confirm_url,
        "confirm_label": confirm_label,
        "variant": variant,
        "target": target,
        "expires_in_s": proposal.get("expires_in_s"),
    }


@dataclass(frozen=True, slots=True)
class TableState:
    """The Fleet table's whole state, carried in the URL query so a view is shareable and survives a
    refresh: filters, sort, page size and the keyset cursor (owner review R3)."""

    zones: tuple[str, ...] = ()
    bank: str = ""
    health: tuple[str, ...] = ()
    activity: tuple[str, ...] = ()
    soc_min: str = ""
    soc_max: str = ""
    q: str = ""
    hw: tuple[str, ...] = ()
    fw: tuple[str, ...] = ()
    fw_not: str = ""
    asset: tuple[str, ...] = ()
    sort: str = "hub"
    dir: str = "asc"
    size: int = DEFAULT_PAGE_SIZE
    cursor: str = ""

    def filter_params(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = [("zone", z) for z in self.zones]
        if self.bank:
            out.append(("bank", self.bank))
        out += [("health", h) for h in self.health]
        out += [("activity", a) for a in self.activity]
        for key in ("soc_min", "soc_max", "q"):
            if getattr(self, key):
                out.append((key, getattr(self, key)))
        out += [("hw", v) for v in self.hw]
        out += [("fw", v) for v in self.fw]
        if self.fw_not:
            out.append(("fw_not", self.fw_not))
        out += [("asset_class", v) for v in self.asset]
        return out

    def view_params(self) -> list[tuple[str, str]]:
        """Filters plus sort/size (no cursor): changing any of them starts again at page 1."""
        out = self.filter_params()
        if self.sort != "hub" or self.dir != "asc":
            out += [("sort", self.sort), ("dir", self.dir)]
        if self.size != DEFAULT_PAGE_SIZE:
            out.append(("size", str(self.size)))
        return out

    def url(self, **changes: Any) -> str:
        state = replace(self, **changes)
        params = state.view_params() + ([("cursor", state.cursor)] if state.cursor else [])
        return f"{BASE_PATH}/fleet" + (f"?{urlencode(params)}" if params else "")

    def sort_url(self, key: str) -> str:
        flip = "desc" if self.sort == key and self.dir == "asc" else "asc"
        return self.url(sort=key, dir=flip, cursor="")

    def chips(self) -> list[dict[str, str]]:
        """One removable chip per active filter value: `{label, remove_url}`."""
        chips: list[dict[str, str]] = []
        for z in self.zones:
            chips.append({"label": f"Zone: {z}", "url": self.url(zones=_without(self.zones, z), cursor="")})
        if self.bank:
            chips.append({"label": f"Bank: {self.bank}", "url": self.url(bank="", cursor="")})
        for h in self.health:
            chips.append(
                {"label": f"Health: {h}", "url": self.url(health=_without(self.health, h), cursor="")}
            )
        for a in self.activity:
            label = ACTIVITY_LABELS.get(a, a)
            chips.append(
                {
                    "label": f"Activity: {label}",
                    "url": self.url(activity=_without(self.activity, a), cursor=""),
                }
            )
        if self.soc_min or self.soc_max:
            span = f"{self.soc_min or '0'}-{self.soc_max or '100'} %"
            chips.append({"label": f"SoC: {span}", "url": self.url(soc_min="", soc_max="", cursor="")})
        if self.q:
            chips.append({"label": f"Hub id: {self.q}*", "url": self.url(q="", cursor="")})
        for v in self.hw:
            chips.append({"label": f"HW: {v}", "url": self.url(hw=_without(self.hw, v), cursor="")})
        for v in self.fw:
            chips.append({"label": f"FW: {v}", "url": self.url(fw=_without(self.fw, v), cursor="")})
        for v in self.asset:
            chips.append(
                {
                    "label": f"Type: {ASSET_LABELS[v]}",
                    "url": self.url(asset=_without(self.asset, v), cursor=""),
                }
            )
        if self.fw_not:
            chips.append({"label": f"FW \u2260 {self.fw_not}", "url": self.url(fw_not="", cursor="")})
        return chips


def _values(raw: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v.strip()[:64] for v in raw if v.strip()))


def _without(values: tuple[str, ...], drop: str) -> tuple[str, ...]:
    return tuple(v for v in values if v != drop)


def _num_text(value: str | None) -> str:
    try:
        number = float(value or "")
    except ValueError:
        return ""
    return f"{min(max(number, 0.0), 100.0):g}"


def table_state(request: Request) -> TableState:
    """Parse the URL query (repeated keys for the multi-selects) into a `TableState`; unknown values
    are dropped rather than forwarded."""
    qp = request.query_params
    size = qp.get("size", str(DEFAULT_PAGE_SIZE))
    return TableState(
        zones=tuple(dict.fromkeys(z.strip() for z in qp.getlist("zone") if z.strip())),
        bank=(qp.get("bank") or "").strip(),
        health=tuple(dict.fromkeys(h.upper() for h in qp.getlist("health") if h.upper() in HEALTH_CHOICES)),
        activity=tuple(dict.fromkeys(a for a in qp.getlist("activity") if a in ACTIVITY_LABELS)),
        soc_min=_num_text(qp.get("soc_min")),
        soc_max=_num_text(qp.get("soc_max")),
        q=(qp.get("q") or "").strip()[:64],
        hw=_values(qp.getlist("hw")),
        fw=_values(qp.getlist("fw")),
        fw_not=(qp.get("fw_not") or "").strip()[:64],
        asset=tuple(dict.fromkeys(v.upper() for v in qp.getlist("asset_class") if v.upper() in ASSET_LABELS)),
        sort=qp.get("sort", "hub") if qp.get("sort", "hub") in SORT_COLUMNS else "hub",
        dir="desc" if qp.get("dir") == "desc" else "asc",
        size=int(size) if size in {str(s) for s in PAGE_SIZES} else DEFAULT_PAGE_SIZE,
        cursor=(qp.get("cursor") or "")[:512],
    )


_SUMMARY_KEYS = frozenset({"total", "online", "stale", "offline"})


def _as_params(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Repeated query keys as `{key: [values]}` (httpx encodes a list as repeated keys)."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        out.setdefault(key, []).append(value)
    return out


_SEARCH_KINDS = ("hub", "bank", "zone", "firmware", "hardware")
_OPTION_QUERY: dict[str, dict[str, Any]] = {
    k: {"kind": k, "q": "", "limit": 50} for k in ("hardware", "firmware")
}


def _ids(body: Any) -> set[str]:
    """The `id`s of a `GET /og/api/fleet/search` body (filter options); empty for anything else."""
    items = body.get("items", []) if isinstance(body, dict) else []
    return {str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")}


def _approx(n: int | None) -> str:
    if n is None:
        return "unknown number of"
    return f"~{n:,}"


def _table_row(hub: dict[str, Any], targets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    last_seen_at = hub.get("last_seen_at")
    return {
        "asset_class": hub.get("asset_class") or "HOME",
        "hardware_revision": hub.get("hardware_revision"),
        "firmware_version": hub.get("firmware_version"),
        "target": targets.get(str(hub.get("hub_id"))),
        "hub_id": hub.get("hub_id", "-"),
        "bank_id": hub.get("bank_id") or "-",
        "zone": hub.get("zone") or "-",
        "soc_pct": hub.get("soc_pct"),
        "soc_kwh": hub.get("soc_kwh"),
        "p_kw": hub.get("p_kw"),
        "activity": ACTIVITY_LABELS.get(str(hub.get("activity") or ""), "-"),
        "health": hub.get("health", "unknown"),
        "health_label": hub.get("health_label") or str(hub.get("health", "unknown")).upper(),
        "last_seen_at": last_seen_at,
        "age_badge": render_stale_badge(
            _age_s(last_seen_at), since_iso=last_seen_at, stale_after_s=_HUB_STALE_AFTER_S
        ),
    }


async def _optional_json(path: str, params: Any = None) -> Any:
    try:
        return await get_json(path, params=params)
    except ApiUnavailable as exc:
        logger.info("fleet screen: %s unavailable (%s)", path, exc)
        return None


@router.get("", response_class=HTMLResponse)
async def fleet_screen(
    request: Request,
    safestop_scope: str | None = Query(default=None),
    safestop_scope_id: str | None = Query(default=None),
) -> HTMLResponse:
    state = table_state(request)
    degraded: str | None = None
    page: dict[str, Any] = {}
    api_params: list[tuple[str, str]] = [
        *state.filter_params(),
        ("sort", state.sort),
        ("dir", state.dir),
        ("limit", str(state.size)),
    ]
    if state.cursor:
        api_params.append(("cursor", state.cursor))
    try:
        raw = await get_json(_TABLE_PATH, params=_as_params(api_params))
        page = raw if isinstance(raw, dict) else {}
    except ApiUnavailable as exc:
        # An API without the paged table (older deploy): the plain hub list, first page only.
        logger.warning("fleet screen: %s unavailable (%s); using %s", _TABLE_PATH, exc, _LEGACY_HUBS_PATH)
        try:
            legacy = await get_json(_LEGACY_HUBS_PATH, params={"limit": state.size})
            page = {"items": legacy.get("items", []) if isinstance(legacy, dict) else []}
        except ApiUnavailable as legacy_exc:
            logger.warning("fleet screen: %s unavailable: %s", _LEGACY_HUBS_PATH, legacy_exc)
            degraded = str(legacy_exc)
    hubs: list[dict[str, Any]] = [h for h in page.get("items", []) if isinstance(h, dict)]

    # The map draws the current page plus every truck and substation BESS (few, always worth seeing), or
    # the richer map payload when the API serves one (CR #19).
    map_hubs = [map_hub(h) for h in hubs]
    on_page = {h["hub_id"] for h in map_hubs}
    special = await _optional_json(
        _TABLE_PATH, params={"asset_class": ["MOBILE", "UTILITY_SCALE"], "limit": 100}
    )
    map_hubs += [
        map_hub(h)
        for h in (special or {}).get("items", [])
        if isinstance(h, dict) and h.get("hub_id") not in on_page
    ]
    stations = await _optional_json(_HOME_STATIONS_PATH)
    from_map = map_hubs_from(await _optional_json(_MAP_PATH, params=_as_params(state.filter_params())))
    if from_map:
        map_hubs = from_map

    summary = await _optional_json(_SUMMARY_PATH)
    zones_raw = await _optional_json(_SEARCH_PATH, params={"kind": "zone", "q": "", "limit": 50})
    zone_options = sorted(_ids(zones_raw) | set(state.zones))
    hw_options = sorted(
        _ids(await _optional_json(_SEARCH_PATH, params=_OPTION_QUERY["hardware"])) | set(state.hw)
    )
    fw_options = sorted(
        _ids(await _optional_json(_SEARCH_PATH, params=_OPTION_QUERY["firmware"])) | set(state.fw)
    )
    targets = await active_targets()
    windows = await _optional_json(_CHARGE_WINDOWS_PATH)
    operator = is_operator(request)
    releases: list[dict[str, Any]] = []
    if operator:
        pending = await _optional_json(_RELEASES_PATH)
        releases = [r for r in (pending or {}).get("items", []) if isinstance(r, dict)]

    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "role": role_of(request),
            "is_operator": operator,
            "state": state,
            "table_rows": [_table_row(h, targets) for h in hubs],
            "hw_options": hw_options,
            "asset_labels": ASSET_LABELS,
            "home_stations": (stations or {}).get("items", []) if isinstance(stations, dict) else [],
            "fw_options": fw_options,
            "target_count": len(targets),
            "charge_windows": charge_window_groups(windows),
            "charge_scopes": CHARGE_SCOPES,
            "approx_total": page.get("approx_total"),
            "approx_text": _approx(page.get("approx_total")),
            "next_url": state.url(cursor=page["next_cursor"]) if page.get("next_cursor") else None,
            "prev_url": state.url(cursor=page["prev_cursor"]) if page.get("prev_cursor") else None,
            "first_url": state.url(cursor="") if state.cursor else None,
            "reset_url": f"{BASE_PATH}/fleet",
            "sort_columns": SORT_COLUMNS,
            "page_sizes": PAGE_SIZES,
            "health_choices": HEALTH_CHOICES,
            "activity_labels": ACTIVITY_LABELS,
            "zone_options": zone_options,
            "summary": summary if isinstance(summary, dict) and summary.keys() >= _SUMMARY_KEYS else None,
            "selection_max": SELECTION_MAX,
            "releases": releases,
            "map_hubs": map_hubs,
            "safestop_prefill": safestop_prefill(safestop_scope, safestop_scope_id),
            "degraded": degraded,
            "rendered_at": datetime.now(UTC).isoformat(),
        },
    )


# -- JSON relays for the page's own scripts (typeahead, select-all-matching, pending releases) ----------


@router.get("/search")
async def fleet_search(
    kind: str = Query(...), q: str = Query(default=""), limit: int = Query(default=20, gt=0, le=50)
) -> JSONResponse:
    """Typeahead for every id field: relays `GET /og/api/fleet/search` (viewer role)."""
    if kind not in _SEARCH_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"kind must be one of {_SEARCH_KINDS}"
        )
    body = await _optional_json(_SEARCH_PATH, params={"kind": kind, "q": q[:64], "limit": limit})
    return JSONResponse(body if isinstance(body, dict) else {"kind": kind, "q": q, "items": []})


@router.get("/selection")
async def fleet_selection(request: Request) -> JSONResponse:
    """ "Select all N matching": every hub id under the current filter, capped server-side. Operators
    only -- a viewer has no selection to make."""
    _require_operator(request)
    state = table_state(request)
    try:
        body = await get_json(
            _SELECTION_PATH, params=_as_params([*state.filter_params(), ("max", str(SELECTION_MAX))])
        )
    except ApiUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=status.HTTP_502_BAD_GATEWAY)
    return JSONResponse(body)


@router.get("/release-requests")
async def fleet_release_requests(request: Request) -> JSONResponse:
    _require_operator(request)
    body = await _optional_json(_RELEASES_PATH)
    return JSONResponse(body if isinstance(body, dict) else {"items": []})


@router.get("/hubs/{hub_id}", response_class=HTMLResponse)
async def hub_drilldown(request: Request, hub_id: str) -> HTMLResponse:
    """The side drawer's body: the one-call detail aggregate when the API serves it, else the plain hub
    read (older API), so the drawer never goes blank."""
    detail: dict[str, Any] | None = None
    hub: dict[str, Any] = {"hub_id": hub_id}
    try:
        raw = await get_json(f"/og/api/fleet/hubs/{hub_id}/detail")
        detail = raw if isinstance(raw, dict) else None
    except ApiUnavailable as exc:
        logger.info("hub drawer: detail endpoint unavailable (%s); using the hub read", exc)
    if detail is None:
        try:
            raw = await get_json(f"/og/api/fleet/hubs/{hub_id}")
            hub = raw if isinstance(raw, dict) else hub
        except ApiUnavailable as exc:
            logger.warning("hub drilldown: /og/api/fleet/hubs/%s unavailable: %s", hub_id, exc)
            hub = {"hub_id": hub_id, "error": str(exc)}
    return templates.TemplateResponse(
        request,
        "_partials/hub_drilldown.html",
        {
            "hub": hub,
            "detail": detail,
            "target": (await active_targets()).get(hub_id),
            "charge_window": await _optional_json(
                f"{_CHARGE_WINDOWS_PATH}/effective", params={"hub_id": hub_id}
            ),
            "activity_labels": ACTIVITY_LABELS,
            "asset_labels": ASSET_LABELS,
            "role": role_of(request),
            "is_operator": is_operator(request),
        },
    )


# -- scoped safe stop, two-step confirmation (BUILD.md code-review round item 1) ----------------------


@router.post("/safestop/propose", response_class=HTMLResponse)
async def propose_safestop(
    request: Request,
    scope: str = Form(...),
    scope_id: str = Form(default=""),
    reason: str = Form(...),
) -> HTMLResponse:
    """Step 1 of 2: relays the operator's scope/reason to `POST /og/api/safestop` and renders the real
    proposal as an already-open confirm dialog. Nothing is stopped yet."""
    _require_operator(request)
    payload = {"scope": scope, "scope_id": scope_id or None, "reason": reason}
    try:
        proposal = await post_json(_SAFESTOP_PROPOSE_PATH, payload, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("fleet safestop propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"safestop-confirm-{proposal['proposal_id']}",
            title="Confirm scoped safe stop",
            proposal=proposal,
            confirm_url=f"{BASE_PATH}/fleet/safestop/{proposal['proposal_id']}/confirm",
            confirm_label="Engage safe stop",
            variant="safestop",
            target="#safestop-confirm-result",
        ),
    )


@router.post("/safestop/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_safestop(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation to `POST /og/api/safestop/{proposal_id}/confirm` and renders
    the engaged/timeout/expired result -- never assumes success."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_SAFESTOP_PROPOSE_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        logger.warning("fleet safestop confirm failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "_partials/safestop_confirm_result.html",
            {"result": None, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/safestop_confirm_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


# -- safe-stop RELEASE, two operators (K8: guardian-signed, og-safestop relays) -----------------------


def _release_result(request: Request, **context: Any) -> HTMLResponse:
    base = {"result": None, "status_code": None, "message": None, "release_request": None}
    return templates.TemplateResponse(request, "_partials/safestop_release_result.html", {**base, **context})


@router.post("/safestop/release/request", response_class=HTMLResponse)
async def request_release(
    request: Request,
    scope: str = Form(...),
    scope_id: str = Form(default=""),
    reason: str = Form(...),
) -> HTMLResponse:
    """Operator A: files the release request. Nothing is released; a DIFFERENT operator must approve it."""
    _require_operator(request)
    path = f"{_SAFESTOP_PROPOSE_PATH}/{scope}/{scope_id or 'FLEET'}/release"
    try:
        accepted = await post_json(path, {"reason": reason}, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("safestop release request failed: %s", exc)
        return _release_result(request, status_code=exc.status_code, message=str(exc))
    return _release_result(request, release_request=accepted)


@router.post("/safestop/release/review", response_class=HTMLResponse)
async def review_release(request: Request, proposal_id: str = Form(...)) -> HTMLResponse:
    """Operator B, step 1 of 2: an open confirm dialog for the request id; nothing is written yet."""
    _require_operator(request)
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"release-approve-{proposal_id}",
            title="Approve safe-stop release",
            proposal={
                "summary": f"Approve release request {proposal_id}. You must not be the operator who "
                "requested it; the guardian signs only for two different authorised operators.",
                "expires_in_s": None,
            },
            confirm_url=f"{BASE_PATH}/fleet/safestop/release/{proposal_id}/approve",
            confirm_label="Approve release",
            variant="danger",
            target="#safestop-release-result",
        ),
    )


@router.post("/safestop/release/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_release(request: Request, proposal_id: str) -> HTMLResponse:
    """Operator B, step 2 of 2: relays the approval; renders released / pending / refused -- never
    assumes the stop was released."""
    _require_operator(request)
    try:
        # 200 released / 202 pending (both parsed; the fragment renders RELEASED or PENDING). The API polls
        # up to 10 s for the guardian's signed release, so this call waits longer than the default.
        result = await post_json(
            f"{_SAFESTOP_PROPOSE_PATH}/release/{proposal_id}/approve",
            {},
            remote_user=remote_user(request),
            timeout_s=_RELEASE_APPROVE_TIMEOUT_S,
        )
    except ApiUnavailable as exc:
        logger.warning("safestop release approve failed: %s", exc)
        return _release_result(request, status_code=exc.status_code, message=str(exc))
    return _release_result(request, result=result, status_code=status.HTTP_200_OK)


# -- manual command, two-step confirmation (BUILD.md code-review round item 2) -------------------------


@router.post("/command/propose", response_class=HTMLResponse)
async def propose_command(
    request: Request,
    bank_id: str = Form(default=""),
    hub_id: str = Form(default=""),
    p_kw_setpoint: float = Form(...),
    reason: str = Form(...),
    duration_minutes: int = Form(default=DEFAULT_TARGET_MINUTES, ge=1, le=MAX_TARGET_MINUTES),
) -> HTMLResponse:
    """Step 1 of 2: relays the operator's target/setpoint/reason to `POST /og/api/fleet/command` and
    renders the real proposal as an already-open confirm dialog. Guardian evaluation happens at confirm
    time, not here."""
    _require_operator(request)
    if not bank_id and not hub_id:
        return templates.TemplateResponse(
            request, "_partials/propose_error.html", {"message": "bank id or hub id is required"}
        )
    payload = {
        "bank_id": bank_id or None,
        "hub_id": hub_id or None,
        "p_kw_setpoint": p_kw_setpoint,
        "reason": reason,
        "duration_minutes": duration_minutes,
    }
    try:
        proposal = await post_json(_COMMAND_PROPOSE_PATH, payload, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("fleet command propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"command-confirm-{proposal['proposal_id']}",
            title="Confirm manual command",
            proposal=proposal,
            confirm_url=f"{BASE_PATH}/fleet/command/{proposal['proposal_id']}/confirm",
            confirm_label="Send command",
            variant="danger",
            target="#command-confirm-result",
        ),
    )


@router.post("/command/bulk/propose", response_class=HTMLResponse)
async def propose_bulk_command(
    request: Request,
    hub_ids: str = Form(default=""),
    p_kw_setpoint: float = Form(...),
    reason: str = Form(...),
    duration_minutes: int = Form(default=DEFAULT_TARGET_MINUTES, ge=1, le=MAX_TARGET_MINUTES),
) -> HTMLResponse:
    """Step 1 of 2 for a selection (CR #19 item 2): relays the selected hubs, setpoint and reason to
    `POST /og/api/fleet/commands/bulk` and renders the real proposal as an already-open confirm dialog.
    When any selected hub is serving a customer or is in a fault/offline state -- this module's own
    `bulk_risk_reasons`, or the API's `requires_double_confirm` -- the dialog demands a second,
    explicit acknowledgement naming the reason before its confirm button will act. The guardian still
    evaluates and signs every command at confirm time; nothing here bypasses it."""
    _require_operator(request)
    selected = parse_hub_ids(hub_ids)
    if not selected:
        return templates.TemplateResponse(
            request,
            "_partials/propose_error.html",
            {"message": "select at least one hub on the map or in the table first"},
        )
    hubs: list[dict[str, Any]] = []
    try:
        raw = await get_json("/og/api/fleet/hubs")
        hubs = raw.get("items", []) if isinstance(raw, dict) else []
    except ApiUnavailable as exc:
        logger.info("bulk propose: hub list unavailable for the risk check (%s)", exc)
    reasons = bulk_risk_reasons(hubs, selected)
    payload = {
        "hub_ids": selected,
        "p_kw_setpoint": p_kw_setpoint,
        "reason": reason,
        "duration_minutes": duration_minutes,
    }
    try:
        proposal = await post_json(_BULK_COMMAND_PATH, payload, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("fleet bulk command propose failed: %s", exc)
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    api_reasons = api_double_confirm_reasons(proposal)
    double = bool(proposal.get("requires_double_confirm")) or bool(reasons)
    context = _confirm_dialog_context(
        dialog_id=f"bulk-confirm-{proposal['proposal_id']}",
        title=f"Confirm command for {len(selected)} hub" + ("" if len(selected) == 1 else "s"),
        proposal=proposal,
        confirm_url=f"{BASE_PATH}/fleet/command/bulk/{proposal['proposal_id']}/confirm",
        confirm_label="Send to selection",
        variant="danger",
        target="#bulk-confirm-result",
    )
    if double:
        combined = api_reasons + [r for r in reasons if r not in api_reasons]
        context["acknowledge"] = "I understand this overrides what these hubs are doing now: " + "; ".join(
            combined or ["the API flagged this selection as high risk"]
        )
    return templates.TemplateResponse(request, "_partials/confirm_dialog.html", context)


@router.post("/command/bulk/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_bulk_command(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 (and 3) for a selection: relays the confirmation. When the API flagged the selection it
    records the first confirm and answers `AWAITING_SECOND_CONFIRM`; the fragment then offers a second,
    separate confirm naming the API's reasons -- never sent automatically. The executing confirm returns
    per-hub guardian outcomes (`status=EXECUTED`, `outcome_counts`, `results`)."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_BULK_COMMAND_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        logger.warning("fleet bulk command confirm failed: %s", exc)
        result = api_error_result(exc)
        return templates.TemplateResponse(
            request,
            "_partials/fleet_bulk_confirm_result.html",
            {"result": result, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_bulk_confirm_result.html",
        {
            "result": result,
            "status_code": status.HTTP_200_OK,
            "message": None,
            "api_reasons": api_double_confirm_reasons(result if isinstance(result, dict) else None),
            "second_confirm_url": f"{BASE_PATH}/fleet/command/bulk/{proposal_id}/confirm",
        },
    )


@router.post("/command/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_command(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 2 of 2: relays the confirmation to `POST /og/api/fleet/command/{proposal_id}/confirm` and
    renders the guardian's pass/veto/timeout/expired result. A 409 veto's body is itself a
    `CommandConfirmResult` (outcome != "PASS"), so it renders the same as a successful pass, just with a
    different outcome."""
    _require_operator(request)
    try:
        result = await post_json(
            f"{_COMMAND_PROPOSE_PATH}/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        logger.warning("fleet command confirm failed: %s", exc)
        result = api_error_result(exc)
        return templates.TemplateResponse(
            request,
            "_partials/fleet_command_confirm_result.html",
            {"result": result, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_command_confirm_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


# -- R3.1: manual targets (ramped by the engine) -----------------------------------------------------


async def active_targets() -> dict[str, dict[str, Any]]:
    """hub_id -> the operator target currently controlling it (`GET /og/api/fleet/manual-targets`);
    empty when the API does not serve it yet, so the table simply shows no markers."""
    body = await _optional_json(_TARGETS_PATH)
    items = body.get("items", []) if isinstance(body, dict) else []
    return {str(t["hub_id"]): t for t in items if isinstance(t, dict) and t.get("hub_id")}


@router.get("/hubs/{hub_id}/live")
async def hub_live(hub_id: str) -> JSONResponse:
    """The hub's current P for the ramp progress indicator (`{hub_id, p_kw, last_seen_at}`)."""
    body = await _optional_json(f"{_LEGACY_HUBS_PATH}/{hub_id}")
    hub = body if isinstance(body, dict) else {}
    return JSONResponse({"hub_id": hub_id, "p_kw": hub.get("p_kw"), "last_seen_at": hub.get("last_seen_at")})


@router.post("/manual-targets/{trace_id}/cancel", response_class=HTMLResponse)
async def cancel_target(request: Request, trace_id: str) -> HTMLResponse:
    """Ends an operator target early: relays `POST /og/api/fleet/manual-targets/{trace_id}/cancel`; the
    engine hands the hubs back to normal dispatch. 404 means the target no longer controls any hub."""
    _require_operator(request)
    try:
        result = await post_json(f"{_TARGETS_PATH}/{trace_id}/cancel", {}, remote_user=remote_user(request))
    except ApiUnavailable as exc:
        logger.warning("manual target cancel failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "_partials/fleet_target_cancel_result.html",
            {"result": None, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_target_cancel_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


# -- R3.1: grid-charging schedule (D-30) -------------------------------------------------------------


def charge_window_groups(body: Any) -> dict[str, Any] | None:
    """`GET /og/api/fleet/charge-windows` grouped by scope kind in hierarchy order, or None when the API
    is absent (the card then says it is available after R3.1)."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return None
    rank = {kind: i for i, (kind, _label) in enumerate(CHARGE_SCOPES)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(
        (i for i in body["items"] if isinstance(i, dict)),
        key=lambda i: (rank.get(str(i.get("scope_kind")), 99), str(i.get("scope_ref"))),
    ):
        groups.setdefault(str(item.get("scope_kind")), []).append(item)
    return {"tz": body.get("tz", "America/Chicago"), "groups": groups}


def parse_windows(starts: list[str], ends: list[str]) -> list[str]:
    """Form rows -> the API's `"HH:MM-HH:MM"` strings. Wrapping past midnight is allowed; an empty row
    is skipped; start == end, a malformed time or more than `MAX_WINDOWS` windows is a ValueError."""
    windows: list[str] = []
    for start, end in zip(starts, ends, strict=False):
        start, end = start.strip(), end.strip()
        if not start and not end:
            continue
        if not (_WINDOW_RE.match(start) and _WINDOW_RE.match(end)):
            raise ValueError(f"times must be HH:MM (got {start or '?'} to {end or '?'})")
        if start == end:
            raise ValueError(f"window {start}-{end} is empty")
        windows.append(f"{start}-{end}")
    if len(windows) > MAX_WINDOWS:
        raise ValueError(f"at most {MAX_WINDOWS} windows")
    return windows


def _charge_path(scope_kind: str, scope_ref: str) -> str:
    kind = scope_kind.upper()
    if kind not in dict(CHARGE_SCOPES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown scope {scope_kind!r}")
    ref = "*" if kind == "FLEET" else scope_ref.strip()
    if not ref:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a scope id is required")
    return f"{_CHARGE_WINDOWS_PATH}/{quote(kind, safe='')}/{quote(ref, safe='')}"


def _charge_dialog(request: Request, proposal: dict[str, Any], title: str) -> HTMLResponse:
    old = ", ".join(proposal.get("old_windows") or []) or "none (inherits)"
    new = proposal.get("new_windows")
    summary = proposal.get("summary") or (
        f"{proposal.get('scope_kind')}:{proposal.get('scope_ref')}: {old} -> "
        + (", ".join(new) if new else "override removed")
    )
    return templates.TemplateResponse(
        request,
        "_partials/confirm_dialog.html",
        _confirm_dialog_context(
            dialog_id=f"charge-confirm-{proposal['proposal_id']}",
            title=title,
            proposal={"summary": summary, "expires_in_s": proposal.get("expires_in_s")},
            confirm_url=f"{BASE_PATH}/fleet/charge-windows/proposals/{proposal['proposal_id']}/confirm",
            confirm_label="Save schedule",
            variant="primary",
            target="#charge-confirm-result",
        ),
    )


@router.post("/charge-windows/propose", response_class=HTMLResponse)
async def propose_charge_windows(request: Request) -> HTMLResponse:
    """Step 1 of 2: `PUT /og/api/fleet/charge-windows/{kind}/{ref}` proposes the new windows; the dialog
    shows old -> new and only its confirm button saves."""
    _require_operator(request)
    form = await request.form()
    reason = str(form.get("reason") or "").strip()
    try:
        windows = parse_windows(
            [str(v) for v in form.getlist("start")], [str(v) for v in form.getlist("end")]
        )
    except ValueError as exc:
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    if not windows or not reason:
        message = "add at least one window" if not windows else "a reason is required"
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": message})
    path = _charge_path(str(form.get("scope_kind") or ""), str(form.get("scope_ref") or ""))
    try:
        proposal = await put_json(
            path, {"windows": windows, "reason": reason}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return _charge_dialog(request, proposal, "Confirm charging schedule")


@router.post("/charge-windows/remove", response_class=HTMLResponse)
async def propose_charge_window_removal(
    request: Request,
    scope_kind: str = Form(...),
    scope_ref: str = Form(...),
    reason: str = Form(default=""),
) -> HTMLResponse:
    """Step 1 of 2 for removing an override (never the Fleet default): the scope then inherits."""
    _require_operator(request)
    if scope_kind.upper() == "FLEET":
        return templates.TemplateResponse(
            request, "_partials/propose_error.html", {"message": "the Fleet default cannot be removed"}
        )
    if not reason.strip():
        return templates.TemplateResponse(
            request, "_partials/propose_error.html", {"message": "a reason is required to remove an override"}
        )
    try:
        proposal = await delete_json(
            _charge_path(scope_kind, scope_ref), remote_user=remote_user(request), payload={"reason": reason}
        )
    except ApiUnavailable as exc:
        return templates.TemplateResponse(request, "_partials/propose_error.html", {"message": str(exc)})
    return _charge_dialog(request, proposal, "Confirm override removal")


@router.post("/charge-windows/proposals/{proposal_id}/confirm", response_class=HTMLResponse)
async def confirm_charge_windows(request: Request, proposal_id: str) -> HTMLResponse:
    _require_operator(request)
    try:
        result = await post_json(
            f"{_CHARGE_WINDOWS_PATH}/proposals/{proposal_id}/confirm", {}, remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        return templates.TemplateResponse(
            request,
            "_partials/fleet_charge_result.html",
            {"result": None, "status_code": exc.status_code, "message": str(exc)},
        )
    return templates.TemplateResponse(
        request,
        "_partials/fleet_charge_result.html",
        {"result": result, "status_code": status.HTTP_200_OK, "message": None},
    )


@router.get("/charge-windows/effective", response_class=HTMLResponse)
async def effective_charge_windows(request: Request, hub_id: str = "", bank_id: str = "") -> HTMLResponse:
    """Preview: the windows that apply to a hub or bank and the scope they come from."""
    target = {"hub_id": hub_id.strip()} if hub_id.strip() else {"bank_id": bank_id.strip()}
    body = (
        await _optional_json(f"{_CHARGE_WINDOWS_PATH}/effective", params=target)
        if any(target.values())
        else None
    )
    return templates.TemplateResponse(
        request, "_partials/fleet_charge_effective.html", {"effective": body, "target": target}
    )
