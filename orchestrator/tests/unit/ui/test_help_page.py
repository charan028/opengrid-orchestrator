"""The Help page (`/og/help`, r3.4) and the Help entry at the bottom of the left menu on every screen."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.api_client as api_client
from opengrid.api.app import create_app
from opengrid.ui.routes import help as help_screen
from opengrid.ui.templating import NAV_SCREENS, TEMPLATES_DIR, help_href

VIEWER = {"X-Remote-User": "viewer"}
_ID = re.compile(r'\bid="([^"]+)"')


def test_help_href_deep_links_each_screen_to_its_section() -> None:
    assert help_href("/og/") == "/og/help#page-control-room"
    assert help_href("/og/fleet") == "/og/help#page-fleet"
    assert help_href("/og/fleet/hubs/hub-0001") == "/og/help#page-fleet"
    assert help_href("/og/billing") == "/og/help#page-billing"
    assert help_href("/og/pq") == "/og/help#page-pq"
    assert help_href("/og/help") == "/og/help#page-help"
    assert help_href("/og/unknown") == "/og/help"


def test_viewer_can_read_the_help_page_and_every_toc_anchor_exists(client: TestClient) -> None:
    resp = client.get("/og/help", headers=VIEWER)
    assert resp.status_code == 200
    body = resp.text
    ids = _ID.findall(body)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    for sec_id, _title, subs in help_screen.TOC:
        assert f'id="{sec_id}"' in body, sec_id
        for sub_id, _sub_title in subs:
            assert f'id="{sub_id}"' in body, sub_id
    assert 'id="hp-search-input"' in body and 'type="search"' in body
    assert "/static/og-help.js" in body and "/static/og-help.css" in body
    assert 'href="/og/api/openapi.json"' in body
    assert 'aria-current="page"' in body.split('id="nav-help"', 1)[1].split("</a>", 1)[0]


def test_every_screen_and_playbook_section_is_documented(client: TestClient) -> None:
    body = client.get("/og/help", headers=VIEWER).text
    for section in help_screen.TOC[4][2]:
        assert f'id="{section[0]}"' in body
    for pb in ("pb-safe-stop", "pb-manual-target", "pb-price-spike", "pb-rebuild", "pb-customer-onboarding"):
        assert f'id="{pb}"' in body, pb


def test_help_fragments_are_plain_html() -> None:
    """The prose fragments are static: no template logic, scripts, inline styles or colour literals."""
    fragments = sorted((TEMPLATES_DIR / "help").glob("*.html"))
    assert fragments
    for fragment in fragments:
        text = fragment.read_text(encoding="utf-8")
        for forbidden in ("{{", "{%", "{#", "<script", "<style", ' style="'):
            assert forbidden not in text, (fragment.name, forbidden)
        assert not re.search(r'(fill|stroke)="#', text), fragment.name


@pytest.fixture
def api_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every screen's data source unreachable: each screen renders its degraded state."""

    async def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise api_client.ApiUnavailable("API unreachable")

    import opengrid.ui.routes as routes_pkg

    for name in (
        "alerts",
        "billing_audit",
        "control_room",
        "dispatch",
        "fleet",
        "health",
        "markets",
        "pq",
        "profitability",
    ):
        module = getattr(routes_pkg, name, None) or __import__(f"opengrid.ui.routes.{name}", fromlist=["x"])
        for attr in ("get_json", "post_json"):
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, unavailable)


@pytest.mark.usefixtures("api_down")
@pytest.mark.parametrize("path", [s["path"] for s in NAV_SCREENS])
def test_every_screen_ends_its_menu_with_the_deep_linked_help_entry(client: TestClient, path: str) -> None:
    resp = client.get(path, headers=VIEWER)
    assert resp.status_code == 200
    body = resp.text
    link = re.search(r'<a href="([^"]+)" id="nav-help" aria-label="Help"', body)
    assert link is not None, path
    assert link.group(1) == help_href(path)
    # the last menu entry: after the screens and tools, before the role footer
    assert body.index('id="nav-scenarios"') < body.index('id="nav-help"') < body.index('class="og-nav-foot"')


def test_api_reference_lists_every_api_route_with_its_role() -> None:
    app = create_app()
    groups = help_screen.api_reference(app)
    rows = {(ep["path"], ep["methods"]): ep for g in groups for ep in g["endpoints"]}
    published = {p for p in app.openapi()["paths"] if p.startswith("/og/api/")}
    assert {path for path, _m in rows} == published
    assert rows[("/og/api/safestop", "POST")]["role"] == "operator"
    assert rows[("/og/api/fleet/hubs", "GET")]["role"] == "viewer"
    # roles come from the routes' own dependencies, so none is left undetermined for an operator write
    assert all(
        ep["role"] != "none declared" for (path, m), ep in rows.items() if m == "POST" and "safestop" in path
    )


def test_the_production_app_renders_the_generated_api_reference() -> None:
    client = TestClient(create_app())
    body = client.get("/og/help", headers=VIEWER).text
    assert "/og/api/safestop" in body
    assert 'id="api-console"' in body
