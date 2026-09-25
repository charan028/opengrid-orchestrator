"""Customers/contracts CRUD (operator only) -- BUILD.md api row: "customers/contracts/opportunities
CRUD via the contracts module." Every read/write here delegates to `opengrid.contracts` (02a, owned by
the selector agent), which is the sole owner of `og.contract`/`og.product_rule`/`og.opportunity` writes
(its own module docstring: "nothing else writes those tables") -- `api` configures its own
`PgContractsRepo`/`TraceStore` pair for this module at start-up (`opengrid.api.app`'s lifespan) rather
than re-deriving admission/CRUD logic here (BUILD.md S1 "no duplicated functions").

MVP-S has no dedicated customer table (`opengrid.contracts.crud`'s own docstring): a "customer" is the
set of contracts sharing a `customer_id`, so `/customers` is derived from `list_contracts()`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status

import opengrid.contracts as contracts
from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_store, get_trace_store
from opengrid.api.schemas import ContractCreate, ContractStatusUpdate
from opengrid.api.store import StoreProtocol
from opengrid.core.models.engine import Contract
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api", tags=["contracts"])


@router.get("/customers")
async def list_customers(_identity: Annotated[Identity, Depends(require_viewer)]) -> list[dict[str, Any]]:
    all_contracts = await contracts.list_contracts()
    by_customer: dict[UUID, list[Contract]] = {}
    for contract in all_contracts:
        by_customer.setdefault(contract.customer_id, []).append(contract)
    return [
        {
            "customer_id": str(customer_id),
            "contract_count": len(rows),
            "service_types": sorted({r.service_type for r in rows}),
        }
        for customer_id, rows in sorted(by_customer.items(), key=lambda kv: str(kv[0]))
    ]


@router.get("/contracts")
async def list_contracts_route(
    _identity: Annotated[Identity, Depends(require_viewer)],
    customer_id: UUID | None = None,
) -> list[dict[str, Any]]:
    rows = await contracts.list_contracts(customer_id=customer_id)
    return [c.model_dump(mode="json") for c in rows]


@router.get("/contracts/{contract_id}")
async def get_contract_route(
    contract_id: UUID, _identity: Annotated[Identity, Depends(require_viewer)]
) -> dict[str, Any]:
    contract = await contracts.get_contract(contract_id)
    return contract.model_dump(mode="json")


@router.post("/contracts", status_code=status.HTTP_201_CREATED)
async def create_contract(
    body: ContractCreate,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    contract = Contract(contract_id=uuid4(), status="ACTIVE", **body.model_dump())
    created = await contracts.create_contract(contract)
    await _record_operator_action(
        store, trace_store, identity, target_ref=str(created.contract_id), reason="contract created"
    )
    return created.model_dump(mode="json") | {"created_by": identity.user}


@router.patch("/contracts/{contract_id}")
async def update_contract_status(
    contract_id: UUID,
    body: ContractStatusUpdate,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    updated = await contracts.set_contract_status(contract_id, body.status)
    await _record_operator_action(
        store, trace_store, identity, target_ref=str(contract_id), reason=f"status -> {body.status}"
    )
    return updated.model_dump(mode="json") | {"updated_by": identity.user}


async def _record_operator_action(
    store: StoreProtocol, trace_store: TraceStore, identity: Identity, *, target_ref: str, reason: str
) -> None:
    """`opengrid.contracts`'s own CRUD helpers don't trace themselves (only `admit`/lifecycle
    transitions do) -- `api` records the operator-facing audit trail for its own CRUD endpoints
    (BUILD.md api row: "every write is recorded in operator_action + trace")."""
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="CONFIG_CHANGE",
        payload={"decision_ref": target_ref, "action": reason},
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="CONFIG_CHANGE",
        target_ref=target_ref,
        tier=None,
        reason=reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
