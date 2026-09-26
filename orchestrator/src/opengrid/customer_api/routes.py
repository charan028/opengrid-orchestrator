"""Customer-facing endpoints under `/og/api/customer/` (06-service-profiles-and-power-quality.md S4:
the service is tailored per customer, so the customer submits, follows and disputes its own service).

Every endpoint depends on `require_customer` and scopes to the caller's own `customer_id`: an id that
belongs to another customer answers 404 exactly like one that does not exist, so a customer cannot even
learn that another customer's object exists. Admission is `opengrid.contracts.admit_priced`, invoice lines
come from `opengrid.api.store`'s billing query, and obligation state changes go through
`opengrid.contracts.transition_obligation` -- none of that logic is repeated here. The cancel/renominate
rules under the commitment lock (K13) are in `rules.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

import opengrid.contracts as contracts
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.store import StoreProtocol
from opengrid.contracts.errors import AdmissionError, ConcurrentUpdateError, IllegalTransitionError
from opengrid.core.models.engine import Contract, Obligation, ObligationState
from opengrid.customer_api.identity import CustomerIdentity, require_customer
from opengrid.customer_api.models import (
    CancelSubmission,
    CustomerObligationRequest,
    DisputeSubmission,
    InvoiceDispute,
    OpportunitySubmission,
    RenominateSubmission,
)
from opengrid.customer_api.rules import (
    CANCEL_TRANSITION_REASON,
    Disposition,
    RuleDecision,
    cancel_decision,
    penalty_terms,
    renominate_decision,
)
from opengrid.customer_api.store import CustomerStore, OpenItemExistsError, get_customer_store
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/customer", tags=["customer"])

Who = Annotated[CustomerIdentity, Depends(require_customer)]
Store = Annotated[CustomerStore, Depends(get_customer_store)]
Trace = Annotated[TraceStore, Depends(get_trace_store)]

R_CUSTOMER_DISPUTE = "R-CUSTOMER-DISPUTE"
R_CUSTOMER_CANCEL_STATE_CHANGED = "R-CUSTOMER-CANCEL-STATE-CHANGED"
R_CUSTOMER_REQUEST_OPEN = "R-CUSTOMER-REQUEST-OPEN"
R_CUSTOMER_DISPUTE_OPEN = "R-CUSTOMER-DISPUTE-OPEN"
CUSTOMER_REQUEST_EVENT_CLASS = "CUSTOMER_REQUEST"
CUSTOMER_DISPUTE_EVENT_CLASS = "CUSTOMER_DISPUTE"
_NOT_FOUND = "not found"
#: Default invoice window when the caller gives none: a quarter back, and far enough ahead to include
#: this and next month's lines (a line's `period_end` is its month end).
DEFAULT_INVOICE_LOOKBACK_DAYS = 92
DEFAULT_INVOICE_LOOKAHEAD_DAYS = 62


def _conflict(reason_code: str, **detail: Any) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"reason_code": reason_code, **detail})


async def _trace(
    trace: TraceStore,
    who: CustomerIdentity,
    decision_type: str,
    event_class: str,
    payload: dict[str, Any],
    reason: str,
) -> UUID:
    """Trace a customer action on the customer's own stream before it is applied (K10)."""
    ref = await trace.append(
        f"customer-{who.customer_id}",
        decision_type,
        event_class,
        {"customer_id": str(who.customer_id), "user": who.user, **payload},
        [reason],
    )
    return ref.trace_id


async def _own_contract(contract_id: UUID, who: CustomerIdentity) -> Contract:
    try:
        contract = await contracts.get_contract(contract_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None
    if contract.customer_id != who.customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return contract


async def _own_obligation(store: CustomerStore, who: CustomerIdentity, obligation_id: UUID) -> Obligation:
    obligation = await store.obligation_for_customer(who.customer_id, obligation_id)
    if obligation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return obligation


@router.get("/me")
async def me(who: Who) -> dict[str, str]:
    return {"user": who.user, "customer_id": str(who.customer_id)}


@router.get("/contracts")
async def my_contracts(who: Who) -> dict[str, Any]:
    """`{"contracts": [...]}` with an `id` per contract (the customer simulator discovers its contract here)."""
    rows = await contracts.list_contracts(customer_id=who.customer_id)
    return {"contracts": [{"id": str(c.contract_id), **c.model_dump(mode="json")} for c in rows]}


@router.post("/opportunities", status_code=status.HTTP_201_CREATED, response_model=None)
async def submit_opportunity(body: OpportunitySubmission, who: Who) -> dict[str, Any] | JSONResponse:
    """Admission through `contracts.admit_priced`; a rejection answers 409 with its `reason_code` (at the
    top level, and under `detail` like every other api error). The new opportunity competes only for
    uncommitted headroom at the next selector gate (BUILD.md S2)."""
    if body.customer_id is not None and body.customer_id != who.customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    contract = await _own_contract(body.contract_id, who)
    try:
        opportunity = await contracts.admit_priced(
            contract.contract_id, body.window_start, body.window_end, body.requested_kw
        )
    except AdmissionError as exc:
        reason = {"reason_code": exc.reason_code}
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={**reason, "detail": reason})
    return {"id": str(opportunity.opportunity_id), **opportunity.model_dump(mode="json")}


