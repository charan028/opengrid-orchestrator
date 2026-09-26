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
            "hub_id": "hub-0001",
            "bank_id": "bank-01",
            "zone": "LZ_SOUTH",
            "soc_kwh": 9.8,
            "e_kwh": 39.2,
            "soc_pct": 25.0,
            "p_kw": -2.1,
            "health": "online",
            "health_label": "OK",
            "activity": "charging",
            "rated_p_kw": 11.0,
            "lat": 29.76,
            "lon": -95.36,
            "last_seen_at": "2026-09-25T11:59:55+00:00",
        }
    if i == 2:
        return {
            "hub_id": "hub-0002",
            "bank_id": "bank-01",
            "zone": "LZ_SOUTH",
            "soc_kwh": 3.1,
            "e_kwh": 39.2,
            "soc_pct": 7.9,
            "p_kw": 0.0,
            "health": "fault",
            "health_label": "FAULT",
            "activity": "idle",
            "rated_p_kw": 11.0,
            "lat": 29.77,
            "lon": -95.34,
            "last_seen_at": "2026-09-25T11:58:00+00:00",
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


HW_REVS = ("B2", "C1")
FW_VERSIONS = ("4.2.1", "4.3.0")


def _with_device(hub: dict[str, Any], i: int) -> dict[str, Any]:
    return {**hub, "hardware_revision": HW_REVS[i % 2], "firmware_version": FW_VERSIONS[(i // 3) % 2]}


#: A D-31 truck away from its depot and a substation BESS (no trucks exist in the real fleet yet).
TRUCK = {
    "hub_id": "trailer-mb-01",
    "bank_id": "trailer-mb-01",
    "zone": "LZ_AEN",
    "soc_kwh": 300.0,
    "e_kwh": 600.0,
    "soc_pct": 50.0,
    "p_kw": -120.0,
    "health": "online",
    "health_label": "OK",
    "activity": "delivering",
    "rated_p_kw": 250.0,
    "lat": 30.27,
    "lon": -97.74,
    "last_seen_at": "2026-09-25T11:59:55+00:00",
    "asset_class": "MOBILE",
    "hardware_revision": None,
    "firmware_version": None,
}
SUBSTATION = {
    "hub_id": "sub-LZ_AEN-00",
    "bank_id": "bank-sub-aen",
    "zone": "LZ_AEN",
    "soc_kwh": 8000.0,
    "e_kwh": 16000.0,
    "soc_pct": 50.0,
    "p_kw": 0.0,
    "health": "online",
    "health_label": "OK",
    "activity": "idle",
    "rated_p_kw": 4000.0,
    "lat": 30.35,
    "lon": -97.68,
    "last_seen_at": "2026-09-25T11:59:55+00:00",
    "asset_class": "UTILITY_SCALE",
    "hardware_revision": None,
    "firmware_version": None,
}
HOME_STATIONS = {
    "items": [
        {
            "home_station_id": "hs-austin-north-01",
            "zone": "LZ_AEN",
            "lat": 30.401,
            "lon": -97.719,
            "charger_kw": 150.0,
            "notes": "Depot",
            "units": ["trailer-mb-01"],
        }
    ]
}
HUBS = [{**_with_device(_hub(i), i), "asset_class": "HOME"} for i in range(1, N_HUBS + 1)] + [
    TRUCK,
    SUBSTATION,
]
_SORT = {
    "hw": "hardware_revision",
    "fw": "firmware_version",
    "hub": "hub_id",
    "bank": "bank_id",
    "zone": "zone",
    "soc": "soc_pct",
    "kw": "p_kw",
    "health": "health",
}


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
    hw, fw, fw_not = _multi(params, "hw"), _multi(params, "fw"), _one(params, "fw_not")
    classes = _multi(params, "asset_class")
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
        if (hw and hub["hardware_revision"] not in hw) or (fw and hub["firmware_version"] not in fw):
            continue
        if fw_not and hub["firmware_version"] == fw_not:
            continue
        if classes and hub["asset_class"] not in classes:
            continue
        out.append(hub)
    return out


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def table(params: Any) -> dict[str, Any]:
    rows = _matching(params)
    sort = _one(params, "sort") or "hub"
    key = _SORT.get(sort, "last_seen_at")
    rows.sort(
        key=lambda h: (h[key] is not None, h[key] if h[key] is not None else "", h["hub_id"]),
        reverse=_one(params, "dir") == "desc",
    )
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
    return {
        "hub_ids": ids[:SELECTION_CAP],
        "count": min(len(ids), SELECTION_CAP),
        "capped": len(ids) > SELECTION_CAP,
        "max": SELECTION_CAP,
    }


def search(params: Any) -> dict[str, Any]:
    kind, q = _one(params, "kind") or "hub", (_one(params, "q") or "").lower()
    limit = int(_one(params, "limit") or 20)
    if kind in ("firmware", "hardware"):
        values = FW_VERSIONS if kind == "firmware" else HW_REVS
        return {"kind": kind, "q": q, "items": [{"id": v} for v in values if v.lower().startswith(q)]}
    if kind == "zone":
        items: list[dict[str, Any]] = [{"id": z} for z in sorted(ZONES) if z.lower().startswith(q)]
    elif kind == "bank":
        banks = sorted({(h["bank_id"], h["zone"]) for h in HUBS})
        items = [{"id": b, "zone": z} for b, z in banks if b.startswith(q)]
    else:
        items = [
            {
                "id": h["hub_id"],
                "bank_id": h["bank_id"],
                "zone": h["zone"],
                "health": h["health"],
                "health_label": h["health_label"],
                "rated_p_kw": h["rated_p_kw"],
            }
            for h in HUBS
            if h["hub_id"].startswith(q)
        ]
    return {"kind": kind, "q": q, "items": items[:limit]}


RELEASES = {
    "items": [
        {
            "proposal_id": "22222222-2222-2222-2222-222222222222",
            "scope": "ZONE",
            "scope_ref": "LZ_SOUTH",
            "requested_by": "alice",
            "reason": "storm passed",
            "age_s": 12.0,
        }
    ]
}

SUMMARY = {"total": N_HUBS, "online": 65, "stale": 22, "offline": 43}


def detail(hub_id: str) -> dict[str, Any]:
    hub = next(h for h in HUBS if h["hub_id"] == hub_id)
    mobile = (
        {
            "home_station": HOME_STATIONS["items"][0],
            "location": {"lat": hub["lat"], "lon": hub["lon"]},
            "status": "AWAY",
            "charging_allowed": False,
            "charging_note": "D-31",
            "next_return": None,
        }
        if hub["asset_class"] == "MOBILE"
        else None
    )
    utility = (
        {
            "asset_id": hub_id,
            "mw": 4.0,
            "mwh": 16.0,
            "poi_import_kva": 4000.0,
            "poi_export_kva": 4000.0,
            "feeder_id": "F-AEN-7",
            "substation_id": "SUB-AEN",
            "status": "ACTIVE",
        }
        if hub["asset_class"] == "UTILITY_SCALE"
        else None
    )
    return {
        "hub_id": hub_id,
        "asset_class": hub["asset_class"],
        "mobile": mobile,
        "utility_scale": utility,
        "status": {
            "health": hub["health"],
            "health_label": hub["health_label"],
            "online": hub["health"] != "offline",
            "last_seen_at": hub["last_seen_at"],
            "age_s": 4.0,
            "activity": hub["activity"],
            "serving": [],
            "p_kw": hub["p_kw"],
            "soc_kwh": hub["soc_kwh"],
            "soc_pct": hub["soc_pct"],
            "reserve_kwh": 7.84,
            "reserve_pct": 20.0,
            "above_reserve_kwh": round(hub["soc_kwh"] - 7.84, 2),
            "fault_code": None,
        },
        "telemetry": {
            "ts": hub["last_seen_at"],
            "fields": {"p_kw": hub["p_kw"], "soc_kwh": hub["soc_kwh"], "cell_temp_c": 27.5},
            "raw": {
                "ts": hub["last_seen_at"],
                "p_kw": hub["p_kw"],
                "soc_kwh": hub["soc_kwh"],
                "cell_temp_c": 27.5,
            },
        },
        "alerts": [
            {
                "severity": "warning",
                "rule": "ALR-HUB-STALE",
                "opened_at": "2026-09-25T11:00:00+00:00",
                "cleared_at": None,
            }
        ],
        "location": {
            "lat": 29.76,
            "lon": -95.36,
            "zone": hub["zone"],
            "bank_id": hub["bank_id"],
            "feeder_id": "F-12",
            "service_transformer_id": None,
        },
        "asset": {
            "installed_at": None,
            "device_info": {
                "hardware_revision": hub["hardware_revision"],
                "firmware_version": hub["firmware_version"],
                "serial_number": "SN-1",
            },
            "device_info_at": "2026-09-26T12:00:00+00:00",
            "last_serviced_at": None,
            "units": 1,
            "rated_p_kw": 11.0,
            "rated_e_kwh": 39.2,
            "reserve_kwh": 7.84,
            "last_calibration": None,
        },
        "control": {
            "lease_epoch": 3,
            "lease_expires_at": None,
            "last_command_id": None,
            "last_command_verdict": None,
        },
    }


def responses() -> dict[str, Any]:
    """GET path -> body or callable(params) for the conftest's fake `get_json`."""
    out: dict[str, Any] = {
        "/og/api/fleet/table": table,
        "/og/api/fleet/selection": selection,
        "/og/api/fleet/search": search,
        "/og/api/fleet/release-requests": RELEASES,
        "/og/api/fleet/summary": SUMMARY,
        "/og/api/fleet/manual-targets": TARGETS,
        "/og/api/fleet/home-stations": HOME_STATIONS,
        CHARGE_PATH: CHARGE_WINDOWS,
        f"{CHARGE_PATH}/effective": effective,
    }
    for hub in HUBS[2:3]:
        out[f"/og/api/fleet/hubs/{hub['hub_id']}"] = hub
    for hub_id in ("hub-0001", "hub-0002", "hub-0003", "trailer-mb-01", "sub-LZ_AEN-00"):
        out[f"/og/api/fleet/hubs/{hub_id}/detail"] = detail(hub_id)
    return out


# -- R3.1: manual targets (FOLLOWUPS' 202 RAMPING shape) and charge windows (D-30) ------------------

TARGET_TRACE = "55555555-5555-5555-5555-555555555555"
CHARGE_PATH = "/og/api/fleet/charge-windows"
CHARGE_PROPOSAL_ID = "66666666-6666-6666-6666-666666666666"
TARGETS = {
    "items": [
        {
            "hub_id": "hub-0003",
            "p_kw_target": 5.0,
            "issued_at": "2026-09-26T23:00:00+00:00",
            "expires_at": "2026-09-26T23:15:00+00:00",
            "trace_id": TARGET_TRACE,
            "proposer": "alice",
            "reason": "test",
        }
    ]
}
CHARGE_WINDOWS = {
    "tz": "America/Chicago",
    "items": [
        {
            "scope_kind": "FLEET",
            "scope_ref": "*",
            "windows": ["22:00-06:00"],
            "updated_by": "seed",
            "updated_at": "2026-09-26T12:00:00+00:00",
        },
        {
            "scope_kind": "BANK",
            "scope_ref": "bank-03",
            "windows": ["23:00-05:00", "13:00-14:00"],
            "updated_by": "alice",
            "updated_at": "2026-09-26T20:00:00+00:00",
        },
    ],
}


def effective(params: Any) -> dict[str, Any]:
    if _one(params, "hub_id") == "hub-0003" or _one(params, "bank_id") == "bank-03":
        return {
            "windows": ["23:00-05:00", "13:00-14:00"],
            "tz": "America/Chicago",
            "source": {"scope_kind": "BANK", "scope_ref": "bank-03"},
        }
    return {
        "windows": ["22:00-06:00"],
        "tz": "America/Chicago",
        "source": {"scope_kind": "FLEET", "scope_ref": "*"},
    }


def charge_propose(path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    kind, ref = path[len(CHARGE_PATH) + 1 :].split("/", 1)
    return {
        "proposal_id": CHARGE_PROPOSAL_ID,
        "summary": None,
        "scope_kind": kind,
        "scope_ref": ref,
        "old_windows": ["22:00-06:00"],
        "new_windows": (payload or {}).get("windows"),
        "expires_in_s": 60.0,
    }


def post_responses(command_id: str, bulk_id: str) -> dict[str, Any]:
    return {
        f"/og/api/fleet/command/{command_id}/confirm": {
            "status": "RAMPING",
            "trace_id": TARGET_TRACE,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "hub_ids": ["hub-0001"],
            "p_kw_target": 5.0,
        },
        f"/og/api/fleet/manual-targets/{TARGET_TRACE}/cancel": {
            "status": "CANCELLED",
            "trace_id": "77777777-7777-7777-7777-777777777777",
            "cancels": TARGET_TRACE,
            "hub_ids": ["hub-0001"],
        },
        f"{CHARGE_PATH}/proposals/{CHARGE_PROPOSAL_ID}/confirm": {
            "scope_kind": "BANK",
            "scope_ref": "bank-05",
            "old_windows": [],
            "windows": ["21:00-05:00"],
            "trace_id": "88888888-8888-8888-8888-888888888888",
        },
    }


def bulk_ramping() -> dict[str, Any]:
    return {
        "status": "RAMPING",
        "hub_count": 3,
        "outcome_counts": {"RAMPING": 3},
        "results": [{"hub_id": h, "outcome": "RAMPING"} for h in ("hub-0001", "hub-0002", "hub-0003")],
        "manual_target_trace_id": TARGET_TRACE,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "trace_id": "99999999-9999-9999-9999-999999999999",
        "p_kw_target": 5.0,
    }
