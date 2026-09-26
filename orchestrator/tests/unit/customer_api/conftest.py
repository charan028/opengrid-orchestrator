"""Customer API fixtures: the real `create_app()` with fakes behind every dependency, two customers (A and
B) with one contract each, and helpers to seed obligations and invoice lines."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import opengrid.api.app as api_app
import opengrid.contracts as contracts_module
from opengrid.api.app import create_app
from opengrid.api.auth import PROXY_SECRET_ENV
from opengrid.api.csrf import COOKIE_NAME, HEADER_NAME
from opengrid.api.deps import get_config, get_store, get_trace_store
from opengrid.core.models.engine import Contract, InvoiceLine, Obligation, ObligationState
from opengrid.customer_api.store import get_customer_store
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

from ..contracts.fakes import FakeContractsRepo, FakeTraceBackend
from .fakes import FakeBillingStore, FakeCustomerStore

PROXY_SECRET = "customer-api-test-secret"  # noqa: S105 -- a test value, not a secret
CUSTOMER_A = UUID("00000000-0000-7000-8000-0000000000c6")
CUSTOMER_B = UUID("00000000-0000-7000-8000-0000000000c7")
CONTRACT_A = UUID("00000000-0000-7000-8000-0000000000d6")
CONTRACT_B = UUID("00000000-0000-7000-8000-0000000000d7")
USER_A = {"X-Remote-User": "og-cust-a"}
USER_B = {"X-Remote-User": "og-cust-b"}
OPERATOR = {"X-Remote-User": "operator"}
VIEWER = {"X-Remote-User": "viewer"}


def _echo_csrf_cookie(request: httpx.Request) -> None:
    """What the browser page does (double-submit cookie, `opengrid.api.csrf`): echo the `og_csrf` cookie
    into `X-CSRF-Token` on mutating calls, so each test request is a correctly-credentialed one."""
    if request.method.upper() in ("GET", "HEAD", "OPTIONS"):
        return
    for part in request.headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            request.headers[HEADER_NAME] = value


def _contract(contract_id: UUID, customer_id: UUID, *, renomination_allowed: bool) -> Contract:
    return Contract(
        contract_id=contract_id,
        customer_id=customer_id,
        service_type="DATA_CENTER",
        variant="BRIDGING",
        tier="T1",
        profile_ref="data-center-profile@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
        renomination_allowed=renomination_allowed,
        penalty_alpha=Decimal("0.02"),
        penalty_beta=Decimal("0.4"),
        penalty_theta=Decimal("0.05"),
    )


@pytest.fixture
def contracts_repo() -> FakeContractsRepo:
    repo = FakeContractsRepo()
    repo.contracts[CONTRACT_A] = _contract(CONTRACT_A, CUSTOMER_A, renomination_allowed=True)
    repo.contracts[CONTRACT_B] = _contract(CONTRACT_B, CUSTOMER_B, renomination_allowed=False)
    return repo


@pytest.fixture
def trace_backend() -> FakeTraceBackend:
    return FakeTraceBackend()


@pytest.fixture
def customer_store(contracts_repo: FakeContractsRepo) -> FakeCustomerStore:
    return FakeCustomerStore(contracts_repo)


@pytest.fixture
def billing_store() -> FakeBillingStore:
    return FakeBillingStore()


@pytest.fixture
def config() -> Config:
    return Config(
        {
            "api": {
                "roles": {
                    "operator": [],
                    "viewer": [],
                    "customer": {
                        "og-cust-a": str(CUSTOMER_A),
                        "og-cust-b": str(CUSTOMER_B),
                        "og-cust-bad": "x",
                    },
                }
            }
        }
    )


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    contracts_repo: FakeContractsRepo,
    trace_backend: FakeTraceBackend,
    customer_store: FakeCustomerStore,
    billing_store: FakeBillingStore,
    config: Config,
) -> Iterator[TestClient]:
    monkeypatch.setenv(PROXY_SECRET_ENV, PROXY_SECRET)
    monkeypatch.setattr(api_app, "customer_api_enabled", lambda: True)
    trace_store = TraceStore(trace_backend)
    # DATA_CENTER admission is gated behind [contracts.activation].data_center (default closed,
    # opengrid.contracts.admission._is_activation_gated) -- these fixtures' CONTRACT_A is a
    # DATA_CENTER contract exercising a live customer submission flow, not the activation gate
    # itself, so the gate is open here.
    contracts_module.configure(contracts_repo, trace_store, data_center_activation_enabled=True)
    app = create_app()
    app.dependency_overrides[get_store] = lambda: billing_store
    app.dependency_overrides[get_trace_store] = lambda: trace_store
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_customer_store] = lambda: customer_store
    test_client = TestClient(app, client=("127.0.0.1", 51000), headers={"X-OG-Proxy-Auth": PROXY_SECRET})
    test_client.event_hooks = {"request": [_echo_csrf_cookie], "response": []}
    yield test_client
    contracts_module.reset_for_testing()


@pytest.fixture
def add_obligation(contracts_repo: FakeContractsRepo) -> Callable[..., Obligation]:
    def _add(contract_id: UUID, state: ObligationState, *, kw: str = "100") -> Obligation:
        now = datetime.now(UTC)
        obligation = Obligation(
            obligation_id=uuid4(),
            opportunity_id=uuid4(),
            contract_id=contract_id,
            service_type="DATA_CENTER",
            tier="T1",
            window_start=now + timedelta(hours=1),
            window_end=now + timedelta(hours=5),
            committed_qty_kw=Decimal(kw),
            state=state,
        )
        contracts_repo.obligations[obligation.obligation_id] = obligation
        return obligation

    return _add


@pytest.fixture
def add_invoice_line(
    customer_store: FakeCustomerStore, billing_store: FakeBillingStore
) -> Callable[[UUID], InvoiceLine]:
    def _add(contract_id: UUID) -> InvoiceLine:
        line = InvoiceLine(
            invoice_line_id=uuid4(),
            contract_id=contract_id,
            obligation_id=uuid4(),
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            line_type="CAPACITY_PAYMENT",
            amount=Decimal("1200.00"),
        )
        customer_store.invoice_lines[line.invoice_line_id] = line
        billing_store.lines.append(line)
        return line

    return _add
