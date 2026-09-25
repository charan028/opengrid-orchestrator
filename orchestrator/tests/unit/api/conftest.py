"""Fixtures for `opengrid.api` unit tests: a `TestClient` wired to fakes via `dependency_overrides`, so
none of `create_app()`'s lifespan (real Postgres pool) ever runs (BUILD.md S5: "Local ... no DB/MQTT").
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opengrid.api.app import create_app
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalStore
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

from .fakes import FakeStore, FakeTraceBackend

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
