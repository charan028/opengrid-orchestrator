"""Shared fixtures for `opengrid.contracts` unit tests: a fresh fake repo + a real `TraceStore` over
a fake backend, wired via `opengrid.contracts.configure()` before each test and cleared after."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.core.models.engine import Contract, ProductRule
from opengrid.trace import TraceStore

from .fakes import FakeContractsRepo, FakeTraceBackend


@pytest.fixture
def repo() -> FakeContractsRepo:
    return FakeContractsRepo()


@pytest.fixture
def trace_backend() -> FakeTraceBackend:
    return FakeTraceBackend()


@pytest.fixture
def trace(trace_backend: FakeTraceBackend) -> TraceStore:
    return TraceStore(trace_backend)


@pytest.fixture(autouse=True)
def _configure(repo: FakeContractsRepo, trace: TraceStore) -> Iterator[None]:
    contracts.configure(repo, trace)
    yield
    contracts.reset_for_testing()


def make_contract(
    *,
    service_type: str = "ERCOT_ENERGY",
    tier: str = "T2",
    status: str = "ACTIVE",
    renomination_allowed: bool = False,
) -> Contract:
    return Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type=service_type,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        profile_ref="profile@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
        renomination_allowed=renomination_allowed,
        status=status,  # type: ignore[arg-type]
    )


def make_product_rule(
    contract_id: object,
    *,
    product_code: str = "ENERGY",
    min_qty_kw: Decimal = Decimal("0"),
    increment_kw: Decimal = Decimal("0"),
    block: bool = False,
    variable_kind: str = "CONTINUOUS",
    duration_minutes: int = 15,
) -> ProductRule:
    return ProductRule(
        product_rule_id=uuid4(),
        contract_id=contract_id,  # type: ignore[arg-type]
        product_code=product_code,
        min_qty_kw=min_qty_kw,
        increment_kw=increment_kw,
        block=block,
        duration_minutes=duration_minutes,
        variable_kind=variable_kind,  # type: ignore[arg-type]
    )
