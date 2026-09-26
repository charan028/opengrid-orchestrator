"""Storage for the customer API: customer-scoped reads of obligations/invoice lines/re-nomination points,
and the two tables this package owns (`og.invoice_dispute`, `og.customer_obligation_request`,
`migrations/0026_customer_services.sql`).

Every customer read filters on the caller's `customer_id` in SQL (joined through `og.contract`), so a
row of another customer is never even loaded. Contract/obligation writes stay with `opengrid.contracts`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import Request
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.engine import InvoiceLine, Obligation, RenominationPoint
from opengrid.customer_api.models import CustomerObligationRequest, InvoiceDispute

#: Page cap on list reads (same order as `opengrid.api.store`'s LIMIT 500).
LIST_LIMIT = 500


class OpenItemExistsError(Exception):
    """A live dispute already exists for the invoice line, or a live request of the same kind for the
    obligation (the partial unique indexes of migration 0026)."""


class CustomerStore(Protocol):
    async def obligations_for_customer(self, customer_id: UUID, *, state: str | None) -> list[Obligation]: ...

    async def obligation_for_customer(self, customer_id: UUID, obligation_id: UUID) -> Obligation | None: ...

    async def invoice_line_for_customer(
        self, customer_id: UUID, invoice_line_id: UUID
    ) -> InvoiceLine | None: ...

    async def upcoming_renomination_point(
        self, contract_id: UUID, obligation_id: UUID, *, after: datetime, before: datetime
    ) -> RenominationPoint | None:
        """The earliest unexercised point for this obligation (or contract-wide) in `(after, before)`."""
        ...

    async def insert_dispute(self, dispute: InvoiceDispute) -> None:
        """Raises `OpenItemExistsError` when the line already has a live dispute."""
        ...

    async def list_disputes(self, *, customer_id: UUID | None) -> list[InvoiceDispute]: ...

    async def review_dispute(
        self, dispute_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> InvoiceDispute | None: ...

    async def insert_request(self, request: CustomerObligationRequest) -> None:
        """Raises `OpenItemExistsError` when a live request of the same kind exists."""
        ...

    async def list_requests(self, *, customer_id: UUID | None) -> list[CustomerObligationRequest]: ...

    async def review_request(
        self, request_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> CustomerObligationRequest | None: ...


def get_customer_store(request: Request) -> CustomerStore:
    """FastAPI dependency: a store over og-api's own pool (`opengrid.api.app` lifespan). Tests override
    it with an in-memory fake."""
    pool: AsyncConnectionPool = request.app.state.pool
    return PgCustomerStore(pool)


_OBLIGATION_COLUMNS = """
    o.obligation_id, o.opportunity_id, o.contract_id, o.service_type, o.tier, o.window_start, o.window_end,
    o.committed_qty_kw, o.state, o.at_risk, o.last_reason_code, o.version,
    es.margin_kwh AS energy_margin_kwh, es.time_to_depletion_h
"""

_OBLIGATIONS_SQL = f"""
    SELECT {_OBLIGATION_COLUMNS}
    FROM og.obligation o
    JOIN og.contract c ON c.contract_id = o.contract_id
    LEFT JOIN og.obligation_energy_status es ON es.obligation_id = o.obligation_id
    WHERE c.customer_id = %(customer_id)s
      AND (%(state)s::text IS NULL OR o.state = %(state)s::text)
      AND (%(obligation_id)s::uuid IS NULL OR o.obligation_id = %(obligation_id)s::uuid)
    ORDER BY o.window_start DESC LIMIT {LIST_LIMIT}
"""  # noqa: S608 -- only fixed fragments are interpolated; values are bound parameters

_INVOICE_LINE_SQL = """
    SELECT l.invoice_line_id, l.contract_id, l.obligation_id, l.period_start, l.period_end, l.line_type,
           l.quantity, l.unit, l.rate, l.amount, l.status, l.supersedes, l.trace_roll_up, l.version
    FROM og.invoice_line l JOIN og.contract c ON c.contract_id = l.contract_id
    WHERE c.customer_id = %s AND l.invoice_line_id = %s
"""

_UPCOMING_POINT_SQL = """
    SELECT renomination_point_id, contract_id, obligation_id, scheduled_at, exercised_at, outcome, plan_id
    FROM og.renomination_point
    WHERE contract_id = %s AND (obligation_id = %s OR obligation_id IS NULL)
      AND exercised_at IS NULL AND scheduled_at > %s AND scheduled_at < %s
    ORDER BY scheduled_at ASC LIMIT 1
"""

_DISPUTE_COLUMNS = (
    "dispute_id, invoice_line_id, contract_id, customer_id, reason, reason_code, status, raised_by, reviewed_by, "
    "review_note, trace_id, created_at, updated_at"
)

_INSERT_DISPUTE_SQL = """
    INSERT INTO og.invoice_dispute (
        dispute_id, invoice_line_id, contract_id, customer_id, reason, reason_code, status, raised_by, trace_id
    ) VALUES (%(dispute_id)s, %(invoice_line_id)s, %(contract_id)s, %(customer_id)s, %(reason)s, %(reason_code)s,
              %(status)s, %(raised_by)s, %(trace_id)s)
