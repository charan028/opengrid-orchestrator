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

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

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
                "raw_state": state,
                # K1 continuous energy-sufficiency (build brief item 5): per-committed-obligation energy
                # margin (kWh, available - required above reserve) and time-to-depletion (hours) at the
                # current committed draw rate. `None` when the API has not (yet) computed/attached these
                # -- see this module's docstring / the caller's final report for the API fields needed
                # (`energy_margin_kwh`, `time_to_depletion_h`) on `/og/api/dispatch/opportunities` rows.
                "energy_margin_kwh": row.get("energy_margin_kwh"),
                "time_to_depletion_h": row.get("time_to_depletion_h"),
            }
        )
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
        "customer_count": len({row.get("customer_id") or row.get("contract_id") for row in obligations}),
        "generated_at": now.isoformat(),
    }


def ledger_timeline_view(
    bank_id: str, reservations: list[dict[str, Any]], bank_capacity_kw: float, *, now: datetime
) -> dict[str, Any]:
    """Stacked-area ECharts option: one series per obligation's committed y-hat plus a headroom series
    for uncommitted capacity, per bank (02b S8 screen 3 ledger timeline)."""
    by_interval: dict[str, dict[str, float]] = {}
    obligation_ids: list[str] = []
    for reservation in reservations:
        if reservation.get("kind") != "POWER_KW" or reservation.get("released_at"):
            continue
        obligation_id = reservation["obligation_id"]
        if obligation_id not in obligation_ids:
            obligation_ids.append(obligation_id)
        by_interval.setdefault(reservation["interval_start"], {})[obligation_id] = float(
            reservation["amount"]
        )
    intervals = sorted(by_interval)
    series: list[dict[str, Any]] = [
        {
            "name": obligation_id,
            "type": "line",
            "stack": "ledger",
            "areaStyle": {},
            "showSymbol": False,
            "data": [by_interval[interval].get(obligation_id, 0.0) for interval in intervals],
        }
        for obligation_id in obligation_ids
    ]
    committed_totals = [sum(by_interval[interval].values()) for interval in intervals]
    headroom = [max(bank_capacity_kw - total, 0.0) for total in committed_totals]
    series.append(
        {
            "name": "free headroom",
            "type": "line",
            "stack": "ledger",
            "areaStyle": {},
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
            "legend": {},
            "tooltip": {"trigger": "axis"},
        },
        "obligation_count": len(obligation_ids),
        "generated_at": now.isoformat(),
    }


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
        "mode_label": "LP optimizer" if is_lp else "Rule-based fallback",
        "is_lp": is_lp,
        "gate_kind": plan.get("gate_kind"),
        "horizon_start": plan.get("horizon_start"),
        "horizon_end": plan.get("horizon_end"),
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


@router.get("", response_class=HTMLResponse)
async def dispatch_page(request: Request, bank_id: str = Query(default=_DEFAULT_BANK_ID)) -> HTMLResponse:
    """Dispatch & commitments screen (`/og/dispatch`, viewer role read-only in MVP-S)."""
    now = datetime.now(tz=UTC)
    degraded: str | None = None
    obligations: list[dict[str, Any]] = []
    plan: dict[str, Any] | None = None
    reservations: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    bank_capacity_kw = 0.0
    commitments: list[dict[str, Any]] = []

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

    try:
        timeline = await get_json(f"/og/api/ledger/{bank_id}/timeline")
        if isinstance(timeline, dict):
            reservations = timeline.get("reservations", [])
            grants = timeline.get("grants", [])
            bank_capacity_kw = float(timeline.get("bank_capacity_kw", 0.0))
            commitments = timeline.get("commitments", [])
    except ApiUnavailable as exc:
        logger.warning("dispatch: /og/api/ledger/%s/timeline unavailable: %s", bank_id, exc)
        degraded = degraded or str(exc)

    return templates.TemplateResponse(
        request,
        "dispatch.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "bank_id": bank_id,
            "pipeline": pipeline_view(obligations if isinstance(obligations, list) else [], now=now),
            "plan": plan_view(plan if isinstance(plan, dict) else None),
            "ledger": ledger_timeline_view(bank_id, reservations, bank_capacity_kw, now=now),
            "grants": grants_and_substitutions_view(grants),
            "lock_events": commitment_lock_events_view(commitments),
            "degraded": degraded,
        },
    )
