"""Profitability screen (02b S8 screen 6). Owner: ui-b (BUILD.md S4).

Renders `/og/profitability`: per-service/customer/obligation/day revenue, energy cost, degradation,
penalty and net margin; an LP-vs-rule-baseline comparison; and the forgone-upside-from-lock line item
(the opportunity cost the commitment lock accepted by not switching mid-contract, see the dispatch
commitment-lock rule). Server-rendered first paint comes from `opengrid.ui.api_client.get_json` (02b
S7.1); the page polls itself every 30 s. View-model functions below are pure and unit-tested against
JSON fixtures, no HTTP or DB involved.
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
router = APIRouter(prefix="/profitability")

_PNL_FIELDS: tuple[str, ...] = (
    "revenue",
    "energy_cost",
    "degradation_cost",
    "penalty",
    "net_value",
    "forgone_upside",
)


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a `Decimal`-as-JSON-string (or number, or None) money/quantity field to `float` for
    display and arithmetic. The API (02b S7.1) serializes Pydantic `Decimal` fields as strings."""
    return float(value) if value is not None else default


def profitability_table_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-service/customer/obligation/day P&L table plus column totals (02b S8 screen 6)."""
    out: list[dict[str, Any]] = []
    totals = dict.fromkeys(_PNL_FIELDS, 0.0)
    for row in rows:
        record = {
            "obligation_id": row["obligation_id"],
            "service_type": row.get("service_type"),
            "customer_id": row.get("customer_id"),
            "interval_start": row["interval_start"],
            "interval_end": row["interval_end"],
            "revenue": _num(row.get("revenue")),
            "energy_cost": _num(row.get("energy_cost")),
            "degradation_cost": _num(row.get("degradation_cost")),
            "penalty": _num(row.get("penalty")),
            "net_value": _num(row.get("net_value")),
            "rule_baseline_value": (
                _num(row["rule_baseline_value"]) if row.get("rule_baseline_value") is not None else None
            ),
            "forgone_upside": _num(row.get("forgone_upside")),
        }
        for field in _PNL_FIELDS:
            totals[field] += record[field]
        out.append(record)
    return {"rows": out, "totals": totals, "row_count": len(out)}


def lp_vs_baseline_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Bar-chart comparison of the LP-selected net value against the rule-fallback baseline value, for
    every obligation where a baseline was computed (02b S8 screen 6)."""
    with_baseline = [row for row in rows if row.get("rule_baseline_value") is not None]
    labels = [row["obligation_id"] for row in with_baseline]
    return {
        "chart_option": {
            "xAxis": {"type": "category", "data": labels},
            "yAxis": {"type": "value", "name": "$"},
            "legend": {},
            "tooltip": {"trigger": "axis"},
            "series": [
                {
                    "name": "LP net value",
                    "type": "bar",
                    "data": [_num(row["net_value"]) for row in with_baseline],
                },
                {
                    "name": "Rule baseline",
                    "type": "bar",
                    "itemStyle": {"color": "token:--muted@0.55"},
                    "data": [_num(row["rule_baseline_value"]) for row in with_baseline],
                },
            ],
        },
        "compared_count": len(with_baseline),
    }


def forgone_upside_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Total forgone upside from the commitment lock: value the optimizer would have captured by
    switching capacity mid-contract, which the lock deliberately declines (dispatch-commitment-lock
    rule)."""
    total = sum(_num(row.get("forgone_upside")) for row in rows)
    at_risk_count = sum(1 for row in rows if _num(row.get("forgone_upside")) > 0)
    return {"total_forgone_upside": total, "obligation_count": at_risk_count}


@router.get("", response_class=HTMLResponse)
async def profitability_page(
    request: Request,
    service: str | None = Query(default=None),
    day: str | None = Query(default=None),
) -> HTMLResponse:
    """Profitability screen (`/og/profitability`, viewer role read-only, 30 s poll per 02b S8)."""
    now = datetime.now(tz=UTC)
    params = {k: v for k, v in {"service": service, "day": day}.items() if v}
    degraded: str | None = None
    rows: list[dict[str, Any]] = []

    try:
        summary = await get_json("/og/api/profitability/summary", params=params)
        rows = summary.get("rows", summary) if isinstance(summary, dict) else summary or []
    except ApiUnavailable as exc:
        logger.warning("profitability: /og/api/profitability/summary unavailable: %s", exc)
        degraded = str(exc)

    rows = rows if isinstance(rows, list) else []
    return templates.TemplateResponse(
        request,
        "profitability.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "service": service,
            "day": day,
            "table": profitability_table_view(rows),
            "lp_vs_baseline": lp_vs_baseline_view(rows),
            "forgone_upside": forgone_upside_view(rows),
            "generated_at": now.isoformat(),
            "degraded": degraded,
        },
    )
