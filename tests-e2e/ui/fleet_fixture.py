"""A params-aware fake of the Fleet table API (`opengrid.api.routers.fleet_search`) for fixture mode:
130 synthetic hubs (the first two match `hubs.json`), filtered, sorted and cursor-paged in memory so the
browser tests can exercise pagination, filters, URL state, typeahead and select-all-matching."""

from __future__ import annotations

import base64
import json
from typing import Any

ZONES = ("LZ_SOUTH", "LZ_NORTH", "LZ_HOUSTON", "LZ_WEST")
HEALTH = ("online", "stale", "online", "fault", "online", "offline")
LABEL = {"online": "OK", "stale": "WATCH", "fault": "FAULT", "offline": "OFFLINE"}
ACT = ("charging", "idle", "delivering", "serving_home")
N_HUBS = 130
SELECTION_CAP = 100  # small in fixture mode so the "capped" note is exercised


def _hub(i: int) -> dict[str, Any]:
    if i == 1:
        return {
            "hub_id": "hub-0001", "bank_id": "bank-01", "zone": "LZ_SOUTH", "soc_kwh": 9.8, "e_kwh": 39.2,
            "soc_pct": 25.0, "p_kw": -2.1, "health": "online", "health_label": "OK", "activity": "charging",
            "rated_p_kw": 11.0, "lat": 29.76, "lon": -95.36, "last_seen_at": "2026-09-25T11:59:55+00:00",
        }
    if i == 2:
        return {
            "hub_id": "hub-0002", "bank_id": "bank-01", "zone": "LZ_SOUTH", "soc_kwh": 3.1, "e_kwh": 39.2,
            "soc_pct": 7.9, "p_kw": 0.0, "health": "fault", "health_label": "FAULT", "activity": "idle",
            "rated_p_kw": 11.0, "lat": 29.77, "lon": -95.34, "last_seen_at": "2026-09-25T11:58:00+00:00",
        }
    health = HEALTH[i % len(HEALTH)]
    soc_pct = float((i * 7) % 100)
    return {
        "hub_id": f"hub-{i:04d}",
        "bank_id": f"bank-{(i % 12) + 1:02d}",
        "zone": ZONES[i % len(ZONES)],
        "soc_kwh": round(soc_pct * 0.392, 2),
        "e_kwh": 39.2,
        "soc_pct": soc_pct,
        "p_kw": float((i % 9) - 4),
        "health": health,
        "health_label": LABEL[health],
        "activity": ACT[i % len(ACT)],
        "rated_p_kw": 11.0,
        "last_seen_at": "2026-09-25T11:59:50+00:00",
    }


HUBS = [_hub(i) for i in range(1, N_HUBS + 1)]
_SORT = {"hub": "hub_id", "bank": "bank_id", "zone": "zone", "soc": "soc_pct", "kw": "p_kw", "health": "health"}


def _multi(params: Any, key: str) -> list[str]:
    if isinstance(params, dict):
        value = params.get(key)
        if value is None:
            return []
        return [str(v) for v in value] if isinstance(value, list | tuple) else [str(value)]
    return [v for k, v in params or [] if k == key]


def _one(params: Any, key: str) -> str | None:
    values = _multi(params, key)
    return values[0] if values else None


def _matching(params: Any) -> list[dict[str, Any]]:
    zones, health, acts = _multi(params, "zone"), _multi(params, "health"), _multi(params, "activity")
    bank, q = _one(params, "bank"), _one(params, "q")
    lo, hi = _one(params, "soc_min"), _one(params, "soc_max")
    out = []
    for hub in HUBS:
        if zones and hub["zone"] not in zones:
            continue
        if bank and hub["bank_id"] != bank:
            continue
        if health and hub["health_label"] not in [h.upper() for h in health]:
            continue
        if acts and hub["activity"] not in acts:
            continue
        if lo and hub["soc_pct"] < float(lo):
            continue
        if hi and hub["soc_pct"] > float(hi):
            continue
        if q and not hub["hub_id"].lower().startswith(q.lower()):
            continue
        out.append(hub)
    return out


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def table(params: Any) -> dict[str, Any]:
    rows = _matching(params)
    sort = _one(params, "sort") or "hub"
    key = _SORT.get(sort, "last_seen_at")
    rows.sort(key=lambda h: (h[key], h["hub_id"]), reverse=_one(params, "dir") == "desc")
    limit = int(_one(params, "limit") or 50)
    raw = _one(params, "cursor")
    offset = json.loads(base64.urlsafe_b64decode(raw))["o"] if raw else 0
    page = rows[offset : offset + limit]
    return {
        "items": page,
        "next_cursor": _cursor(offset + limit) if offset + limit < len(rows) else None,
        "prev_cursor": _cursor(max(offset - limit, 0)) if offset > 0 else None,
        "approx_total": len(rows),
        "total_is_estimate": True,
        "limit": limit,
        "sort": sort,
        "dir": _one(params, "dir") or "asc",
    }


