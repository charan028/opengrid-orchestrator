"""In-memory fakes for the customer API tests: `FakeCustomerStore` (the `CustomerStore` protocol, with the
same customer scoping the SQL applies) and `FakeBillingStore` (the two `StoreProtocol` methods the
customer API uses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from opengrid.core.models.engine import InvoiceLine, Obligation, RenominationPoint
from opengrid.customer_api.models import CustomerObligationRequest, InvoiceDispute
from opengrid.customer_api.store import OpenItemExistsError

from ..contracts.fakes import FakeContractsRepo

_LIVE_DISPUTE = frozenset({"OPEN", "UNDER_REVIEW"})
_LIVE_REQUEST = frozenset({"PENDING_OPERATOR_REVIEW", "QUEUED_FOR_RENOMINATION"})


@dataclass
class FakeCustomerStore:
    """Reads obligations from the shared contracts fake, so a state change made through
    `opengrid.contracts` is visible here exactly as in Postgres."""

    contracts_repo: FakeContractsRepo
    invoice_lines: dict[UUID, InvoiceLine] = field(default_factory=dict)
    points: list[RenominationPoint] = field(default_factory=list)
    disputes: dict[UUID, InvoiceDispute] = field(default_factory=dict)
    requests: dict[UUID, CustomerObligationRequest] = field(default_factory=dict)

    def _customer_of(self, contract_id: UUID) -> UUID | None:
        contract = self.contracts_repo.contracts.get(contract_id)
        return contract.customer_id if contract else None

    async def obligations_for_customer(self, customer_id: UUID, *, state: str | None) -> list[Obligation]:
        return [
            o
            for o in self.contracts_repo.obligations.values()
            if self._customer_of(o.contract_id) == customer_id and (state is None or o.state == state)
        ]

    async def obligation_for_customer(self, customer_id: UUID, obligation_id: UUID) -> Obligation | None:
        obligation = self.contracts_repo.obligations.get(obligation_id)
        if obligation is None or self._customer_of(obligation.contract_id) != customer_id:
            return None
        return obligation

    async def invoice_line_for_customer(self, customer_id: UUID, invoice_line_id: UUID) -> InvoiceLine | None:
        line = self.invoice_lines.get(invoice_line_id)
        if line is None or self._customer_of(line.contract_id) != customer_id:
            return None
        return line

    async def upcoming_renomination_point(
        self, contract_id: UUID, obligation_id: UUID, *, after: datetime, before: datetime
    ) -> RenominationPoint | None:
        candidates = [
            p
            for p in self.points
            if p.contract_id == contract_id
            and p.obligation_id in (obligation_id, None)
            and p.exercised_at is None
            and after < p.scheduled_at < before
        ]
        return min(candidates, key=lambda p: p.scheduled_at) if candidates else None

    async def insert_dispute(self, dispute: InvoiceDispute) -> None:
        if any(
            d.invoice_line_id == dispute.invoice_line_id and d.status in _LIVE_DISPUTE
            for d in self.disputes.values()
        ):
            raise OpenItemExistsError("live dispute exists")
        self.disputes[dispute.dispute_id] = dispute

    async def list_disputes(self, *, customer_id: UUID | None) -> list[InvoiceDispute]:
        return [d for d in self.disputes.values() if customer_id is None or d.customer_id == customer_id]

    async def review_dispute(
        self, dispute_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> InvoiceDispute | None:
        current = self.disputes.get(dispute_id)
        if current is None or current.status not in _LIVE_DISPUTE:
            return None
        updated = current.model_copy(
            update={"status": status, "reviewed_by": reviewed_by, "review_note": note}
        )
        self.disputes[dispute_id] = updated
        return updated

    async def insert_request(self, request: CustomerObligationRequest) -> None:
        if request.status in _LIVE_REQUEST and any(
            r.obligation_id == request.obligation_id and r.kind == request.kind and r.status in _LIVE_REQUEST
            for r in self.requests.values()
        ):
            raise OpenItemExistsError("live request exists")
        self.requests[request.request_id] = request

    async def list_requests(self, *, customer_id: UUID | None) -> list[CustomerObligationRequest]:
        return [r for r in self.requests.values() if customer_id is None or r.customer_id == customer_id]

    async def review_request(
        self, request_id: UUID, *, status: str, reviewed_by: str, note: str | None
    ) -> CustomerObligationRequest | None:
        current = self.requests.get(request_id)
        if current is None or current.status not in _LIVE_REQUEST:
            return None
        updated = current.model_copy(
            update={"status": status, "reviewed_by": reviewed_by, "review_note": note}
        )
        self.requests[request_id] = updated
        return updated


@dataclass
class FakeBillingStore:
    """`StoreProtocol.invoice_lines` returns every line, unscoped, like the real billing query."""

    lines: list[InvoiceLine] = field(default_factory=list)
    operator_actions: list[dict[str, Any]] = field(default_factory=list)

    async def invoice_lines(self, *, t0: date, t1: date) -> list[InvoiceLine]:
        return [line for line in self.lines if line.period_start >= t0 and line.period_end <= t1]

    async def insert_operator_action(self, **kwargs: Any) -> UUID:
        self.operator_actions.append(kwargs)
        return uuid4()
