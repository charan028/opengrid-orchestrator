"""Dispatch & commitments screen (02b S8 screen 3, UI-DSP). Owner: ui-b (BUILD.md S4).

Renders `/og/dispatch`: the opportunity/obligation pipeline across ALL concurrent customers, the
per-bank ledger timeline (committed y-hat + free headroom), the latest selector plan (LP vs rule
baseline), a real-time grants/substitutions feed, and commitment-lock events (K13). Server-rendered
first paint comes from `opengrid.ui.api_client.get_json` (a plain HTTP call to `og-api`, 02b S7.1);
live updates after first paint are the browser's job via `og.sse("/og/api/stream/dispatch", ...)`. The
view-model functions below are pure and unit-tested against JSON fixtures, no HTTP or DB involved.

API field needed (not owned here, `opengrid.api`'s router): each row from
`/og/api/dispatch/opportunities` (and the `/og/api/stream/dispatch` SSE payload's `opportunities` list)
for a `COMMITTED`/`DELIVERING` obligation should carry `energy_margin_kwh` (float) and
`time_to_depletion_h` (float | null) -- the continuous per-obligation energy-sufficiency check's output
(K1, `opengrid.allocator.energy_sufficiency.EnergySufficiencyResult`). `pipeline_view` below already
passes these through when present and degrades to `None` when absent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from opengrid.core.services import ERCOT_AS_SERVICE_TYPE, REGULATED_CAPACITY_SERVICE_TYPE
from opengrid.core.services import AS_HOLD_HOURS, AS_MAX_DEPLOY_MINUTES, canonical_product
from opengrid.ui.api_client import ApiUnavailable, delete_json, get_json, post_json
from opengrid.ui.role import is_operator, remote_user, role_of
from opengrid.ui.templating import BASE_PATH, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dispatch")

_DEFAULT_BANK_ID = "BANK-0001"
_LOCK_REASON_PREFIX = "R-COMMIT-LOCK"

PIPELINE_STATES: tuple[str, ...] = (
    "OFFERED",
    "SELECTED",
    "COMMITTED",
    "DELIVERING",
    "FULFILLED_OR_SHORTFALL",
)

_PIPELINE_LABELS: dict[str, str] = {
    "OFFERED": "Offered",
    "SELECTED": "Selected",
    "COMMITTED": "Committed",
    "DELIVERING": "Delivering",
    "FULFILLED_OR_SHORTFALL": "Fulfilled / shortfall",
}


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def decision_line(row: dict[str, Any]) -> str:
    """One sentence saying why a card sits in its column, from the numbers the selector weighed:
    the offer's value ($/MWh) against the contract's degradation cost ($/kWh -> shown per MWh), the
    promised kW and the window. This is the board's answer to "what is the optimizer doing?"."""
    state = row.get("state")
    value = _f(row.get("value_per_mwh"))
    degradation = _f(row.get("degradation_cost"))
    degradation_mwh = degradation * 1000.0 if degradation is not None else None
    kw = _f(row.get("committed_qty_kw")) or _f(row.get("requested_kw")) or 0.0
    hours = None
    try:
        start = datetime.fromisoformat(str(row["window_start"]))
        end = datetime.fromisoformat(str(row["window_end"]))
        hours = max((end - start).total_seconds() / 3600.0, 0.0)
    except (KeyError, TypeError, ValueError):
        pass
    if state == "SELECTED":
        if value is not None and degradation_mwh is not None:
            margin = (value - degradation_mwh) / 1000.0 * kw * (hours or 0.0)
            return (
                f"Selected: {value:.0f} $/MWh clears {degradation_mwh:.0f} $/MWh degradation, "
                f"est. margin ${margin:,.0f} for the window"
            )
        return "Selected by the optimizer, capacity reserved on the ledger"
    if state in ("COMMITTED", "DELIVERING"):
        return "Locked: this promise is kept even if a better price appears (K13)"
    if state == "FULFILLED":
        return "Delivered and verified"
    if state == "SHORTFALL":
        return "Delivered short; penalty applies"
    if state == "EXPIRED":
        return "Window started before a gate could commit it"
    if state == "OFFERED":
        if value is None:
            return "Awaiting the next gate; no price on this offer yet"
        if degradation_mwh is not None and value < degradation_mwh:
            return (
                f"Declined so far: {value:.2f} $/MWh does not cover {degradation_mwh:.0f} $/MWh degradation"
            )
        if row.get("decided_at"):
            return f"Evaluated at {value:.0f} $/MWh; waiting for headroom at the next gate"
        return f"Offered at {value:.0f} $/MWh; awaiting the next quarter-hour gate"
    return ""


