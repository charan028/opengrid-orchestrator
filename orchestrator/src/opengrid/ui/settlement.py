"""Shared view model for the Profitability and Billing & audit screens. Owner: ui (MERGE).

Both screens read ONE payload, `GET /og/api/views/settlement` (`opengrid.api.views_ext`), so a contract
carries the same label on both (`ERCOT_AS · d03`, from the single `contract_id -> og.contract` join), an
obligation reads as its window and product instead of a UUID, and a row superseded by a settlement
correction is shown struck through and left out of every total. Full UUIDs stay available on hover.

Pure functions only (no HTTP): the routes fetch, these shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from opengrid.ui.templating import BASE_PATH

SETTLEMENT_VIEW_PATH = "/og/api/views/settlement"
MARKET_TZ = ZoneInfo("America/Chicago")

PNL_FIELDS: tuple[str, ...] = (
    "revenue",
    "energy_cost",
    "degradation_cost",
    "penalty",
    "net_value",
    "forgone_upside",
)


def num(value: Any) -> float:
    """Money/quantity fields arrive as decimal strings; None reads as 0."""
    return float(value) if value not in (None, "") else 0.0


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(MARKET_TZ)


def interval_label(start: str, end: str) -> str:
    """`Sep 26 12:15-12:30` (ERCOT local time)."""
    s, e = _local(start), _local(end)
    return f"{s.strftime('%b %d %H:%M')}\u2013{e.strftime('%H:%M')}"


def local_day(ts: str) -> str:
    return _local(ts).date().isoformat()


def _index(view: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    contracts = {c["contract_id"]: c for c in view.get("contracts") or []}
    obligations = view.get("obligations") or {}
    return contracts, obligations


def filter_options(view: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Customer and contract choices for the filter selects (labels, ids as values)."""
    contracts = view.get("contracts") or []
    customers = {c["customer_id"]: c["customer_label"] for c in contracts}
    return {
        "customers": [{"id": k, "label": v} for k, v in sorted(customers.items(), key=lambda kv: kv[1])],
        "contracts": [{"id": c["contract_id"], "label": c["label"]} for c in contracts],
    }


def _matches(contract: dict[str, Any] | None, *, customer: str | None, contract_id: str | None) -> bool:
    if contract_id and (contract is None or contract["contract_id"] != contract_id):
        return False
    return not (customer and (contract is None or contract["customer_id"] != customer))


def _labels(
    contract: dict[str, Any] | None, contract_id: str, obligation: dict[str, Any] | None, obligation_id: str
) -> dict[str, Any]:
    return {
        "contract_id": contract_id,
        "contract_label": contract["label"] if contract else f"contract {contract_id[-3:]}",
        "customer_label": contract["customer_label"] if contract else "-",
        "customer_id": contract["customer_id"] if contract else "",
        "service_type": contract["service_type"] if contract else "-",
        "obligation_id": obligation_id,
        "obligation_label": obligation["label"] if obligation else f"obligation {obligation_id[:8]}",
    }


def _group_totals(rows: list[dict[str, Any]], key: str, label_key: str, field: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["superseded"]:
            continue
        g = groups.setdefault(row[key], {"key": row[key], "label": row[label_key], "total": 0.0, "count": 0})
        g["total"] += row[field]
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: g["label"])


