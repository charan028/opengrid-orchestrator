"""Screen 0: Story (`/og/story`). Owner: ui-a.

The one screen a judge, a new operator or a member's advocate reads first. With the live system's own
numbers it answers, in order: what the orchestrator is for, what it is doing this minute, what it
declined to do and why, who it serves, where it runs, and how it decides. Every figure is the same
read the operational screens make (health, obligations, settlement, hubs, the price series), shown
with its age, so this is a narrative over live data and never a second source of truth. Nothing here
is a control: a viewer sees exactly what an operator sees.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json
from opengrid.ui.role import role_of
from opengrid.ui.routes.markets import _SERIES_QUERY, series_chart_view
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

#: Obligation states that are a live claim on the fleet's capacity right now.
LIVE_STATES: tuple[str, ...] = ("SELECTED", "COMMITTED", "DELIVERING")

#: The buyer catalogue (brief S3.1): every customer type the dispatch-profile catalogue can serve. The
#: screen shows which of these hold a live claim on this fleet today, and which are only catalogued.
SERVICE_LABELS: dict[str, str] = {
    "HOME": "Homes (their own backup reserve)",
    "ERCOT_ENERGY": "ERCOT wholesale energy",
    "ERCOT_AS": "ERCOT ancillary services",
    "PARTNER_CAPACITY": "Partner capacity",
    "DIST_DEFERRAL": "Distribution deferral",
    "REGULATED_CAPACITY": "Regulated utility capacity",
    "DATA_CENTER": "Data centre support",
    "LARGE_LOAD": "Large loads",
    "PIPELINE_AC": "Pipeline AC smoothing",
    "MOBILE_TEEEF": "Mobile storage",
    "PJM_CAPACITY": "PJM capacity",
}

#: The decision pipeline, one row per independent process, in the order a kilowatt travels. `process`
#: is the key `GET /og/api/health` uses in its `processes` map (`opengrid.health`).
STAGES: tuple[dict[str, str], ...] = (
    {
        "key": "feeds",
        "process": "feeds",
        "label": "Read the market",
        "blurb": (
            "ERCOT prices per load zone, load, wind and solar; EIA demand; NWS weather. Validated on "
            "arrival, quarantined when implausible, never smoothed or clipped."
        ),
    },
    {
        "key": "engine",
        "process": "engine",
        "label": "Plan, then allocate",
        "blurb": (
            "A mixed-integer program chooses which offers to take under P10/P50/P90 scenarios. Then, "
            "every two seconds, the allocator gives each kilowatt to exactly one obligation."
        ),
    },
    {
        "key": "guardian",
        "process": "guardian",
        "label": "Check and sign",
        "blurb": (
            "An independent process re-checks every batch against the physical envelope and every "
            "home's reserve, then signs it. A veto makes publishing impossible, not merely blocked."
        ),
    },
    {
        "key": "safestop",
        "process": "safestop",
        "label": "Stop, even when the signer is down",
        "blurb": (
            "A scoped safe stop with its own stop-only key. It can only stop, never release; release "
            "needs the guardian and two people."
        ),
    },
    {
        "key": "settle",
        "process": "settle",
        "label": "Measure, bill, prove",
        "blurb": (
            "Delivery is metered, priced with degradation and penalties, and every decision is written "
            "to a hash chain that anyone can verify from the Billing screen."
        ),
    },
)


def _num(value: Any, default: float = 0.0) -> float:
    with contextlib.suppress(TypeError, ValueError):
        return float(value)
    return default


def _process_status(processes: Any, key: str) -> str:
    """The health status of one pipeline stage: the API's `processes` map is keyed by the short name
    (`feeds`) on the current stack and by the unit name (`og-feeds`) on older ones."""
    if not isinstance(processes, dict):
        return "unknown"
    entry = processes.get(key) or processes.get(f"og-{key}")
    if isinstance(entry, dict):
        return str(entry.get("status") or "unknown")
    return "unknown"


def story_page_view(
    health: dict[str, Any],
    obligations: list[dict[str, Any]],
    settlement: dict[str, Any],
    hubs: list[dict[str, Any]],
    ticker_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the Story screen says, derived from the reads the operational screens already make.
    Pure: no I/O, so the narrative can be pinned exactly in a unit test."""
    counts = health.get("hub_health_counts") or {}
    hubs_total = sum(int(v or 0) for v in counts.values()) or len(hubs)
    hubs_online = int(counts.get("online") or 0)

    live = [o for o in obligations if o.get("state") in LIVE_STATES]
    by_service: dict[str, dict[str, Any]] = {}
    for o in live:
        service = str(o.get("service_type") or "OTHER")
        row = by_service.setdefault(
            service,
            {
                "service_type": service,
                "label": SERVICE_LABELS.get(service, service),
                "kw": 0.0,
                "count": 0,
                "delivering": 0,
                "at_risk": 0,
                "_buyers": set(),
            },
        )
        row["kw"] += _num(o.get("committed_qty_kw"))
        row["count"] += 1
        row["delivering"] += 1 if o.get("state") == "DELIVERING" else 0
        row["at_risk"] += 1 if o.get("at_risk") else 0
        row["_buyers"].add(o.get("contract_id"))
    buyers = []
    for row in sorted(by_service.values(), key=lambda r: -r["kw"]):
        row["buyers"] = len(row.pop("_buyers"))
        row["kw"] = round(row["kw"], 1)
        buyers.append(row)

    promised_kw = round(sum(r["kw"] for r in buyers), 1)
    distinct_buyers = len({o.get("contract_id") for o in live})
    live_services = {r["service_type"] for r in buyers}
    catalogue = [
        {"service_type": key, "label": label, "live": key in live_services}
        for key, label in SERVICE_LABELS.items()
    ]

    zones = sorted({str(h.get("zone")) for h in hubs if h.get("zone")})

    pnl_rows = settlement.get("pnl_rows") or []
    net_value = round(sum(_num(r.get("net_value")) for r in pnl_rows), 2)
    forgone_upside = round(sum(_num(r.get("forgone_upside")) for r in pnl_rows), 2)
    with_baseline = [r for r in pnl_rows if r.get("rule_baseline_value") is not None]
    value_of_orchestration = (
        round(sum(_num(r.get("net_value")) - _num(r.get("rule_baseline_value")) for r in with_baseline), 2)
        if with_baseline
        else None
    )
    period = settlement.get("period") if isinstance(settlement.get("period"), dict) else {}

    promises = {
        "reserve_breaches": int(health.get("reserve_breaches") or 0),
        "double_sold_kwh": int(_num(health.get("double_sold_kwh"))),
        "commitment_switches": int(health.get("commitment_switches") or 0),
    }

    processes = health.get("processes") or {}
    stages = [{**stage, "status": _process_status(processes, stage["process"])} for stage in STAGES]
    stages_ok = sum(1 for s in stages if s["status"] == "ok")

    ticker = series_chart_view(ticker_rows, series_key="price")

    return {
        "hubs_online": hubs_online,
        "hubs_total": hubs_total,
        "fleet_mw": health.get("fleet_mw"),
        "fleet_mwh": health.get("fleet_mwh"),
        "live_obligations": len(live),
        "distinct_buyers": distinct_buyers,
        "promised_kw": promised_kw,
        "buyers": buyers,
        "catalogue": catalogue,
        "catalogue_live_count": len(live_services),
        "zones": zones,
        "promises": promises,
        "promises_kept": not any(promises.values()),
        "active_commitments": health.get("active_commitments"),
        "net_margin_usd": health.get("net_margin_usd"),
        "net_value": net_value,
        "forgone_upside": forgone_upside,
        "value_of_orchestration": value_of_orchestration,
        "pnl_rows": len(pnl_rows),
        "period": period,
        "stages": stages,
        "stages_ok": stages_ok,
        "ticker": ticker,
        "health_as_of": health.get("as_of"),
        "invariants_as_of": health.get("invariants_checked_at") or health.get("as_of"),
        "settlement_as_of": settlement.get("as_of"),
    }


