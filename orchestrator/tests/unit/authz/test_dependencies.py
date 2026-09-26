"""`require_action` mounted on a real FastAPI route (not just called as a plain Python function): this
is what catches the class of bug where FastAPI can't resolve a dependency's parameter annotations (e.g.
because the names they reference are only importable inside an enclosing function, or only under
`TYPE_CHECKING`) and silently treats `identity` as an ordinary body field instead of running
`current_identity`/`require_action` at all -- every call then answers `422 Unprocessable Entity`
regardless of role. These tests assert the real outcomes (`200`/`403`/`401`), which a 422 regression
would fail immediately.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from opengrid.api.auth import PROXY_SECRET_ENV, Identity
from opengrid.api.deps import get_config, get_trace_store
from opengrid.authz import dependencies as authz_dependencies
from opengrid.authz.policy import PolicyEngine, Rule
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

PROXY_SECRET = "test-proxy-secret"  # noqa: S105 -- a test value, not a secret


class _FakeTraceBackend:
    """Just enough of `TraceBackend` (structural, not a subclass) for `TraceStore.append` to work."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        return -1, None

    async def insert_trace_row(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture(autouse=True)
def _proxy_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROXY_SECRET_ENV, PROXY_SECRET)


@pytest.fixture
def app_and_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _FakeTraceBackend]:
    engine = PolicyEngine([Rule(role="operator", action="widgets.poke", audited=True)])
    monkeypatch.setattr(authz_dependencies, "policy_engine_for", lambda cfg: engine)

    backend = _FakeTraceBackend()
    trace_store = TraceStore(backend)
    cfg = Config({"api": {"roles": {"operator": ["op1"], "viewer": ["view1"]}}})

    app = FastAPI()

    @app.post("/poke")
    async def poke(
        identity: Annotated[Identity, Depends(authz_dependencies.require_action("widgets.poke"))],
    ) -> dict[str, str]:
        return {"user": identity.user}

    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_trace_store] = lambda: trace_store
    return app, backend


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_operator_is_allowed_and_gets_200_not_422(
    app_and_backend: tuple[FastAPI, _FakeTraceBackend],
) -> None:
    app, _backend = app_and_backend
    resp = _client(app).post("/poke", headers={"X-Remote-User": "op1", "X-OG-Proxy-Auth": PROXY_SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"user": "op1"}


def test_viewer_is_denied_with_403_not_422(app_and_backend: tuple[FastAPI, _FakeTraceBackend]) -> None:
    app, backend = app_and_backend
    resp = _client(app).post("/poke", headers={"X-Remote-User": "view1", "X-OG-Proxy-Auth": PROXY_SECRET})
    assert resp.status_code == 403, resp.text
    # The action is `audited=True`, so the deny must have been written to the trace store.
    assert len(backend.rows) == 1
    assert backend.rows[0]["event_class"] == "TRACE_AUTHZ_DENY"


def test_missing_identity_header_is_401_not_422(
    app_and_backend: tuple[FastAPI, _FakeTraceBackend],
) -> None:
    app, _backend = app_and_backend
    resp = _client(app).post("/poke")
    assert resp.status_code == 401, resp.text
