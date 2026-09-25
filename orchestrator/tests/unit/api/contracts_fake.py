"""Minimal in-memory `opengrid.contracts.repository.ContractsRepo` for `api`'s own unit tests.

Only the CRUD surface `opengrid.api.routers.contracts` actually calls is implemented; admission's
fuller machinery (product rules, opportunity/obligation creation) is exercised by
`tests/unit/contracts/` (owned by the selector agent) -- `api`'s own test for that path
(`test_opportunity_creation_surfaces_not_implemented_as_503`) only checks the API-layer contract for a
collaborator this fake does not model.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from opengrid.core.models.engine import (
    Contract,
    Obligation,
    ObligationState,
    Opportunity,
    ProductRule,
    RenominationPoint,
)


class FakeContractsRepo:
    def __init__(self) -> None:
        self.contracts: dict[UUID, Contract] = {}

    async def get_contract(self, contract_id: UUID) -> Contract | None:
        return self.contracts.get(contract_id)

    async def list_contracts(
        self, *, customer_id: UUID | None = None, service_type: str | None = None, status: str | None = None
    ) -> list[Contract]:
        rows = self.contracts.values()
        if customer_id is not None:
            rows = [r for r in rows if r.customer_id == customer_id]
        if service_type is not None:
            rows = [r for r in rows if r.service_type == service_type]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        return list(rows)

    async def list_customer_ids(self) -> list[UUID]:
        return sorted({c.customer_id for c in self.contracts.values()}, key=str)

    async def upsert_contract(self, contract: Contract) -> Contract:
        self.contracts[contract.contract_id] = contract
        return contract

    async def get_product_rules(self, contract_id: UUID) -> list[ProductRule]:
        return []

    async def upsert_product_rule(self, rule: ProductRule) -> ProductRule:
        raise NotImplementedError

    async def create_opportunity_and_obligation(
        self, opportunity: Opportunity, obligation: Obligation
    ) -> None:
        raise NotImplementedError

    async def get_opportunity(self, opportunity_id: UUID) -> Opportunity | None:
        return None

    async def update_opportunity_state(
        self, opportunity_id: UUID, *, state: str, reason_code: str | None, decided_at: datetime
    ) -> Opportunity:
        raise NotImplementedError

    async def get_obligation(self, obligation_id: UUID) -> Obligation | None:
        return None

    async def get_obligation_by_opportunity(self, opportunity_id: UUID) -> Obligation | None:
        return None

    async def update_obligation_state(
        self,
        obligation_id: UUID,
        *,
        to_state: ObligationState,
        reason_code: str | None,
        expected_version: int,
        at_risk: bool | None = None,
    ) -> Obligation:
        raise NotImplementedError

    async def active_obligations_by_interval(
        self,
        interval_start: datetime,
        interval_end: datetime,
        *,
        service_type: str | None = None,
        states: tuple[ObligationState, ...] = ("COMMITTED", "DELIVERING"),
    ) -> list[Obligation]:
        return []

    async def unselected_offered_before(self, cutoff: datetime) -> list[Opportunity]:
        return []

    async def due_renomination_points(self, as_of: datetime) -> list[RenominationPoint]:
        return []

    async def get_renomination_point(self, renomination_point_id: UUID) -> RenominationPoint | None:
        return None

    async def mark_renomination_exercised(
        self,
        renomination_point_id: UUID,
        *,
        outcome: str,
        plan_id: UUID | None,
        exercised_at: datetime,
    ) -> RenominationPoint:
        raise NotImplementedError
