"""Fixtures for `opengrid.api` unit tests: a `TestClient` wired to fakes via `dependency_overrides`, so
none of `create_app()`'s lifespan (real Postgres pool) ever runs (BUILD.md S5: "Local ... no DB/MQTT").
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import opengrid.contracts as contracts_module
from opengrid.api.app import create_app
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalStore
from opengrid.core.models.engine import Contract
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

from .contracts_fake import FakeContractsRepo
from .fakes import SAMPLE_CONTRACT_ID, SAMPLE_CUSTOMER_ID, FakeStore, FakeTraceBackend

OPERATOR_HEADERS = {"X-Remote-User": "operator"}
VIEWER_HEADERS = {"X-Remote-User": "viewer"}


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def fake_trace_store() -> TraceStore:
    return TraceStore(FakeTraceBackend())


@pytest.fixture
def fake_proposals() -> ProposalStore:
    return ProposalStore()


@pytest.fixture
def fake_config() -> Config:
    return Config({"api": {"sse_heartbeat_s": 15, "roles": {"operator": [], "viewer": []}}})


@pytest.fixture(autouse=True)
def _configured_contracts(fake_trace_store):
    """`opengrid.contracts` (02a, selector agent) is a module-level singleton configured once by its
    owning process -- `api`'s own tests wire a minimal in-memory fake here rather than depending on
    another agent's `tests/unit/contracts/fakes.py`, and reset it afterwards so this suite's state
    never leaks into another module's test run."""
    repo = FakeContractsRepo()
    repo.contracts[SAMPLE_CONTRACT_ID] = Contract(
        contract_id=SAMPLE_CONTRACT_ID,
        customer_id=SAMPLE_CUSTOMER_ID,
        service_type="ERCOT_ENERGY",
        tier="T2",
        profile_ref="ercot-energy-profile@1",
        start_at=datetime.now(UTC),
        status="ACTIVE",
    )
    contracts_module.configure(repo, fake_trace_store)
    yield
    contracts_module.reset_for_testing()


@pytest.fixture
def client(fake_store, fake_trace_store, fake_proposals, fake_config) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[get_trace_store] = lambda: fake_trace_store
    app.dependency_overrides[get_proposals] = lambda: fake_proposals
    app.dependency_overrides[get_config] = lambda: fake_config
    return TestClient(app, client=("127.0.0.1", 51234))


@pytest.fixture
def non_loopback_client(fake_store, fake_trace_store, fake_proposals, fake_config) -> TestClient:
    """Default `TestClient` host ("testclient") is deliberately *not* loopback -- used to prove
    `/og/api/health` rejects non-loopback callers regardless of headers (`opengrid.api.auth`)."""
    app = create_app()
    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[get_trace_store] = lambda: fake_trace_store
    app.dependency_overrides[get_proposals] = lambda: fake_proposals
    app.dependency_overrides[get_config] = lambda: fake_config
    return TestClient(app)