def selection(params: Any) -> dict[str, Any]:
    ids = [h["hub_id"] for h in _matching(params)]
    return {"hub_ids": ids[:SELECTION_CAP], "count": min(len(ids), SELECTION_CAP), "capped": len(ids) > SELECTION_CAP, "max": SELECTION_CAP}


def search(params: Any) -> dict[str, Any]:
    kind, q = _one(params, "kind") or "hub", (_one(params, "q") or "").lower()
    limit = int(_one(params, "limit") or 20)
    if kind == "zone":
        items: list[dict[str, Any]] = [{"id": z} for z in sorted(ZONES) if z.lower().startswith(q)]
    elif kind == "bank":
        banks = sorted({(h["bank_id"], h["zone"]) for h in HUBS})
        items = [{"id": b, "zone": z} for b, z in banks if b.startswith(q)]
    else:
        items = [
            {"id": h["hub_id"], "bank_id": h["bank_id"], "zone": h["zone"], "health": h["health"],
             "health_label": h["health_label"], "rated_p_kw": h["rated_p_kw"]}
            for h in HUBS
            if h["hub_id"].startswith(q)
        ]
    return {"kind": kind, "q": q, "items": items[:limit]}


RELEASES = {
    "items": [
        {"proposal_id": "22222222-2222-2222-2222-222222222222", "scope": "ZONE", "scope_ref": "LZ_SOUTH",
         "requested_by": "alice", "reason": "storm passed", "age_s": 12.0}
    ]
}

SUMMARY = {"total": N_HUBS, "online": 65, "stale": 22, "offline": 43}


def detail(hub_id: str) -> dict[str, Any]:
    hub = next(h for h in HUBS if h["hub_id"] == hub_id)
    return {
        "hub_id": hub_id,
        "status": {
            "health": hub["health"], "health_label": hub["health_label"], "online": hub["health"] != "offline",
            "last_seen_at": hub["last_seen_at"], "age_s": 4.0, "activity": hub["activity"], "serving": [],
            "p_kw": hub["p_kw"], "soc_kwh": hub["soc_kwh"], "soc_pct": hub["soc_pct"], "reserve_kwh": 7.84,
            "reserve_pct": 20.0, "above_reserve_kwh": round(hub["soc_kwh"] - 7.84, 2), "fault_code": None,
        },
        "telemetry": {
            "ts": hub["last_seen_at"],
            "fields": {"p_kw": hub["p_kw"], "soc_kwh": hub["soc_kwh"], "cell_temp_c": 27.5},
            "raw": {"ts": hub["last_seen_at"], "p_kw": hub["p_kw"], "soc_kwh": hub["soc_kwh"], "cell_temp_c": 27.5},
        },
        "alerts": [
            {"severity": "warning", "rule": "ALR-HUB-STALE", "opened_at": "2026-09-25T11:00:00+00:00", "cleared_at": None}
        ],
        "location": {"lat": 29.76, "lon": -95.36, "zone": hub["zone"], "bank_id": hub["bank_id"],
                     "feeder_id": "F-12", "service_transformer_id": None},
        "asset": {"install_date": None, "last_serviced_at": None, "units": 1, "rated_p_kw": 11.0,
                  "rated_e_kwh": 39.2, "reserve_kwh": 7.84, "last_calibration": None},
        "control": {"lease_epoch": 3, "lease_expires_at": None, "last_command_id": None, "last_command_verdict": None},
    }


def responses() -> dict[str, Any]:
    """GET path -> body or callable(params) for the conftest's fake `get_json`."""
    out: dict[str, Any] = {
        "/og/api/fleet/table": table,
        "/og/api/fleet/selection": selection,
        "/og/api/fleet/search": search,
        "/og/api/fleet/release-requests": RELEASES,
        "/og/api/fleet/summary": SUMMARY,
    }
    for hub_id in ("hub-0001", "hub-0002", "hub-0003"):
        out[f"/og/api/fleet/hubs/{hub_id}/detail"] = detail(hub_id)
    return out
