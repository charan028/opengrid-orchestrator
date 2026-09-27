"""Read-only settlement views for the operator console (Profitability, Billing & audit). Owner: ui (MERGE).

`GET /og/api/views/settlement` returns, in one read, everything both screens need so they label the same
contract the same way (owner-reported defect, 2026-09-26: the screens showed raw UUIDs, Profitability
never showed the contract at all, and the two screens could not be matched):

- `contracts`: every `og.contract` with a short code (`d03`), a label (`ERCOT_AS · d03`) and its
  customer's label -- the `[api.roles.customer]` account name when one is mapped to that customer_id,
  otherwise the customer's short code (`c03`). One join, `contract_id -> og.contract`, for both screens.
- `obligations`: every obligation the P&L or invoice rows reference, labelled by its window and product
  (`Sep 26 12:00-13:00 ECRS 500 kW`, ERCOT local time).
- `pnl_rows` and `invoice_lines` for the period, each with its version and the id of the row that
  supersedes it (`superseded_by`), so a screen can strike a superseded row and total only active ones.
- `last_updated`: `max(created_at)` of `og.pnl`, `og.invoice_line` and `og.meter_interval`, so liveness
  is visible even when settlement is sparse.

It only reads, it computes no money (settle owns every figure, 02b S12), and it never mutates anything.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_config, get_pool
from opengrid.core.models.market import SAMPLE_INACTIVE_LABEL
from opengrid.market.availability import BANK_CAPACITY_SQL, capacity_summary
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api/views", tags=["views"])

#: ERCOT's market clock; obligation windows are labelled in it.
MARKET_TZ = ZoneInfo("America/Chicago")
DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 366
MAX_ROWS = 2000

_CONTRACTS_SQL = """
    SELECT contract_id, customer_id, service_type, variant, tier, status,
           -- D-37 (migration 0046); to_jsonb keeps a pre-0046 database working
           to_jsonb(c) ->> 'name' AS name, (to_jsonb(c) ->> 'is_sample')::boolean AS is_sample
    FROM og.contract c ORDER BY contract_id
"""

_PNL_SQL = """
    SELECT p.pnl_id, p.obligation_id, o.contract_id, p.interval_start, p.interval_end, p.revenue,
           p.energy_cost, p.degradation_cost, p.penalty, p.net_value, p.rule_baseline_value,
           p.forgone_upside, p.delivery_charge, p.version, p.superseded_by, p.created_at
    FROM og.pnl p JOIN og.obligation o ON o.obligation_id = p.obligation_id
    WHERE p.interval_start >= %(t0)s AND p.interval_start < %(t1)s
    ORDER BY p.interval_start DESC, p.version DESC
    LIMIT %(limit)s
"""

_INVOICE_SQL = """
    SELECT il.invoice_line_id, il.contract_id, il.obligation_id, il.period_start, il.period_end,
           il.line_type, il.quantity, il.unit, il.rate, il.amount, il.status, il.supersedes, il.version,
           il.created_at, (SELECT c.invoice_line_id FROM og.invoice_line c
                           WHERE c.supersedes = il.invoice_line_id
                           ORDER BY c.version DESC LIMIT 1) AS superseded_by
    FROM og.invoice_line il
    WHERE il.period_start >= %(d0)s AND il.period_start < %(d1)s
    ORDER BY il.period_start DESC, il.created_at DESC
    LIMIT %(limit)s
"""

_OBLIGATIONS_SQL = """
    SELECT o.obligation_id, o.contract_id, o.service_type, o.window_start, o.window_end,
           o.committed_qty_kw, o.state
    FROM og.obligation o WHERE o.obligation_id = ANY(%(ids)s::uuid[])
"""

_POSTURE_SQL = """
    SELECT scope_kind, scope_ref, veto_ratio, consecutive, stop_requested, since, updated_at
    FROM og.scope_posture WHERE posture = 'CONSERVATIVE' ORDER BY scope_kind, scope_ref
