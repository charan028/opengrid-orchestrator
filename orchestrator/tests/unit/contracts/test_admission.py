"""Admission tests (ES04-S02/S03/S05, TS-04-15/16). Exercises `opengrid.contracts.admit` end to end
against the fake repo/trace fixtures from conftest.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.contracts.errors import AdmissionError

from .conftest import make_contract, make_product_rule
from .fakes import FakeContractsRepo, FakeTraceBackend


def _window() -> tuple[datetime, datetime]:
    start = datetime.now(UTC) + timedelta(hours=1)
    return start, start + timedelta(minutes=15)


async def test_admit_creates_paired_opportunity_and_obligation(repo: FakeContractsRepo) -> None:
    contract = make_contract(service_type="HOME")
    await repo.upsert_contract(contract)
    start, end = _window()

    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("5"))

    assert opportunity.state == "OFFERED"
    obligation = await repo.get_obligation_by_opportunity(opportunity.opportunity_id)
    assert obligation is not None
    assert obligation.state == "OFFERED"
    assert obligation.contract_id == contract.contract_id
    assert obligation.committed_qty_kw == Decimal("5")


async def test_admit_unknown_contract_is_rejected_and_traced(
    repo: FakeContractsRepo, trace_backend: FakeTraceBackend
) -> None:
    start, end = _window()
    with pytest.raises(AdmissionError) as exc_info:
        await contracts.admit(uuid4(), start, end, Decimal("5"))
    assert exc_info.value.reason_code == "R-ADMIT-UNKNOWN-CONTRACT"
    assert any(r["reason_codes"] == ["R-ADMIT-UNKNOWN-CONTRACT"] for r in trace_backend.rows)


async def test_admit_inactive_contract_is_rejected(repo: FakeContractsRepo) -> None:
    contract = make_contract(status="SUSPENDED")
    await repo.upsert_contract(contract)
    start, end = _window()
    with pytest.raises(AdmissionError) as exc_info:
        await contracts.admit(contract.contract_id, start, end, Decimal("5"))
    assert exc_info.value.reason_code == "R-ADMIT-CONTRACT-INACTIVE"


async def test_admit_invalid_window_is_rejected(repo: FakeContractsRepo) -> None:
    contract = make_contract()
    await repo.upsert_contract(contract)
    start, end = _window()
    with pytest.raises(AdmissionError) as exc_info:
        await contracts.admit(contract.contract_id, end, start, Decimal("5"))
    assert exc_info.value.reason_code == "R-ADMIT-INVALID-WINDOW"


async def test_admit_non_positive_quantity_is_rejected(repo: FakeContractsRepo) -> None:
    contract = make_contract()
    await repo.upsert_contract(contract)
    start, end = _window()
    with pytest.raises(AdmissionError) as exc_info:
        await contracts.admit(contract.contract_id, start, end, Decimal("0"))
    assert exc_info.value.reason_code == "R-ADMIT-INVALID-QUANTITY"


async def test_admit_rounds_semi_continuous_down_to_the_increment(repo: FakeContractsRepo) -> None:
    """TS-04-15: min_qty 0.1 MW, increment 0.1 MW; offering 0.25 MW takes 0.2 MW, never 0.25 directly
    and never below min_qty."""
    contract = make_contract(service_type="ERCOT_AS")
    await repo.upsert_contract(contract)
    rule = make_product_rule(
        contract.contract_id,
        product_code="NONSPIN",
        min_qty_kw=Decimal("100"),
        increment_kw=Decimal("100"),
        variable_kind="SEMI_CONTINUOUS",
    )
    await repo.upsert_product_rule(rule)
    start, end = _window()

    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("250"))

    assert opportunity.requested_kw == Decimal("200")


async def test_admit_block_rule_rejects_a_partial_amount(repo: FakeContractsRepo) -> None:
    """TS-04-16: an all-or-nothing block rejects a request insufficient for the full block, never
    partially admits it."""
    contract = make_contract(service_type="PARTNER_CAPACITY")
    await repo.upsert_contract(contract)
    rule = make_product_rule(
        contract.contract_id,
        product_code="EVENT_BLOCK",
        min_qty_kw=Decimal("50"),
        increment_kw=Decimal("0"),
        block=True,
        variable_kind="BINARY",
    )
    await repo.upsert_product_rule(rule)
    start, end = _window()

    with pytest.raises(AdmissionError) as exc_info:
        await contracts.admit(contract.contract_id, start, end, Decimal("30"))
    assert exc_info.value.reason_code == "R-ADMIT-INFEASIBLE-PRODUCT-RULE"


async def test_admit_block_rule_admits_exactly_the_block_size(repo: FakeContractsRepo) -> None:
    contract = make_contract(service_type="PARTNER_CAPACITY")
    await repo.upsert_contract(contract)
    rule = make_product_rule(
        contract.contract_id,
        product_code="EVENT_BLOCK",
        min_qty_kw=Decimal("50"),
        increment_kw=Decimal("0"),
        block=True,
        variable_kind="BINARY",
    )
    await repo.upsert_product_rule(rule)
    start, end = _window()

    opportunity = await contracts.admit(contract.contract_id, start, end, Decimal("50"))
    assert opportunity.requested_kw == Decimal("50")