@router.get("/story", response_class=HTMLResponse)
async def story(request: Request) -> HTMLResponse:
    degraded: str | None = None
    health: dict[str, Any] = {}
    obligations: list[dict[str, Any]] = []
    settlement: dict[str, Any] = {}
    hubs: list[dict[str, Any]] = []
    ticker_rows: list[dict[str, Any]] = []

    try:
        raw = await get_json("/og/api/health")
        health = raw if isinstance(raw, dict) else {}
    except ApiUnavailable as exc:
        logger.warning("story: /og/api/health unavailable: %s", exc)
        degraded = str(exc)

    try:
        raw = await get_json("/og/api/dispatch/opportunities")
        obligations = raw if isinstance(raw, list) else []
    except ApiUnavailable as exc:
        logger.warning("story: obligations unavailable: %s", exc)
        degraded = degraded or str(exc)

    # The rest never degrade the screen: a missing read leaves its own section honest and empty.
    try:
        raw = await get_json("/og/api/views/settlement")
        settlement = raw if isinstance(raw, dict) else {}
    except ApiUnavailable as exc:
        logger.info("story: settlement view unavailable (%s); the money section stays empty", exc)

    try:
        raw = await get_json("/og/api/fleet/hubs")
        hubs = raw.get("items", []) if isinstance(raw, dict) else []
    except ApiUnavailable as exc:
        logger.info("story: hub list unavailable (%s); zones come from health only", exc)

    try:
        raw = await get_json("/og/api/markets/series", params=_SERIES_QUERY["price"])
        ticker_rows = raw if isinstance(raw, list) else []
    except ApiUnavailable as exc:
        logger.info("story: price series unavailable (%s); the ticker stays empty", exc)

    return templates.TemplateResponse(
        request,
        "story.html",
        {
            "role": role_of(request),
            "degraded": degraded,
            "s": story_page_view(health, obligations, settlement, hubs, ticker_rows),
        },
    )
