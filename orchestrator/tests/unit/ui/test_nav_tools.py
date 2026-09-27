"""Console -> simulator control plane link (Scenarios): on every screen, outside the 7-screen list."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.health as health_route
from opengrid.ui.templating import NAV_SCREENS, NAV_TOOLS, SCENARIOS_PATH


def test_scenarios_is_a_tool_link_not_a_screen() -> None:
    assert SCENARIOS_PATH == "/ogsim/"
    assert [t["path"] for t in NAV_TOOLS] == ["/ogsim/"]
    assert all(s["path"] != SCENARIOS_PATH for s in NAV_SCREENS)
    assert len(NAV_SCREENS) == 9


def test_every_screen_links_to_scenarios(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        return {}

    monkeypatch.setattr(health_route, "get_json", fake_get_json)
    body = client.get("/og/health", headers={"X-Remote-User": "viewer"}).text
    assert '<ul class="og-nav-tools" aria-label="Tools">' in body
    assert 'href="/ogsim/"' in body and 'id="nav-scenarios"' in body