def pipeline_view(obligations: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    """Group obligation rows into Kanban columns by state, across all customers concurrently (BUILD.md
    S2). FULFILLED and SHORTFALL share a terminal column so an operator sees each committed obligation's
    final outcome side by side."""
    columns: dict[str, list[dict[str, Any]]] = {state: [] for state in PIPELINE_STATES}
    for row in obligations:
        state = row.get("state", "OFFERED")
        column = "FULFILLED_OR_SHORTFALL" if state in ("FULFILLED", "SHORTFALL") else state
        if column not in columns:
            continue
        columns[column].append(
            {
                "obligation_id": row.get("obligation_id"),
                "customer_id": row.get("customer_id") or row.get("contract_id"),
                "service_type": row.get("service_type"),
                "tier": row.get("tier"),
                "committed_qty_kw": row.get("committed_qty_kw", row.get("requested_kw")),
                "window_start": row.get("window_start"),
                "window_end": row.get("window_end"),
                "at_risk": bool(row.get("at_risk", False)),
                "reason_code": row.get("last_reason_code") or row.get("reason_code"),
                # Energy columns (NOTICES 2026-09-25: energy is checked every cycle). Field contract with the
                # api owner in tests-e2e/ui/NEEDS_FROM_OTHER_OWNERS.md; absent fields render as "-".
                "raw_state": state,
                # K1 continuous energy-sufficiency (build brief item 5): per-committed-obligation energy
                # margin (kWh, available - required above reserve) and time-to-depletion (hours) at the
                # current committed draw rate. `None` when the API has not (yet) computed/attached these
                # -- see this module's docstring / the caller's final report for the API fields needed
                # (`energy_margin_kwh`, `time_to_depletion_h`) on `/og/api/dispatch/opportunities` rows.
                "energy_margin_kwh": row.get("energy_margin_kwh"),
                "time_to_depletion_h": row.get("time_to_depletion_h"),
                "decision": decision_line(row),
            }
        )
    shown = sum(len(items) for items in columns.values())
    return {
        "columns": [
            {
                "state": state,
                "label": _PIPELINE_LABELS[state],
                "items": columns[state],
                "count": len(columns[state]),
            }
            for state in PIPELINE_STATES
        ],
        "total": len(obligations),
        # EXPIRED/REJECTED/SETTLED rows have no column; say so instead of a total that does not add up
        # to the cards on the board (seen live: "7 obligations" over a board showing 1).
        "open": shown,
        "closed": len(obligations) - shown,
        "customer_count": len({row.get("customer_id") or row.get("contract_id") for row in obligations}),
        "generated_at": now.isoformat(),
    }


LedgerLevel = Literal["fleet", "zone", "bank", "hub"]
UNCOMMITTED_SERIES = "uncommitted capacity (kW)"
_HUBS_PER_BANK_DEFAULT = 50


def ledger_timeline_view(
    bank_id: str,
    reservations: list[dict[str, Any]],
    bank_capacity_kw: float,
    *,
    now: datetime,
    obligation_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Stacked-area ECharts option for one ledger scope: one series per obligation's committed kW plus
    the capacity nobody has bought yet (02b S8 screen 3; CR #19 item 3: the legend names each series by
    service and short id and calls the remainder "uncommitted capacity", never "free headroom").
    `reservations` may span several banks (fleet/zone scopes): amounts for the same obligation and
    interval are summed, so the chart is the aggregate the scope asks for."""
    by_interval: dict[str, dict[str, float]] = {}
    obligation_ids: list[str] = []
    for reservation in reservations:
        if reservation.get("kind") != "POWER_KW" or reservation.get("released_at"):
            continue
        obligation_id = reservation["obligation_id"]
        if obligation_id not in obligation_ids:
            obligation_ids.append(obligation_id)
        bucket = by_interval.setdefault(reservation["interval_start"], {})
        bucket[obligation_id] = bucket.get(obligation_id, 0.0) + float(reservation["amount"])
    intervals = sorted(by_interval)
    labels = obligation_labels or {}
    series: list[dict[str, Any]] = [
        {
            "name": labels.get(obligation_id) or obligation_id[:8],
            "type": "line",
            "stack": "ledger",
            "areaStyle": {},
            "showSymbol": False,
            "data": [round(by_interval[interval].get(obligation_id, 0.0), 3) for interval in intervals],
        }
        for obligation_id in obligation_ids
    ]
    committed_totals = [sum(by_interval[interval].values()) for interval in intervals]
    headroom = [round(max(bank_capacity_kw - total, 0.0), 3) for total in committed_totals]
    series.append(
        {
            "name": UNCOMMITTED_SERIES,
            "type": "line",
            "stack": "ledger",
            "areaStyle": {"opacity": 0.35},
            "lineStyle": {"type": "dashed"},
            "itemStyle": {"color": "token:--muted@0.55"},
            "showSymbol": False,
            "data": headroom,
        }
    )
    return {
        "bank_id": bank_id,
        "chart_option": {
            "xAxis": {"type": "category", "data": intervals},
            "yAxis": {"type": "value", "name": "kW"},
            "series": series,
            "legend": {"data": [s["name"] for s in series], "top": 0},
            "tooltip": {"trigger": "axis", "valueFormatter": "token:kw"},
            "grid": {"containLabel": True, "left": 8, "right": 12, "top": 44, "bottom": 8},
        },
        "obligation_count": len(obligation_ids),
        "capacity_kw": round(bank_capacity_kw, 3),
        "generated_at": now.isoformat(),
    }


HELD_SERIES = "Held (AS/toll)"


def apply_ledger_counts(scope: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Counts, title and drill-down lists from `GET /og/api/dispatch/ledger` itself (never from constants or
    a paged hub list): `hub_count`, `available_hub_count` and the child scopes (fleet -> zones, zone ->
    feeder segments)."""
    children = [str(c.get("id")) for c in payload.get("children") or [] if c.get("id")]
    hubs = int(payload.get("hub_count") or 0)
    available = payload.get("available_hub_count")
    level, sid = scope["level"], scope["id"]
    if level == "fleet":
        if children:
            scope["zones"] = children
        title = f"fleet ({len(children)} zones, {hubs:,} homes)"
    elif level == "zone":
        if children:
            scope["banks"] = children
        title = f"{sid} ({len(children)} feeder segments, {hubs:,} homes)"
    elif level == "bank":
        title = f"{sid} (feeder segment of {hubs:,} homes)"
    else:
        title = scope["title"]
    scope["title"] = title
    scope["hubs_in_scope"] = hubs
    scope["available_hubs"] = available
    return scope


def ledger_api_view(payload: dict[str, Any], title: str, *, now: datetime) -> dict[str, Any] | None:
    """`GET /og/api/dispatch/ledger` (`{level, id, now, timeline[{t, capacity_kw, committed_kw,
    uncommitted_capacity_kw, over_committed_kw, committed_by_service, unallocated_committed_kw}], ...}`)
    as the same stacked chart: committed kW per service, the capacity nobody has bought yet, and any
    over-commitment in its own red series. `None` when the body has no timeline."""
    timeline = payload.get("timeline")
    if not isinstance(timeline, list):
        return None
    xs = [_minutes(b.get("t")) for b in timeline]
    services = sorted({s for b in timeline for s in (b.get("committed_by_service") or {})})

    def col(key: str) -> list[float]:
        return [round(float(b.get(key) or 0.0), 3) for b in timeline]

    series: list[dict[str, Any]] = [
        {
            "name": service,
            "type": "line",
            "stack": "ledger",
            "areaStyle": {},
            "showSymbol": False,
            "data": [
                round(float((b.get("committed_by_service") or {}).get(service) or 0.0), 3) for b in timeline
            ],
        }
        for service in services
    ]
    # An AS award (or a toll) is a commitment granted 0 kW while held: its capacity is reserved but not
    # committed, so it shows here as reserved minus committed rather than disappearing from the chart.
    held = [
        round(max(float(b.get("reserved_kw") or 0.0) - float(b.get("committed_kw") or 0.0), 0.0), 3)
        for b in timeline
    ]
    if any(held):
        series.append(
            {
                "name": HELD_SERIES,
                "type": "line",
                "stack": "ledger",
                "areaStyle": {"opacity": 0.6},
                "itemStyle": {"color": "token:--status-caution@0.8"},
                "showSymbol": False,
                "data": held,
            }
        )
    if any(col("unallocated_committed_kw")):
        series.append(
            {
                "name": "Committed, not yet on a segment",
                "type": "line",
                "stack": "ledger",
                "areaStyle": {},
                "showSymbol": False,
                "data": col("unallocated_committed_kw"),
            }
        )
    series.append(
        {
            "name": UNCOMMITTED_SERIES,
            "type": "line",
            "stack": "ledger",
            "areaStyle": {"opacity": 0.35},
            "lineStyle": {"type": "dashed"},
            "itemStyle": {"color": "token:--muted@0.55"},
            "showSymbol": False,
            "data": col("uncommitted_capacity_kw"),
        }
    )
    if any(col("over_committed_kw")):
        series.append(
            {
                "name": "Over-committed",
                "type": "line",
                "showSymbol": False,
                "itemStyle": {"color": "token:--status-critical"},
                "data": col("over_committed_kw"),
            }
        )
    current = payload.get("now") or (timeline[-1] if timeline else {})
    return {
        "bank_id": title,
        "chart_option": {
            "xAxis": {"type": "category", "data": xs},
            "yAxis": {"type": "value", "name": "kW"},
            "series": series,
            "legend": {"data": [s["name"] for s in series], "top": 0},
            "tooltip": {"trigger": "axis", "valueFormatter": "token:kw"},
            "grid": {"containLabel": True, "left": 8, "right": 12, "top": 44, "bottom": 8},
        },
        "obligation_count": len(services),
        "series_noun": "service",
        "held_kw_now": round(
            max(float(current.get("reserved_kw") or 0.0) - float(current.get("committed_kw") or 0.0), 0.0), 3
        ),
        # The ledger only changes at the selector's gate (one bucket); stale only after two missed buckets.
        "stale_after_s": 2 * 60 * int(payload.get("bucket_minutes") or 15),
        "capacity_kw": round(float(current.get("capacity_kw") or 0.0), 3),
        "hub_count": payload.get("hub_count"),
        "available_hub_count": payload.get("available_hub_count"),
        "notes": payload.get("notes") or [],
        "generated_at": now.isoformat(),
    }


def obligation_labels(obligations: list[dict[str, Any]]) -> dict[str, str]:
    """`obligation_id -> "ERCOT_AS 8937a346"` for the ledger legend, from the pipeline rows."""
    out: dict[str, str] = {}
    for row in obligations:
        oid = str(row.get("obligation_id") or "")
        if oid:
            out[oid] = f"{row.get('service_type') or 'obligation'} {oid[:8]}"
    return out


def ledger_scope(level: str | None, scope_id: str | None, hubs: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve the aggregate-first ledger scope (CR #19 item 3: fleet -> zone -> bank -> hub) to the set
    of banks it covers, plus breadcrumb and caption data. A bank is a feeder segment aggregating its
    hubs (the seeded fleet: 50 homes per 600 kVA segment), never a single hub, and the caption says so."""
    banks: dict[str, dict[str, Any]] = {}
    for h in hubs:
        b = str(h.get("bank_id") or "")
        if not b:
            continue
        entry = banks.setdefault(b, {"bank_id": b, "zone": str(h.get("zone") or ""), "hubs": 0})
        entry["hubs"] += 1
    zones = sorted({b["zone"] for b in banks.values() if b["zone"]})
    lvl: str = level if level in ("fleet", "zone", "bank", "hub") else "fleet"
    sid = (scope_id or "").strip()
    hub_bank: str | None = None
    if lvl == "hub":
        match = next((h for h in hubs if str(h.get("hub_id")) == sid), None)
        hub_bank = str(match.get("bank_id")) if match else None
        if hub_bank is None:
            lvl = "fleet"
    if lvl == "zone" and sid not in zones:
        lvl = "fleet"
    if lvl == "bank" and sid not in banks:
        lvl = "fleet"
    if lvl == "fleet":
        covered = sorted(banks)
    elif lvl == "zone":
        covered = sorted(b for b, e in banks.items() if e["zone"] == sid)
    elif lvl == "bank":
        covered = [sid]
    else:
        covered = [hub_bank or ""]
    zone_of_bank = (
        banks.get(covered[0], {}).get("zone") if lvl in ("bank", "hub") and covered else None
    ) or ""
    crumbs = [{"label": "Fleet", "level": "fleet", "id": ""}]
    if lvl in ("zone", "bank", "hub"):
        z = sid if lvl == "zone" else zone_of_bank
        if z:
            crumbs.append({"label": z, "level": "zone", "id": z})
    if lvl in ("bank", "hub"):
        crumbs.append({"label": covered[0], "level": "bank", "id": covered[0]})
    if lvl == "hub":
        crumbs.append({"label": sid, "level": "hub", "id": sid})
    hubs_in_scope = sum(banks[b]["hubs"] for b in covered if b in banks)
    if lvl == "fleet":
        title = f"fleet ({len(covered)} feeder segments, {hubs_in_scope} homes)"
    elif lvl == "zone":
        title = f"{sid} ({len(covered)} feeder segments, {hubs_in_scope} homes)"
    elif lvl == "bank":
        title = f"{sid} (feeder segment of {hubs_in_scope or _HUBS_PER_BANK_DEFAULT} homes)"
    else:
        title = f"{sid} on {covered[0]} (the ledger is kept per feeder segment)"
    return {
        "level": lvl,
        "id": sid if lvl != "fleet" else "",
        "banks": covered,
        "zones": zones,
        "all_banks": sorted(banks),
        "crumbs": crumbs,
        "title": title,
        "hubs_in_scope": hubs_in_scope,
    }


async def _aggregate_timeline(banks: list[str]) -> tuple[list[dict[str, Any]], float, list[str]]:
    """Sum the per-bank ledger timelines of `banks`: reservations concatenated (the view sums same
    obligation/interval amounts), capacity summed. Returns (reservations, capacity_kw, failed_banks).
    ponytail: N calls to the per-bank endpoint until `GET /og/api/dispatch/ledger?level=` lands."""

    async def one(bank: str) -> tuple[str, dict[str, Any] | None]:
        try:
            body = await get_json(f"/og/api/ledger/{bank}/timeline")
            return bank, body if isinstance(body, dict) else None
        except ApiUnavailable as exc:
            logger.warning("dispatch: /og/api/ledger/%s/timeline unavailable: %s", bank, exc)
            return bank, None

    results = await asyncio.gather(*(one(b) for b in banks))
    reservations: list[dict[str, Any]] = []
    capacity = 0.0
    failed: list[str] = []
    for bank, body in results:
        if body is None:
            failed.append(bank)
            continue
        reservations.extend(body.get("reservations", []))
        capacity += float(body.get("bank_capacity_kw", 0.0))
    return reservations, capacity, failed


def _minutes(value: Any) -> str:
    """An ISO timestamp cut to minutes for display (the raw value keeps microseconds)."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(str(value)).isoformat(timespec="minutes")
    except ValueError:
        return str(value)


async def _default_bank_id() -> str:
    """The first real bank id from the fleet, so the ledger timeline opens on a bank that exists (the
    old fixed placeholder `BANK-0001` matched nothing live). Falls back to the placeholder if the fleet
    read fails; the screen then shows its degraded banner from the ledger call as before."""
    try:
        raw = await get_json("/og/api/fleet/hubs")
    except ApiUnavailable:
        return _DEFAULT_BANK_ID
    hubs = raw.get("items", []) if isinstance(raw, dict) else []
    bank_ids = sorted({str(h["bank_id"]) for h in hubs if h.get("bank_id")})
    return bank_ids[0] if bank_ids else _DEFAULT_BANK_ID


def plan_view(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Latest selector plan panel: LP mode vs rule-fallback baseline, solver diagnostics (02b S8
    screen 3)."""
    if plan is None:
        return {"has_plan": False}
    is_lp = plan.get("plan_mode") in ("L-DA", "L-ID")
    solver_gap = plan.get("solver_gap")
    return {
        "has_plan": True,
        "plan_id": plan.get("plan_id"),
        "plan_mode": plan.get("plan_mode"),
        "mode_label": "MILP optimizer" if is_lp else "Rule-based fallback",
        "is_lp": is_lp,
        "gate_kind": plan.get("gate_kind"),
        "horizon_start": plan.get("horizon_start"),
        "horizon_end": plan.get("horizon_end"),
        "horizon_display": f"{_minutes(plan.get('horizon_start'))} to {_minutes(plan.get('horizon_end'))}",
        "solver_status": plan.get("solver_status"),
        "solver_gap_pct": float(solver_gap) * 100 if solver_gap is not None else None,
        "solver_time_ms": plan.get("solver_time_ms"),
        "objective_value": plan.get("objective_value"),
    }


def _grant_sort_key(grant: dict[str, Any]) -> tuple[str, str]:
    return (grant.get("cycle_id") or "", grant.get("grant_id") or "")


def grants_and_substitutions_view(grants: list[dict[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    """Real-time grants/substitutions feed (02b S8 screen 3). A grant re-assigning an obligation to a
    different bank than its previous grant is surfaced as a substitution -- detected in chronological
    (oldest-first) order so the *later* grant is the one flagged, then displayed newest-first."""
    kind_by_grant_id: dict[str, str] = {}
    seen_obligation_bank: dict[str, str] = {}
    for grant in sorted(grants, key=_grant_sort_key):
        if grant.get("is_headroom"):
            kind_by_grant_id[grant["grant_id"]] = "headroom"
            continue
        obligation_id = grant.get("obligation_id")
        substitution = bool(
            obligation_id
            and obligation_id in seen_obligation_bank
            and seen_obligation_bank[obligation_id] != grant["bank_id"]
        )
        if obligation_id:
            seen_obligation_bank[obligation_id] = grant["bank_id"]
        kind_by_grant_id[grant["grant_id"]] = "substitution" if substitution else "commitment"

    ordered = sorted(grants, key=_grant_sort_key, reverse=True)[:limit]
    return [
        {
            "grant_id": grant["grant_id"],
            "cycle_id": grant.get("cycle_id"),
            "obligation_id": grant.get("obligation_id"),
            "bank_id": grant["bank_id"],
            "granted_kw": grant.get("granted_kw"),
            "kind": kind_by_grant_id[grant["grant_id"]],
        }
        for grant in ordered
    ]


def commitment_lock_events_view(commitments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Commitment-lock events (K13): a commitment whose reason code starts with R-COMMIT-LOCK, or that
    supersedes a prior commitment, is a lock-driven event worth surfacing to the operator verbatim."""
    events: list[dict[str, Any]] = []
    for commitment in commitments:
        reason = commitment.get("reason_code", "")
        if not (reason.startswith(_LOCK_REASON_PREFIX) or commitment.get("supersedes")):
            continue
        events.append(
            {
                "commitment_id": commitment["commitment_id"],
                "obligation_id": commitment["obligation_id"],
                "reason_code": reason,
                "supersedes": commitment.get("supersedes"),
                "committed_kw": commitment.get("committed_kw"),
                "interval_start": commitment.get("interval_start"),
            }
        )
    return events


#: Hold hours and deployment caps per product live in `opengrid.core.services` (one owner), keyed by the
#: canonical product (`canonical_product`: NONSPIN / NON_SPIN are NSPIN).
_AS_HOLD_HOURS = AS_HOLD_HOURS
#: Only an AWARDED AS obligation is a hold that can be deployed; an OFFERED one is just an offer.
_TOLL_SERVICE_TYPE = REGULATED_CAPACITY_SERVICE_TYPE
_TOLL_VARIANT = "TOLLING"
_AS_AWARDED_STATES = frozenset({"COMMITTED", "DELIVERING", "SHORTFALL"})


def contract_products(contracts: Any) -> dict[str, str]:
    """`contract_id -> variant` from `GET /og/api/contracts` (the AS product lives on the contract)."""
    rows = contracts if isinstance(contracts, list) else []
    return {
        str(c["contract_id"]): str(c["variant"])
        for c in rows
        if isinstance(c, dict) and c.get("contract_id") and c.get("variant")
    }


def as_awards_view(
    opportunities: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    *,
    now: datetime,
    product_by_contract: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Join ERCOT_AS awards with the active deployment records for the operator panel.

    Awards are held until a deployment exists.  The API owns the authoritative award and deployment
    state; this view only joins the two read models and preserves optional energy-risk fields when the
    allocator supplies them.
    """
    active_by_obligation = {
        str(row.get("obligation_id")): row for row in deployments if row.get("obligation_id") is not None
    }
    all_deployment = next((row for row in deployments if row.get("obligation_id") is None), None)
    rows: list[dict[str, Any]] = []
    for award in opportunities:
        if award.get("state") not in _AS_AWARDED_STATES:
            continue
        product_hint = str(
            award.get("product")
            or award.get("variant")
            or (product_by_contract or {}).get(str(award.get("contract_id") or ""))
            or ""
        ).upper()
        # D-29: a tolling obligation (REGULATED_CAPACITY, contract variant TOLLING) is deployed by a
        # utility's call through the same route; anything else that is not an ERCOT_AS award is skipped.
        is_toll = award.get("service_type") == _TOLL_SERVICE_TYPE and product_hint == _TOLL_VARIANT
        if award.get("service_type") != ERCOT_AS_SERVICE_TYPE and not is_toll:
            continue
        obligation_id = str(award.get("obligation_id") or "")
        deployment = active_by_obligation.get(obligation_id) or all_deployment
        state = "deployed" if deployment else "held"
        # Obligation rows carry no product: it is the contract's variant (ECRS, NSPIN, ...), looked up from
        # GET /og/api/contracts. An unknown product shows as such rather than defaulting to Non-Spin's 4 h.
        product = str(
            award.get("product")
            or award.get("variant")
            or (product_by_contract or {}).get(str(award.get("contract_id") or ""))
            or ""
        ).upper()
        required_hours: int | None = _AS_HOLD_HOURS.get(canonical_product(product) or "")
        energy_held = _f(award.get("energy_held_kwh"))
        required_energy = _f(award.get("required_energy_kwh"))
        if required_energy is None:
            committed_kw = _f(award.get("committed_qty_kw")) or _f(award.get("requested_kw")) or 0.0
            required_energy = committed_kw * required_hours if required_hours is not None else None
        at_risk = bool(award.get("at_risk", False))
        if energy_held is not None and required_energy is not None:
            at_risk = at_risk or energy_held < required_energy
        margin = _f(award.get("energy_margin_kwh"))
        if margin is not None:
            at_risk = at_risk or margin < 0
        rows.append(
            {
                "obligation_id": obligation_id,
                "customer_id": award.get("customer_id") or award.get("contract_id"),
                "product": product or "ERCOT_AS",
                "committed_kw": award.get("committed_qty_kw", award.get("requested_kw")),
                "energy_held_kwh": energy_held,
                "energy_margin_kwh": _f(award.get("energy_margin_kwh")),
                "required_energy_kwh": required_energy,
                "required_hours": required_hours,
                "max_minutes": AS_MAX_DEPLOY_MINUTES.get(canonical_product(product) or ""),
                "at_risk": at_risk,
                "state": state,
                "deployment_id": str(deployment["deployment_id"]) if deployment else None,
                "deployment_end": deployment.get("end_at") if deployment else None,
                # D-33: who called it -- OPERATOR, UTILITY (customer API), GRID_LINK, ERCOT, MARKET_SIM, SCENARIO.
                "deployment_origin": deployment.get("source") if deployment else None,
                "deployment_by": deployment.get("requested_by") if deployment else None,
                "deployment_kw": deployment.get("requested_kw") if deployment else None,
                "utility_call": is_toll,
            }
        )
    return rows


def _require_operator(request: Request) -> None:
    if not is_operator(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="operator role required")


def _action_result(request: Request, *, message: str, ok: bool = False) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_partials/as_deployment_result.html",
        {"message": message, "ok": ok},
    )


@router.post("/as-deployments/propose", response_class=HTMLResponse)
async def propose_as_deployment(
    request: Request,
    obligation_id: str = Form(default=""),
    duration_minutes: int = Form(default=15),
    reason: str = Form(...),
    product: str = Form(default=""),
) -> HTMLResponse:
    """Step 1: render the exact AS deployment summary without changing system state. One award at a time:
    the API refuses a fleet-wide (ALL) deployment with 409, so the console never offers it. The duration is
    capped by the award's product rule (the API remains the authority)."""
    _require_operator(request)
    if not obligation_id.strip():
        return _action_result(request, message="Choose the held award to deploy (one award per deployment).")
    cap = AS_MAX_DEPLOY_MINUTES.get(canonical_product(product) or "", 240)
    if not 1 <= duration_minutes <= cap:
        label = product.strip().upper() or "this product"
        return _action_result(request, message=f"Duration must be between 1 and {cap} minutes for {label}.")
    return templates.TemplateResponse(
        request,
        "_partials/as_deployment_confirm.html",
        {
            "obligation_id": obligation_id,
            "duration_minutes": duration_minutes,
            "reason": reason,
            "confirm_url": f"{BASE_PATH}/dispatch/as-deployments/confirm",
            "summary": (
                (
                    f"Issue the utility's call on tolling obligation {obligation_id[:8]} for {duration_minutes} minutes"
                    if product.strip().upper() == _TOLL_VARIANT
                    else f"Deploy award {obligation_id[:8]} ({product.strip().upper() or 'ERCOT_AS'}) for {duration_minutes} minutes"
                )
                + f" ({reason})"
            ),
        },
    )


@router.post("/as-deployments/confirm", response_class=HTMLResponse)
async def confirm_as_deployment(
    request: Request,
    obligation_id: str = Form(default=""),
    duration_minutes: int = Form(...),
    reason: str = Form(...),
) -> HTMLResponse:
    """Step 2: create the deployment through the existing operator API."""
    _require_operator(request)
    try:
        result = await post_json(
            "/og/api/dispatch/as-deployments",
            {
                "obligation_id": obligation_id or None,
                "duration_minutes": duration_minutes,
                "reason": reason,
            },
            remote_user=remote_user(request),
        )
    except ApiUnavailable as exc:
        if exc.status_code in (404, 409, 422, 429):
            # D-33 refusal from the shared call path: {"detail": {"reason_code", "detail"}} -- overlap,
            # over the product cap, not deployable, sign/size, window or rate limit.
            detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail
            if isinstance(detail, dict):
                detail = f"{detail.get('reason_code', '')}: {detail.get('detail', '')}"
            return _action_result(request, message=f"Refused by the API: {detail or exc}")
        return _action_result(request, message=str(exc))
    return _action_result(
        request,
        message=f"Deployment {result.get('deployment_id', 'accepted')} is active.",
        ok=True,
    )


@router.post("/as-deployments/{deployment_id}/stop-propose", response_class=HTMLResponse)
async def propose_stop_as_deployment(request: Request, deployment_id: str) -> HTMLResponse:
    _require_operator(request)
    return templates.TemplateResponse(
        request,
        "_partials/as_deployment_confirm.html",
        {
            "obligation_id": "",
            "duration_minutes": "",
            "reason": "",
            "deployment_id": deployment_id,
            "confirm_url": f"{BASE_PATH}/dispatch/as-deployments/{deployment_id}/stop-confirm",
            "summary": f"Stop active ERCOT_AS deployment {deployment_id} early",
            "stop": True,
        },
    )


@router.post("/as-deployments/{deployment_id}/stop-confirm", response_class=HTMLResponse)
async def confirm_stop_as_deployment(request: Request, deployment_id: str) -> HTMLResponse:
    _require_operator(request)
    try:
        await delete_json(
            f"/og/api/dispatch/as-deployments/{deployment_id}", remote_user=remote_user(request)
        )
    except ApiUnavailable as exc:
        return _action_result(request, message=str(exc))
    return _action_result(request, message=f"Deployment {deployment_id} stopped.", ok=True)


@router.get("", response_class=HTMLResponse)
async def dispatch_page(
    request: Request,
    bank_id: str | None = Query(default=None),
    level: str | None = Query(default=None),
    id: str | None = Query(default=None),
) -> HTMLResponse:
    """Dispatch & commitments screen (`/og/dispatch`, viewer role read-only in MVP-S). The ledger panel
    is aggregate-first (CR #19 item 3): `?level=fleet|zone|bank|hub&id=` selects the scope, default
    fleet; the legacy `?bank_id=` still opens a bank."""
    now = datetime.now(tz=UTC)
    if bank_id and not level:
        level, id = "bank", bank_id
    degraded: str | None = None
    obligations: list[dict[str, Any]] = []
    plan: dict[str, Any] | None = None
    reservations: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    bank_capacity_kw = 0.0
    commitments: list[dict[str, Any]] = []
    deployments: list[dict[str, Any]] = []
    # Every hub, from the fleet map (the hub list is paged at 200, which undercounted the fleet); the hub
    # list only as a fallback when the map is not serving.
    hubs: list[dict[str, Any]] = []
    try:
        raw_map = await get_json("/og/api/fleet/map")
        hubs = list(raw_map.get("hubs") or []) if isinstance(raw_map, dict) else []
    except ApiUnavailable as exc:
        logger.info("dispatch: /og/api/fleet/map unavailable (%s); using the hub list", exc)
    if not hubs:
        try:
            raw_hubs = await get_json("/og/api/fleet/hubs", params={"limit": 2000})
            hubs = raw_hubs.get("items", []) if isinstance(raw_hubs, dict) else raw_hubs or []
        except ApiUnavailable as exc:
            logger.warning("dispatch: /og/api/fleet/hubs unavailable: %s", exc)
    scope = ledger_scope(level, id, hubs)
    if not scope["banks"] or scope["banks"] == [""]:
        scope["banks"] = [bank_id or await _default_bank_id()]
    bank_id = scope["banks"][0]

    try:
        raw = await get_json("/og/api/dispatch/opportunities")
        obligations = raw.get("items", raw) if isinstance(raw, dict) else raw or []
    except ApiUnavailable as exc:
        logger.warning("dispatch: /og/api/dispatch/opportunities unavailable: %s", exc)
        degraded = str(exc)

    try:
        plan = await get_json("/og/api/dispatch/plan/latest")
    except ApiUnavailable as exc:
        logger.warning("dispatch: /og/api/dispatch/plan/latest unavailable: %s", exc)
        degraded = degraded or str(exc)

    # Aggregate ledger: the CR #19 endpoint when it exists, else the per-bank timelines summed.
    aggregate_loaded = False
    ledger_payload: dict[str, Any] | None = None
    try:
        ledger_params = {"level": scope["level"], **({"id": scope["id"]} if scope["id"] else {})}
        agg = await get_json("/og/api/dispatch/ledger", params=ledger_params)
        if isinstance(agg, dict) and isinstance(agg.get("timeline"), list):
            ledger_payload = agg
            aggregate_loaded = True
        elif isinstance(agg, dict) and "reservations" in agg:
            reservations = agg.get("reservations", [])
            bank_capacity_kw = float(agg.get("capacity_kw") or agg.get("bank_capacity_kw") or 0.0)
            aggregate_loaded = True
    except ApiUnavailable as exc:
        if exc.status_code not in (404, 405):
            logger.info("dispatch: aggregate ledger endpoint not available yet: %s", exc)
    if not aggregate_loaded:
        reservations, bank_capacity_kw, failed = await _aggregate_timeline(scope["banks"])
        if failed and len(failed) == len(scope["banks"]):
            degraded = degraded or "GET /og/api/ledger/<bank>/timeline failed for every bank in scope"
        scope["banks_unavailable"] = failed

    if ledger_payload is not None:
        apply_ledger_counts(scope, ledger_payload)

    # Grants and lock events stay per bank (the first bank in scope): they list rows, not a curve.
    try:
        raw_deployments = await get_json("/og/api/dispatch/as-deployments")
        deployments = raw_deployments if isinstance(raw_deployments, list) else []
    except ApiUnavailable as exc:
        logger.warning("dispatch: AS deployment API unavailable: %s", exc)
        degraded = degraded or str(exc)

    try:
        timeline = await get_json(f"/og/api/ledger/{bank_id}/timeline")
        if isinstance(timeline, dict):
            grants = timeline.get("grants", [])
            commitments = timeline.get("commitments", [])
    except ApiUnavailable as exc:
        logger.warning("dispatch: /og/api/ledger/%s/timeline unavailable: %s", bank_id, exc)
        degraded = degraded or str(exc)

    products: dict[str, str] = {}
    try:
        products = contract_products(await get_json("/og/api/contracts"))
    except ApiUnavailable as exc:
        logger.info("dispatch: contracts unavailable for AS product names: %s", exc)

    return templates.TemplateResponse(
        request,
        "dispatch.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "bank_id": bank_id,
            "scope": scope,
            "pipeline": pipeline_view(obligations if isinstance(obligations, list) else [], now=now),
            "plan": plan_view(plan if isinstance(plan, dict) else None),
            "ledger": (ledger_api_view(ledger_payload, scope["title"], now=now) if ledger_payload else None)
            or ledger_timeline_view(
                scope["title"],
                reservations,
                bank_capacity_kw,
                now=now,
                obligation_labels=obligation_labels(obligations if isinstance(obligations, list) else []),
            ),
            "grants": grants_and_substitutions_view(grants),
            "lock_events": commitment_lock_events_view(commitments),
            "as_awards": as_awards_view(
                obligations if isinstance(obligations, list) else [],
                deployments,
                now=now,
                product_by_contract=products,
            ),
            "as_deployments": deployments,
            "degraded": degraded,
        },
    )
