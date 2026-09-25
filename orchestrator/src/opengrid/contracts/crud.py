"""Customer/contract/product-rule CRUD (ES04-S01). MVP-S has no dedicated customer table -- a
"customer" is simply the set of contracts sharing a `customer_id` (02a S1.2's "opaque; customer
directory lives" elsewhere) -- so `list_customer_ids` derives the directory from `contract` rather
than duplicating one. Used by the API agent's `/contracts`, `/customers` endpoints.
"""

from __future__ import annotations

from typing import Literal, get_args
from uuid import UUID

from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import Contract, ProductRule

ContractStatus = Literal["ACTIVE", "SUSPENDED", "ENDED"]
_CONTRACT_STATUSES: frozenset[str] = frozenset(get_args(ContractStatus))


async def list_contracts(
    repo: ContractsRepo,
    *,
    customer_id: UUID | None = None,
    service_type: str | None = None,
    status: str | None = None,
) -> list[Contract]:
    return await repo.list_contracts(customer_id=customer_id, service_type=service_type, status=status)


async def list_customer_ids(repo: ContractsRepo) -> list[UUID]:
    return await repo.list_customer_ids()


async def create_contract(repo: ContractsRepo, contract: Contract) -> Contract:
    return await repo.upsert_contract(contract)


async def set_contract_status(repo: ContractsRepo, contract_id: UUID, status: str) -> Contract:
    if status not in _CONTRACT_STATUSES:
        raise ValueError(f"invalid contract status: {status!r}")
    existing = await repo.get_contract(contract_id)
    if existing is None:
        raise LookupError(f"no such contract: {contract_id}")
    updated = existing.model_copy(update={"status": status})
    return await repo.upsert_contract(updated)


async def create_product_rule(repo: ContractsRepo, rule: ProductRule) -> ProductRule:
    return await repo.upsert_product_rule(rule)
