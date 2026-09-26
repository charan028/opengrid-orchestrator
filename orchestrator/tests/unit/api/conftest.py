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
#: What Apache sets on every proxied request (`opengrid.api.auth.proxy_authenticated`); the `client`
#: fixtures send it by default, as every real request through Apache does.
PROXY_SECRET = "test-proxy-secret"  # noqa: S105 -- a test value, not a secret
PROXY_HEADERS = {"X-OG-Proxy-Auth": PROXY_SECRET}


@pytest.fixture(autouse=True)
def _proxy_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from opengrid.api.auth import PROXY_SECRET_ENV

    monkeypatch.setenv(PROXY_SECRET_ENV, PROXY_SECRET)


def _echo_csrf_cookie_as_header(request: object) -> None:
    """Test-only stand-in for what a real browser + `hx-headers` on `<body>` does (`base.html`,
    qa/security-review.md F-03): once `opengrid.api.csrf.CSRFMiddleware` has set the `og_csrf` cookie on
    a prior response, httpx auto-attaches it to this request's `Cookie` header before this hook runs --
    echo that same value into the `X-CSRF-Token` header so every existing test's mutating call is a
    same-origin, correctly-credentialed request rather than the exact shape CSRF protection exists to
    reject. Tests that specifically exercise CSRF rejection (`test_csrf.py`) build their own bare
    `TestClient` without this hook."""
    from opengrid.api.csrf import COOKIE_NAME, HEADER_NAME

    if request.method.upper() not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    cookie_header = request.headers.get("cookie", "")
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            request.headers[HEADER_NAME] = value
            return


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
    test_client = TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)
    test_client.event_hooks = {"request": [_echo_csrf_cookie_as_header], "response": []}
    return test_client


@pytest.fixture
def non_loopback_client(fake_store, fake_trace_store, fake_proposals, fake_config) -> TestClient:
    """Default `TestClient` host ("testclient") is deliberately *not* loopback -- used to prove
    `/og/api/health` rejects non-loopback callers regardless of headers (`opengrid.api.auth`)."""
    app = create_app()
    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[get_trace_store] = lambda: fake_trace_store
    app.dependency_overrides[get_proposals] = lambda: fake_proposals
    app.dependency_overrides[get_config] = lambda: fake_config
    return TestClient(app, headers=PROXY_HEADERS)