@router.get("/obligations")
async def my_obligations(store: Store, who: Who, state: ObligationState | None = None) -> dict[str, Any]:
    """The caller's obligations with their state and `at_risk` flag (K1/K13/K14 risk)."""
    rows = await store.obligations_for_customer(who.customer_id, state=state)
    return {"obligations": [{"id": str(o.obligation_id), **o.model_dump(mode="json")} for o in rows]}


@router.get("/obligations/{obligation_id}")
async def my_obligation(obligation_id: UUID, store: Store, who: Who) -> dict[str, Any]:
    return (await _own_obligation(store, who, obligation_id)).model_dump(mode="json")


@router.get("/invoices")
async def my_invoice_lines(
    api_store: Annotated[StoreProtocol, Depends(get_store)],
    who: Who,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """Invoice lines for the period (default: `DEFAULT_INVOICE_LOOKBACK_DAYS` back to
    `DEFAULT_INVOICE_LOOKAHEAD_DAYS` ahead), from the billing query (`StoreProtocol.invoice_lines`), keeping only the
    caller's contracts. Each line's `id` is what `invoices/{id}/dispute` takes."""
    today = datetime.now(UTC).date()
    t0 = from_ or today - timedelta(days=DEFAULT_INVOICE_LOOKBACK_DAYS)
    t1 = to or today + timedelta(days=DEFAULT_INVOICE_LOOKAHEAD_DAYS)
    own = {c.contract_id for c in await contracts.list_contracts(customer_id=who.customer_id)}
    lines = [line for line in await api_store.invoice_lines(t0=t0, t1=t1) if line.contract_id in own]
    return {"invoices": [{"id": str(line.invoice_line_id), **line.model_dump(mode="json")} for line in lines]}


@router.post("/invoices/{invoice_line_id}/dispute", status_code=status.HTTP_201_CREATED)
async def dispute_invoice_line(
    invoice_line_id: UUID, body: DisputeSubmission, store: Store, trace: Trace, who: Who
) -> dict[str, Any]:
    """Record a dispute (traced, `og.invoice_dispute`, visible to operators at `/og/api/customer-disputes`).
    One live dispute per invoice line (409 otherwise)."""
    line = await store.invoice_line_for_customer(who.customer_id, invoice_line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    trace_id = await _trace(
        trace,
        who,
        "SETTLEMENT",
        CUSTOMER_DISPUTE_EVENT_CLASS,
        {"invoice_line_id": str(invoice_line_id), "reason": body.reason, "reason_code": body.reason_code},
        R_CUSTOMER_DISPUTE,
    )
    dispute = InvoiceDispute(
        dispute_id=uuid4(),
        invoice_line_id=invoice_line_id,
        contract_id=line.contract_id,
        customer_id=who.customer_id,
        reason=body.reason,
        reason_code=body.reason_code,
        raised_by=who.user,
        trace_id=trace_id,
    )
    try:
        await store.insert_dispute(dispute)
    except OpenItemExistsError:
        raise _conflict(R_CUSTOMER_DISPUTE_OPEN) from None
    return dispute.model_dump(mode="json")


@router.get("/disputes")
async def my_disputes(store: Store, who: Who) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in await store.list_disputes(customer_id=who.customer_id)]


@router.get("/requests")
async def my_requests(store: Store, who: Who) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in await store.list_requests(customer_id=who.customer_id)]


@router.post("/obligations/{obligation_id}/cancel", response_model=None)
async def cancel_obligation(
    obligation_id: UUID, body: CancelSubmission, store: Store, trace: Trace, who: Who
) -> JSONResponse:
    """`rules.cancel_decision`: 200 when cancelled now (only before commitment), 202 when it became an
    operator-reviewed request (the obligation stays committed, K13), 409 when refused."""
    obligation = await _own_obligation(store, who, obligation_id)
    decision = cancel_decision(obligation.state)
    if decision.disposition is Disposition.REFUSE:
        raise _conflict(decision.rule_code, state=obligation.state)
    contract = await _own_contract(obligation.contract_id, who)
    trace_id = await _trace(
        trace,
        who,
        "COMMITMENT",
        CUSTOMER_REQUEST_EVENT_CLASS,
        _request_payload(obligation, "CANCEL", decision, body.reason),
        decision.rule_code,
    )
    if decision.disposition is Disposition.APPLY_NOW:
        await _apply_pre_commit_cancel(obligation, decision, trace_id)
    request = _request_row(obligation, contract, who, decision, trace_id, kind="CANCEL")
    await _insert_request(store, request)
    code = status.HTTP_200_OK if decision.disposition is Disposition.APPLY_NOW else status.HTTP_202_ACCEPTED
    return JSONResponse(status_code=code, content=request.model_dump(mode="json"))