"""

_LAST_UPDATED_SQL = """
    SELECT (SELECT max(created_at) FROM og.pnl) AS pnl,
           (SELECT max(created_at) FROM og.invoice_line) AS invoice_line,
           (SELECT max(created_at) FROM og.meter_interval) AS meter_interval
"""


# -- labels (pure) ----------------------------------------------------------------------------------------


def short_code(value: UUID | str) -> str:
    """The last 3 hex digits of an id, e.g. `...-000000000d03` -> `d03`: readable, and unique among the
    seeded demo ids; the full UUID stays available on hover."""
    return str(value).replace("-", "")[-3:]


def contract_label(service_type: str, contract_id: UUID | str) -> str:
    return f"{service_type} · {short_code(contract_id)}"


def customer_names(cfg: Config) -> dict[str, str]:
    """`customer_id -> account name` from `[api.roles.customer]` (`user = "<customer_id>"`)."""
    table = cfg.get("api.roles.customer", {}) or {}
    if not isinstance(table, dict):
        return {}
    return {str(customer_id): str(user) for user, customer_id in sorted(table.items())}


def obligation_label(
    window_start: datetime, window_end: datetime, product: str | None, committed_kw: Decimal | float | None
) -> str:
    """`Sep 26 12:00-13:00 ECRS 500 kW` in ERCOT local time (the end carries its date when it differs)."""
    start = window_start.astimezone(MARKET_TZ)
    end = window_end.astimezone(MARKET_TZ)
    end_text = end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%b %d %H:%M")
    parts = [f"{start.strftime('%b %d %H:%M')}\u2013{end_text}"]
    if product:
        parts.append(product)
    if committed_kw is not None:
        parts.append(f"{Decimal(str(committed_kw)).normalize():f} kW")
    return " ".join(parts)


def sample_fields(contract: dict[str, Any]) -> dict[str, Any]:
    """D-37: `name`, `status`, `is_sample` and `sample_label` of a contract row. A sample contract is never
    ACTIVE (0046 CHECK), so it has no obligation, P&L or invoice line: it is outside every revenue total by
    construction, and listed only with its SAMPLE - INACTIVE label."""
    is_sample = bool(contract.get("is_sample"))
    return {
        "name": contract.get("name"),
        "status": contract.get("status"),
        "is_sample": is_sample,
        "sample_label": SAMPLE_INACTIVE_LABEL if is_sample else None,
    }


def _period(from_: date | None, to: date | None) -> tuple[date, date]:
    """`[from, to)` in days; default the last 30 days through tomorrow. 422 on an inverted or huge range."""
    today = datetime.now(UTC).date()
    d1 = (to or today) + timedelta(days=1)
    d0 = from_ or (d1 - timedelta(days=DEFAULT_PERIOD_DAYS + 1))
    if d0 >= d1 or (d1 - d0).days > MAX_PERIOD_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid from/to period")
    return d0, d1


def _json(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, UUID | Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime | date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def build_view(
    *,
    contracts: list[dict[str, Any]],
    obligations: list[dict[str, Any]],
    pnl_rows: list[dict[str, Any]],
    invoice_lines: list[dict[str, Any]],
    last_updated: dict[str, Any],
    names: dict[str, str],
    period: tuple[date, date],
) -> dict[str, Any]:
    """Assemble the response from raw rows (pure: unit-tested without a database)."""
    contract_rows = []
    variant_by_contract: dict[str, str | None] = {}
    for c in contracts:
        cid, customer = str(c["contract_id"]), str(c["customer_id"])
        variant_by_contract[cid] = c.get("variant")
        contract_rows.append(
            {
                "contract_id": cid,
                "short": short_code(cid),
                "label": contract_label(str(c["service_type"]), cid),
                "service_type": c["service_type"],
                "variant": c.get("variant"),
                "tier": c.get("tier"),
                "customer_id": customer,
                "customer_label": names.get(customer, short_code(customer)),
                **sample_fields(c),
            }
        )
        if c.get("is_sample"):
            # D-37: a sample contract is listed with its SAMPLE - INACTIVE label wherever contracts show.
            contract_rows[-1]["label"] = (
                f"{SAMPLE_INACTIVE_LABEL} \u00b7 {c.get('name') or contract_rows[-1]['label']}"
            )
    obligation_map = {
        str(o["obligation_id"]): {
            "obligation_id": str(o["obligation_id"]),
            "contract_id": str(o["contract_id"]),
            "state": o.get("state"),
            "label": obligation_label(
                o["window_start"],
                o["window_end"],
                variant_by_contract.get(str(o["contract_id"])) or o.get("service_type"),
                o.get("committed_qty_kw"),
            ),
        }
        for o in obligations
    }
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "period": {"from": period[0].isoformat(), "to_exclusive": period[1].isoformat()},
        "last_updated": {
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in last_updated.items()
        },
        "contracts": contract_rows,
        "obligations": obligation_map,
        "pnl_rows": [_json(r) for r in pnl_rows],
        "invoice_lines": [_json(r) for r in invoice_lines],
    }


# -- endpoint ---------------------------------------------------------------------------------------------


async def _fetch(
    pool: AsyncConnectionPool, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params or {})
        return list(await cur.fetchall())


@router.get("/settlement")
async def settlement_view(
    _identity: Annotated[Identity, Depends(require_viewer)],
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    cfg: Annotated[Config, Depends(get_config)],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    d0, d1 = _period(from_, to)
    t0 = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    t1 = datetime(d1.year, d1.month, d1.day, tzinfo=UTC)
    contracts = await _fetch(pool, _CONTRACTS_SQL)
    pnl_rows = await _fetch(pool, _PNL_SQL, {"t0": t0, "t1": t1, "limit": MAX_ROWS})
    invoice_lines = await _fetch(pool, _INVOICE_SQL, {"d0": d0, "d1": d1, "limit": MAX_ROWS})
    ids = sorted({str(r["obligation_id"]) for r in [*pnl_rows, *invoice_lines] if r.get("obligation_id")})
    obligations = await _fetch(pool, _OBLIGATIONS_SQL, {"ids": ids}) if ids else []
    (last_updated,) = await _fetch(pool, _LAST_UPDATED_SQL)
    return build_view(
        contracts=contracts,
        obligations=obligations,
        pnl_rows=pnl_rows,
        invoice_lines=invoice_lines,
        last_updated=last_updated,
        names=customer_names(cfg),
        period=(d0, d1),
    )


@router.get("/availability")
async def availability_view(
    _identity: Annotated[Identity, Depends(require_viewer)],
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
) -> dict[str, Any]:
    """D-37: bank availability per zone and the fleet capacity split -- available kW vs "Regulated market -
    no contract" kW -- for the Profitability capacity line and the Control room zone summary. Rated kW is
    the bank's kVA rating (the bank's discharge ceiling). A database before 0046 reads all AVAILABLE."""
    rows = await _fetch(pool, BANK_CAPACITY_SQL)
    return {"as_of": datetime.now(UTC).isoformat(), **capacity_summary(rows)}


@router.get("/scope-posture")
async def scope_posture(
    _identity: Annotated[Identity, Depends(require_viewer)],
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
) -> dict[str, Any]:
    """The guardian's CURRENT safety posture: every scope held CONSERVATIVE right now (`og.scope_posture`,
    written only by og-guardian, K7). The console's posture strip reads this, not open alerts. A database
    without the table (before migration 0019) answers an empty list."""
    try:
        rows = await _fetch(pool, _POSTURE_SQL)
    except UndefinedTable:
        rows = []
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "count": len(rows),
        "conservative": [_json(r) for r in rows],
    }
