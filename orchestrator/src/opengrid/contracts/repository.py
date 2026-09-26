"""Storage contract `opengrid.contracts` needs (mirrors `opengrid.trace.store.TraceBackend`'s
pattern, BUILD.md S5a "pure logic separated from I/O"). A real backend is `pg_repo.PgContractsRepo`;
unit tests use an in-memory fake (`tests/unit/contracts/fakes.py`) so admission/lifecycle/
re-nomination logic is testable without Postgres.

Only `opengrid.contracts` writes `og.contract`, `og.product_rule`, `og.opportunity`,
`og.obligation` and `og.renomination_point` (BUILD.md S4 directory ownership); `selector`/`ledger`/
`settle` read obligations/commitments through the query functions here rather than querying the
tables directly, so the admission/lifecycle rules stay in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from opengrid.core.models.engine import (
    Contract,
    Obligation,
    ObligationState,
    Opportunity,
    ProductRule,
    RenominationPoint,
)


class ContractsRepo(Protocol):
    """Async storage contract. Every method is a single statement/transaction; multi-step workflows
    (admission, transitions, re-nomination) live in `admission.py`/`lifecycle.py`/`renomination.py`
    and call these primitives."""

    # -- contract / product_rule CRUD (ES04-S01) ------------------------------------------------
    async def get_contract(self, contract_id: UUID) -> Contract | None: ...

    async def list_contracts(
        self, *, customer_id: UUID | None = None, service_type: str | None = None, status: str | None = None
    ) -> list[Contract]: ...

    async def list_customer_ids(self) -> list[UUID]: ...

    async def upsert_contract(self, contract: Contract) -> Contract: ...

    async def get_product_rules(self, contract_id: UUID) -> list[ProductRule]: ...

    async def upsert_product_rule(self, rule: ProductRule) -> ProductRule: ...

    # -- opportunity / obligation admission (ES04-S02) ------------------------------------------
    async def create_opportunity_and_obligation(
        self, opportunity: Opportunity, obligation: Obligation
    ) -> None:
        """Atomically insert both rows at `OFFERED` (02a S1.4/S1.5's 1:1 "admitted as" edge)."""
        ...

    async def get_opportunity(self, opportunity_id: UUID) -> Opportunity | None: ...

    async def update_opportunity_state(
        self,
        opportunity_id: UUID,
        *,
        state: str,
        reason_code: str | None,
        decided_at: datetime,
        gate_id: UUID | None = None,
    ) -> Opportunity:
        """Set the opportunity's decision; `gate_id` (the deciding plan) is kept when not given."""
        ...

    # -- obligation lifecycle (02a S2.1) ---------------------------------------------------------
    async def get_obligation(self, obligation_id: UUID) -> Obligation | None: ...

    async def get_obligation_by_opportunity(self, opportunity_id: UUID) -> Obligation | None: ...

    async def update_obligation_state(
        self,
        obligation_id: UUID,
        *,
        to_state: ObligationState,
        reason_code: str | None,
        expected_version: int,
        at_risk: bool | None = None,
    ) -> Obligation:
        """Compare-and-swap on `version` (02a S1.5's optimistic lock). Raises
        `ConcurrentUpdateError` if the row is no longer at `expected_version`."""
        ...

    async def set_obligation_at_risk(self, obligation_id: UUID, at_risk: bool) -> Obligation:
        """Set the `at_risk` flag only (no state change, no version bump). Raises `LookupError`."""
        ...

    async def active_obligations_by_interval(
        self,
        interval_start: datetime,
        interval_end: datetime,
        *,
        service_type: str | None = None,
        states: tuple[ObligationState, ...] = ("COMMITTED", "DELIVERING"),
    ) -> list[Obligation]:
        """Obligations in `states` whose `[window_start, window_end)` overlaps the given interval --
        what `selector.load_frozen_commitments` and the allocator's per-cycle scope both need."""
        ...

    async def unselected_offered_before(self, cutoff: datetime) -> list[Opportunity]:
        """`OFFERED` opportunities whose `window_start` has passed with no selection (02a S2.1's
        `OFFERED -> EXPIRED`)."""
        ...

    async def find_opportunity_by_window(
        self, contract_id: UUID, window_start: datetime, window_end: datetime
    ) -> Opportunity | None:
        """The opportunity (any state) already admitted for this contract/window, or `None`.

        `opengrid.contracts.intake` calls this before `admit()` at every gate so re-running intake
        against the same contract/interval never creates a duplicate opportunity (its idempotency
        requirement) -- MVP-S contracts carry at most one saleable product per contract (02a S1.3), so
        `(contract_id, window_start, window_end)` alone is a sufficient dedupe key without a product
        code column."""
        ...

    # -- re-nomination points (02a S1.7, ES04-S04) -----------------------------------------------
    async def due_renomination_points(self, as_of: datetime) -> list[RenominationPoint]: ...

    async def get_renomination_point(self, renomination_point_id: UUID) -> RenominationPoint | None: ...

    async def mark_renomination_exercised(
        self,
        renomination_point_id: UUID,
        *,
        outcome: str,
        plan_id: UUID | None,
        exercised_at: datetime,
    ) -> RenominationPoint: ...