@router.post("/obligations/{obligation_id}/renominate", status_code=status.HTTP_202_ACCEPTED)
async def renominate_obligation(
    obligation_id: UUID, body: RenominateSubmission, store: Store, trace: Trace, who: Who
) -> dict[str, Any]:
    """`rules.renominate_decision`: queued (202) for the next declared re-nomination point when the
    contract allows it; the commitment itself only changes at that point's selector gate (K13)."""
    obligation = await _own_obligation(store, who, obligation_id)
    contract = await _own_contract(obligation.contract_id, who)
    point = await store.upcoming_renomination_point(
        contract.contract_id, obligation.obligation_id, after=datetime.now(UTC), before=obligation.window_end
    )
    decision = renominate_decision(
        obligation.state,
        renomination_allowed=contract.renomination_allowed,
        has_upcoming_point=point is not None,
    )
    if decision.disposition is Disposition.REFUSE or point is None:
        raise _conflict(decision.rule_code, state=obligation.state)
    payload = _request_payload(obligation, "RENOMINATE", decision, body.reason)
    requested = {
        "requested_kw": body.requested_kw,
        "requested_window_start": body.window_start,
        "requested_window_end": body.window_end,
        "renomination_point_id": point.renomination_point_id,
    }
    payload |= {k: None if v is None else str(v) for k, v in requested.items()}
    trace_id = await _trace(
        trace, who, "RENOMINATION", CUSTOMER_REQUEST_EVENT_CLASS, payload, decision.rule_code
    )
    request = _request_row(obligation, contract, who, decision, trace_id, kind="RENOMINATE").model_copy(
        update=requested
    )
    await _insert_request(store, request)
    return request.model_dump(mode="json")


async def _apply_pre_commit_cancel(obligation: Obligation, decision: RuleDecision, trace_id: UUID) -> None:
    """`OFFERED -> REJECTED` through `opengrid.contracts` (the only writer of obligation state). If a
    selector gate moved the obligation meanwhile, the cancel is refused; a retry then files a review."""
    try:
        await contracts.transition_obligation(
            obligation.obligation_id,
            "REJECTED",
            reason_code=CANCEL_TRANSITION_REASON,
            payload={
                "customer_cancel": True,
                "rule_code": decision.rule_code,
                "customer_trace_id": str(trace_id),
            },
        )
    except (IllegalTransitionError, ConcurrentUpdateError):
        raise _conflict(R_CUSTOMER_CANCEL_STATE_CHANGED) from None


async def _insert_request(store: CustomerStore, request: CustomerObligationRequest) -> None:
    try:
        await store.insert_request(request)
    except OpenItemExistsError:
        raise _conflict(R_CUSTOMER_REQUEST_OPEN) from None


def _request_payload(
    obligation: Obligation, kind: str, decision: RuleDecision, reason: str | None
) -> dict[str, Any]:
    return {
        "obligation_id": str(obligation.obligation_id),
        "kind": kind,
        "obligation_state": obligation.state,
        "disposition": decision.disposition.value,
        "reason": reason,
    }


_STATUS_FOR: dict[Disposition, str] = {
    Disposition.APPLY_NOW: "APPLIED",
    Disposition.OPERATOR_REVIEW: "PENDING_OPERATOR_REVIEW",
    Disposition.QUEUE_FOR_RENOMINATION: "QUEUED_FOR_RENOMINATION",
}


def _request_row(
    obligation: Obligation,
    contract: Contract,
    who: CustomerIdentity,
    decision: RuleDecision,
    trace_id: UUID,
    *,
    kind: str,
) -> CustomerObligationRequest:
    return CustomerObligationRequest.model_validate(
        {
            "request_id": uuid4(),
            "obligation_id": obligation.obligation_id,
            "contract_id": obligation.contract_id,
            "customer_id": who.customer_id,
            "kind": kind,
            "obligation_state": obligation.state,
            "penalty_terms": penalty_terms(contract)
            if decision.disposition is Disposition.OPERATOR_REVIEW
            else None,
            "rule_code": decision.rule_code,
            "status": _STATUS_FOR[decision.disposition],
            "raised_by": who.user,
            "trace_id": trace_id,
        }
    )
