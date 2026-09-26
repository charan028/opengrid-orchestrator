"""Request/response and row shapes of the customer API (`opengrid.customer_api`), mirroring
`migrations/0026_customer_services.sql` (`og.invoice_dispute`, `og.customer_obligation_request`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

DisputeStatus = Literal["OPEN", "UNDER_REVIEW", "RESOLVED", "REJECTED"]
RequestKind = Literal["CANCEL", "RENOMINATE"]
RequestStatus = Literal[
    "PENDING_OPERATOR_REVIEW", "QUEUED_FOR_RENOMINATION", "ACCEPTED", "REJECTED", "APPLIED"
]

#: Free-text bounds: long enough for a real explanation, short enough to keep rows and traces small.
MAX_REASON_CHARS = 2000
MAX_CODE_CHARS = 64


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpportunitySubmission(_Body):
    """A customer's call for delivery under one of its own contracts (02a S1.4); admitted by
    `opengrid.contracts.admit_priced`, never by logic here."""

    contract_id: UUID
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal = Field(gt=0)
    #: Optional, as the customer simulator sends them: `customer_id` must be the caller's own (else 404);
    #: `service_profile` is informational -- the contract, not the request, fixes the service.
    customer_id: UUID | None = None
    service_profile: str | None = Field(default=None, max_length=MAX_CODE_CHARS)


class DisputeSubmission(_Body):
    """A machine `reason_code`, a free-text `reason`, or both."""

    reason: str | None = Field(default=None, min_length=1, max_length=MAX_REASON_CHARS)
    reason_code: str | None = Field(default=None, min_length=1, max_length=MAX_CODE_CHARS)

    @model_validator(mode="after")
    def _has_a_reason(self) -> DisputeSubmission:
        if self.reason is None and self.reason_code is None:
            raise ValueError("a dispute needs a reason or a reason_code")
        return self


class CancelSubmission(_Body):
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class RenominateSubmission(_Body):
    """A new quantity, a new window, or both, for the next re-nomination point to decide."""

    requested_kw: Decimal | None = Field(default=None, gt=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @model_validator(mode="after")
    def _asks_for_something(self) -> RenominateSubmission:
        window = (self.window_start, self.window_end)
        if (window[0] is None) != (window[1] is None):
            raise ValueError("window_start and window_end go together")
        if window[0] is not None and window[1] is not None and window[1] <= window[0]:
            raise ValueError("window_end must be after window_start")
        if self.requested_kw is None and window[0] is None:
            raise ValueError("a renomination needs requested_kw or a new window")
        return self


class DisputeReview(_Body):
    status: Literal["UNDER_REVIEW", "RESOLVED", "REJECTED"]
    note: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class RequestReview(_Body):
    status: Literal["ACCEPTED", "REJECTED"]
    note: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class InvoiceDispute(_Body):
    """One `og.invoice_dispute` row."""

    dispute_id: UUID
    invoice_line_id: UUID
    contract_id: UUID
    customer_id: UUID
    reason: str | None = None
    reason_code: str | None = None
    status: DisputeStatus = "OPEN"
    raised_by: str
    reviewed_by: str | None = None
    review_note: str | None = None
    trace_id: UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CustomerObligationRequest(_Body):
    """One `og.customer_obligation_request` row."""

    request_id: UUID
    obligation_id: UUID
    contract_id: UUID
    customer_id: UUID
    kind: RequestKind
    obligation_state: str
    requested_kw: Decimal | None = None
    requested_window_start: datetime | None = None
    requested_window_end: datetime | None = None
    renomination_point_id: UUID | None = None
    penalty_terms: dict[str, str | None] | None = None
    rule_code: str
    status: RequestStatus
    raised_by: str
    reviewed_by: str | None = None
    review_note: str | None = None
    trace_id: UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None