def pnl_view(
    view: dict[str, Any],
    *,
    customer: str | None = None,
    contract: str | None = None,
    service: str | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Profitability rows (active and superseded), totals over active rows only, per-contract and per-day
    net totals, and the LP-vs-baseline chart keyed by obligation label."""
    contracts, obligations = _index(view)
    rows: list[dict[str, Any]] = []
    for r in view.get("pnl_rows") or []:
        c = contracts.get(r["contract_id"])
        if not _matches(c, customer=customer, contract_id=contract):
            continue
        if service and (c is None or c["service_type"] != service):
            continue
        if day and local_day(r["interval_start"]) != day:
            continue
        row = {
            **_labels(c, r["contract_id"], obligations.get(r["obligation_id"]), r["obligation_id"]),
            "pnl_id": r["pnl_id"],
            "interval_label": interval_label(r["interval_start"], r["interval_end"]),
            "day": local_day(r["interval_start"]),
            "version": r.get("version", 1),
            "superseded": bool(r.get("superseded_by")),
            "rule_baseline_value": num(r["rule_baseline_value"])
            if r.get("rule_baseline_value") is not None
            else None,
            "invoice_link": f"{BASE_PATH}/billing?obligation={r['obligation_id']}#invoice-lines",
            **{f: num(r.get(f)) for f in PNL_FIELDS},
        }
        rows.append(row)
    active = [r for r in rows if not r["superseded"]]
    totals = {f: sum(r[f] for r in active) for f in PNL_FIELDS}
    compared = [r for r in active if r["rule_baseline_value"] is not None]
    return {
        "rows": rows,
        "row_count": len(active),
        "superseded_count": len(rows) - len(active),
        "totals": totals,
        "by_contract": _group_totals(rows, "contract_id", "contract_label", "net_value"),
        "by_day": _group_totals(rows, "day", "day", "net_value"),
        "lp_chart": {
            "xAxis": {
                "type": "category",
                "data": [f"{r['obligation_label']} · {r['interval_label'][-11:]}" for r in compared],
            },
            "yAxis": {"type": "value", "name": "$"},
            "legend": {},
            "tooltip": {"trigger": "axis"},
            "series": [
                {"name": "LP net value", "type": "bar", "data": [r["net_value"] for r in compared]},
                {
                    "name": "Rule baseline",
                    "type": "bar",
                    "itemStyle": {"color": "token:--muted@0.55"},
                    "data": [r["rule_baseline_value"] for r in compared],
                },
            ],
        },
        "compared_count": len(compared),
    }


def invoice_view(
    view: dict[str, Any],
    *,
    customer: str | None = None,
    contract: str | None = None,
    obligation: str | None = None,
) -> dict[str, Any]:
    """Invoice lines with labels; a superseded line is flagged (struck through) and excluded from totals,
    and its correction is marked. Totals by line type and by contract."""
    contracts, obligations = _index(view)
    rows: list[dict[str, Any]] = []
    for line in view.get("invoice_lines") or []:
        c = contracts.get(line["contract_id"])
        if not _matches(c, customer=customer, contract_id=contract):
            continue
        if obligation and line["obligation_id"] != obligation:
            continue
        rows.append(
            {
                **_labels(
                    c, line["contract_id"], obligations.get(line["obligation_id"]), line["obligation_id"]
                ),
                "invoice_line_id": line["invoice_line_id"],
                "line_type": line["line_type"],
                "quantity": line.get("quantity"),
                "unit": line.get("unit") or "",
                "rate": line.get("rate"),
                "amount": num(line["amount"]),
                "status": line.get("status", "PROVISIONAL"),
                "version": line.get("version", 1),
                "superseded": bool(line.get("superseded_by")),
                "is_correction": bool(line.get("supersedes")),
                "created_at": line.get("created_at"),
            }
        )
    active = [r for r in rows if not r["superseded"]]
    by_type: dict[str, float] = {}
    for r in active:
        by_type[r["line_type"]] = by_type.get(r["line_type"], 0.0) + r["amount"]
    return {
        "rows": rows,
        "row_count": len(active),
        "superseded_count": len(rows) - len(active),
        "total_amount": sum(r["amount"] for r in active),
        "totals_by_type": dict(sorted(by_type.items())),
        "by_contract": _group_totals(rows, "contract_id", "contract_label", "amount"),
    }


def last_updated(view: dict[str, Any], key: str) -> str | None:
    value = (view.get("last_updated") or {}).get(key)
    return str(value) if value else None
