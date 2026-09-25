"""Multi-customer tests (BUILD.md S2: "several customers and obligations concurrently"). Admits and
progresses obligations for several customers/contracts/services at once and checks none of them
interfere -- no shared mutable state leaks between obligations, and each obligation's transitions
are independent."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import opengrid.contracts as contracts
from opengrid.core.models.engine import ServiceType

from .conftest import make_contract
from .fakes import FakeContractsRepo

SERVICES: tuple[ServiceType, ...] = ("HOME", "ERCOT_ENERGY", "ERCOT_AS", "DIST_DEFERRAL", "PARTNER_CAPACITY")


def _window(offset_minutes: int = 60) -> tuple[datetime, datetime]:
    start = datetime.now(UTC) + timedelta(minutes=offset_minutes)
    return start, start + timedelta(minutes=15)


async def test_five_customers_admit_concurrently_without_interference(repo: FakeContractsRepo) -> None:
    contracts_by_service = {service: make_contract(service_type=service) for service in SERVICES}
    for contract in contracts_by_service.values():
        await repo.upsert_contract(contract)

    start, end = _window()
    opportunities = await asyncio.gather(
        *(
            contracts.admit(contract.contract_id, start, end, Decimal("10") * (i + 1))
            for i, contract in enumerate(contracts_by_service.values())
        )
    )

    assert len({o.opportunity_id for o in opportunities}) == len(SERVICES)
    obligations = [await repo.get_obligation_by_opportunity(o.opportunity_id) for o in opportunities]
    assert all(o is not None for o in obligations)
    # Each obligation belongs to a distinct contract/customer, and none share an obligation_id.
    assert len({o.obligation_id for o in obligations if o is not None}) == len(SERVICES)
    assert len({o.contract_id for o in obligations if o is not None}) == len(SERVICES)


async def test_transitioning_one_customers_obligation_does_not_affect_another(
    repo: FakeContractsRepo,
) -> None:
    contract_a = make_contract(service_type="ERCOT_ENERGY")
    contract_b = make_contract(service_type="ERCOT_AS")
    await repo.upsert_contract(contract_a)
    await repo.upsert_contract(contract_b)
    start, end = _window()

    opp_a = await contracts.admit(contract_a.contract_id, start, end, Decimal("5"))
    opp_b = await contracts.admit(contract_b.contract_id, start, end, Decimal("7"))
    obligation_a = await repo.get_obligation_by_opportunity(opp_a.opportunity_id)
    obligation_b = await repo.get_obligation_by_opportunity(opp_b.opportunity_id)
    assert obligation_a is not None and obligation_b is not None

    await contracts.transition_obligation(obligation_a.obligation_id, "SELECTED", reason_code="R-GATE-SELECT")

    assert repo.obligations[obligation_a.obligation_id].state == "SELECTED"
    assert repo.obligations[obligation_b.obligation_id].state == "OFFERED"


async def test_active_obligations_by_interval_scopes_correctly_across_customers(
    repo: FakeContractsRepo,
) -> None:
    now = datetime.now(UTC)
    contracts_by_service = {service: make_contract(service_type=service) for service in SERVICES}
    for i, contract in enumerate(contracts_by_service.values()):
        await repo.upsert_contract(contract)
        start = now + timedelta(minutes=15 * i)
        opportunity = await contracts.admit(
            contract.contract_id, start, start + timedelta(minutes=15), Decimal("3")
        )
        obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
        assert obligation is not None
        # Fast-forward straight to COMMITTED so the query under test (COMMITTED/DELIVERING by
        # default) sees it -- the FSM transitions themselves are covered by test_lifecycle.py.
        await contracts.transition_obligation(
            obligation.obligation_id, "SELECTED", reason_code="R-GATE-SELECT"
        )
        await contracts.transition_obligation(
            obligation.obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER"
        )

    first_service_window = await contracts.active_obligations(now, now + timedelta(minutes=15))
    assert len(first_service_window) == 1
    assert first_service_window[0].service_type == next(iter(SERVICES))

    full_horizon = await contracts.active_obligations(now, now + timedelta(minutes=15 * len(SERVICES)))
    assert len(full_horizon) == len(SERVICES)
    assert len({o.contract_id for o in full_horizon}) == len(SERVICES)