"""

_REQUEST_COLUMNS = (
    "request_id, obligation_id, contract_id, customer_id, kind, obligation_state, requested_kw, "
    "requested_window_start, requested_window_end, "
    "renomination_point_id, penalty_terms, rule_code, status, raised_by, reviewed_by, review_note, "
    "trace_id, created_at, updated_at"
)

_INSERT_REQUEST_SQL = """
    INSERT INTO og.customer_obligation_request (
        request_id, obligation_id, contract_id, customer_id, kind, obligation_state, requested_kw,
        requested_window_start, requested_window_end, renomination_point_id, penalty_terms, rule_code, status, raised_by, trace_id
    ) VALUES (%(request_id)s, %(obligation_id)s, %(contract_id)s, %(customer_id)s, %(kind)s,
              %(obligation_state)s, %(requested_kw)s, %(requested_window_start)s, %(requested_window_end)s, %(renomination_point_id)s, %(penalty_terms)s,
              %(rule_code)s, %(status)s, %(raised_by)s, %(trace_id)s)
"""


# Only fixed column lists and the LIST_LIMIT constant are interpolated; every value is a bound parameter.
_LIST_DISPUTES_SQL = f"""
    SELECT {_DISPUTE_COLUMNS} FROM og.invoice_dispute
    WHERE (%(c)s::uuid IS NULL OR customer_id = %(c)s::uuid) ORDER BY created_at DESC LIMIT {LIST_LIMIT}
"""  # noqa: S608
_REVIEW_DISPUTE_SQL = f"""
    UPDATE og.invoice_dispute SET status = %s, reviewed_by = %s, review_note = %s, updated_at = now()
    WHERE dispute_id = %s AND status IN ('OPEN', 'UNDER_REVIEW') RETURNING {_DISPUTE_COLUMNS}
"""  # noqa: S608
_LIST_REQUESTS_SQL = f"""
    SELECT {_REQUEST_COLUMNS} FROM og.customer_obligation_request
    WHERE (%(c)s::uuid IS NULL OR customer_id = %(c)s::uuid) ORDER BY created_at DESC LIMIT {LIST_LIMIT}
"""  # noqa: S608
_REVIEW_REQUEST_SQL = f"""
    UPDATE og.customer_obligation_request SET status = %s, reviewed_by = %s, review_note = %s, updated_at = now()
    WHERE request_id = %s AND status IN ('PENDING_OPERATOR_REVIEW', 'QUEUED_FOR_RENOMINATION')
    RETURNING {_REQUEST_COLUMNS}
"""  # noqa: S608


class PgCustomerStore:
    """`CustomerStore` over psycopg 3. A connection-context exit commits (psycopg's default)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _fetch(self, sql: str, params: Any) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())

    async def _insert(self, sql: str, params: dict[str, Any]) -> None:
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
        except pg_errors.UniqueViolation as exc:
            raise OpenItemExistsError(str(exc)) from exc

    async def obligations_for_customer(self, customer_id: UUID, *, state: str | None) -> list[Obligation]:
        rows = await self._fetch(
            _OBLIGATIONS_SQL, {"customer_id": customer_id, "state": state, "obligation_id": None}
        )
        return [Obligation(**row) for row in rows]

    async def obligation_for_customer(self, customer_id: UUID, obligation_id: UUID) -> Obligation | None:
        rows = await self._fetch(
            _OBLIGATIONS_SQL, {"customer_id": customer_id, "state": None, "obligation_id": obligation_id}
        )
        return Obligation(**rows[0]) if rows else None

    async def invoice_line_for_customer(self, customer_id: UUID, invoice_line_id: UUID) -> InvoiceLine | None:
        rows = await self._fetch(_INVOICE_LINE_SQL, (customer_id, invoice_line_id))
        return InvoiceLine(**rows[0]) if rows else None

    async def upcoming_renomination_point(
        self, contract_id: UUID, obligation_id: UUID, *, after: datetime, before: datetime
    ) -> RenominationPoint | None:
        rows = await self._fetch(_UPCOMING_POINT_SQL, (contract_id, obligation_id, after, before))
        return RenominationPoint(**rows[0]) if rows else None

    async def insert_dispute(self, dispute: InvoiceDispute) -> None:
        await self._insert(_INSERT_DISPUTE_SQL, dispute.model_dump())

    async def list_disputes(self, *, customer_id: UUID | None) -> list[InvoiceDispute]:
        rows = await self._fetch(_LIST_DISPUTES_SQL, {"c": customer_id})
        return [InvoiceDispute(**row) for row in rows]

    async def review_dispute(
        self, dispute_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> InvoiceDispute | None:
        rows = await self._fetch(_REVIEW_DISPUTE_SQL, (status, reviewed_by, note, dispute_id))
        return InvoiceDispute(**rows[0]) if rows else None

    async def insert_request(self, request: CustomerObligationRequest) -> None:
        params = request.model_dump()
        params["penalty_terms"] = Jsonb(request.penalty_terms) if request.penalty_terms is not None else None
        await self._insert(_INSERT_REQUEST_SQL, params)

    async def list_requests(self, *, customer_id: UUID | None) -> list[CustomerObligationRequest]:
        rows = await self._fetch(_LIST_REQUESTS_SQL, {"c": customer_id})
        return [CustomerObligationRequest(**row) for row in rows]

    async def review_request(
        self, request_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> CustomerObligationRequest | None:
        rows = await self._fetch(_REVIEW_REQUEST_SQL, (status, reviewed_by, note, request_id))
        return CustomerObligationRequest(**rows[0]) if rows else None
