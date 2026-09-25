"""Customer/contract/product-rule CRUD tests (ES04-S01)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts

from .conftest import make_contract, make_product_rule
from .fakes import FakeContractsRepo


async def test_create_and_list_contracts(repo: FakeContractsRepo) -> None:
    contract = make_contract(service_type="DIST_DEFERRAL")
    created = await contracts.create_contract(contract)

    assert created == contract
    assert await contracts.list_contracts(customer_id=contract.customer_id) == [contract]
    assert await contracts.list_contracts(service_type="DIST_DEFERRAL") == [contract]
    assert await contracts.list_contracts(service_type="HOME") == []


async def test_list_customer_ids_is_the_distinct_set_across_contracts(repo: FakeContractsRepo) -> None:
    a = make_contract()
    b = make_contract()
    await contracts.create_contract(a)
    await contracts.create_contract(b)

    ids = await contracts.list_customer_ids()

    assert set(ids) == {a.customer_id, b.customer_id}


async def test_set_contract_status_updates_the_row(repo: FakeContractsRepo) -> None:
    contract = make_contract(status="ACTIVE")
    await contracts.create_contract(contract)

    updated = await contracts.set_contract_status(contract.contract_id, "SUSPENDED")

    assert updated.status == "SUSPENDED"
    assert (await contracts.get_contract(contract.contract_id)).status == "SUSPENDED"


async def test_set_contract_status_rejects_an_invalid_status(repo: FakeContractsRepo) -> None:
    contract = make_contract()
    await contracts.create_contract(contract)
    with pytest.raises(ValueError, match="invalid contract status"):
        await contracts.set_contract_status(contract.contract_id, "DELETED")


async def test_set_contract_status_unknown_contract_raises(repo: FakeContractsRepo) -> None:
    with pytest.raises(LookupError):
        await contracts.set_contract_status(uuid4(), "ACTIVE")


async def test_create_product_rule_is_visible_via_product_rules_for(repo: FakeContractsRepo) -> None:
    contract = make_contract(service_type="ERCOT_AS")
    await contracts.create_contract(contract)
    rule = make_product_rule(contract.contract_id, product_code="NONSPIN", min_qty_kw=Decimal("100"))

    await contracts.create_product_rule(rule)

    rules = await contracts.product_rules_for(contract.contract_id)
    assert rules == [rule]


async def test_get_contract_unknown_id_raises_lookup_error(repo: FakeContractsRepo) -> None:
    with pytest.raises(LookupError):
        await contracts.get_contract(uuid4())
