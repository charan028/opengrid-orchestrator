"""Fixtures for opengrid.ui route/template tests. Builds a standalone FastAPI app from
`opengrid.ui.build_router()` (never `opengrid.api.create_app()`, which is still a stub owned by the api
agent) and monkeypatches the API HTTP boundary (`opengrid.ui.api_client.get_json`) with recorded JSON
fixtures, per BUILD.md's task brief for ui-a."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
import opengrid.ui.api_client as api_client
from opengrid.platform.config import Config

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# `opengrid.ui.role.role_of` maps the trusted `X-Remote-User` identity through `[api.roles]` config
# (`opengrid.api.auth.role_for_identity`), the same table the API itself uses -- these tests exercise UI
# route guards against named accounts, mirroring `tests/unit/api/test_safestop_release.py`'s ALICE/BOB.
# The literal identities "operator"/"viewer" also resolve via `role_for_identity`'s zero-config fallback
# (Apache's own accounts are named exactly that), so a bare `X-Remote-User: operator` works without being
# listed here too.
TEST_ROLES_CONFIG = Config({"api": {"roles": {"operator": ["alice", "bob"], "viewer": ["carol"]}}})


def load_fixture(name: str) -> Any:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(ui.build_router(), prefix="/og")
    application.state.config = TEST_ROLES_CONFIG
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], None]:
    """Install a fake `get_json` that serves fixture payloads keyed by request path."""

    def install(responses: dict[str, Any]) -> None:
        async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
            if path not in responses:
                raise api_client.ApiUnavailable(f"no fixture registered for {path}")
            return responses[path]

        monkeypatch.setattr(api_client, "get_json", fake_get_json)
        # route modules imported `get_json` by name, so patch their references too.
        import opengrid.ui.routes.control_room as control_room
        import opengrid.ui.routes.fleet as fleet
        import opengrid.ui.routes.health as health

        monkeypatch.setattr(control_room, "get_json", fake_get_json)
        monkeypatch.setattr(fleet, "get_json", fake_get_json)
        monkeypatch.setattr(health, "get_json", fake_get_json)

    return install


@pytest.fixture
def fake_post_api(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], None]:
    """Install a fake `post_json` that serves fixture payloads keyed by request path. A value that is
    itself an `api_client.ApiUnavailable` instance is raised instead of returned, so a single `install()`
    call can set up both the happy path and an error path (e.g. a 409 veto, a 503 timeout) per test.
    Every call is recorded on `install.posted` (path, payload, forwarded remote_user)."""
    posted: list[dict[str, Any]] = []

    def install(responses: dict[str, Any]) -> None:
        async def fake_post_json(
            path: str, payload: dict[str, Any], *, remote_user: str | None = None
        ) -> Any:
            posted.append({"path": path, "payload": payload, "remote_user": remote_user})
            if path not in responses:
                raise api_client.ApiUnavailable(f"no fixture registered for POST {path}")
            entry = responses[path]
            if isinstance(entry, api_client.ApiUnavailable):
                raise entry
            return entry

        monkeypatch.setattr(api_client, "post_json", fake_post_json)
        # route modules imported `post_json` by name, so patch their references too.
        import opengrid.ui.routes.control_room as control_room
        import opengrid.ui.routes.fleet as fleet
        import opengrid.ui.routes.health as health

        monkeypatch.setattr(control_room, "post_json", fake_post_json)
        monkeypatch.setattr(fleet, "post_json", fake_post_json)
        monkeypatch.setattr(health, "post_json", fake_post_json)
        try:
            import opengrid.ui.routes.billing_audit as billing_audit
        except ImportError:
            return
        monkeypatch.setattr(billing_audit, "post_json", fake_post_json)

    install.posted = posted  # type: ignore[attr-defined]
    return install
