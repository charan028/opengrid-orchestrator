"""TS-10-* -- base contract: nav lists all 7 screens, blocks render, static assets are served, role
gating hides write actions from a viewer. BUILD.md task brief: route tests with TestClient + fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    with (_FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def test_control_room_renders_with_nav_and_kpis(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api(
        {
            "/og/api/health": load_fixture("health.json"),
            "/og/api/fleet/hubs": load_fixture("hubs.json"),
        }
    )

    response = client.get("/og/")

    assert response.status_code == 200
    body = response.text
    for label, path in [
        ("Control room", "/og/"),
        ("Fleet", "/og/fleet"),
        ("Dispatch", "/og/dispatch"),
        ("Markets", "/og/markets"),
        ("Health", "/og/health"),
        ("Profitability", "/og/profitability"),
        ("Billing &amp; audit", "/og/billing"),
    ]:
        assert label in body
        assert path in body
    assert "Reserve breaches" in body
    assert "kWh sold twice" in body
    assert "Commitment switches" in body


def test_control_room_degrades_gracefully_when_api_unavailable(client: TestClient) -> None:
    # no fixtures registered -> api_client.get_json raises ApiUnavailable for every path
    response = client.get("/og/")

    assert response.status_code == 200
    assert "Degraded" in response.text


def test_fleet_screen_lists_hubs_and_hides_writes_from_viewer(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api({"/og/api/fleet/hubs": load_fixture("hubs.json")})

    response = client.get("/og/fleet")

    assert response.status_code == 200
    body = response.text
    assert "hub-0001" in body
    assert "hub-0002" in body
    # viewer role (default, no X-OG-Role header) must not see manual command or safe stop controls
    assert "Manual command" not in body
    assert "Scoped safe stop" not in body


def test_fleet_screen_shows_write_actions_for_operator(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api({"/og/api/fleet/hubs": load_fixture("hubs.json")})

    response = client.get("/og/fleet", headers={"X-OG-Role": "operator"})

    assert response.status_code == 200
    body = response.text
    assert "Manual command" in body
    assert "Scoped safe stop" in body
    assert "Propose safe stop (step 1 of 2)" in body
    assert 'hx-post="/og/fleet/safestop/propose"' in body


def test_fleet_hub_table_shows_a_per_row_age_column(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api({"/og/api/fleet/hubs": load_fixture("hubs.json")})

    response = client.get("/og/fleet")

    assert response.status_code == 200
    body = response.text
    assert "Age</th>" in body
    table_html = body.split("<table")[1].split("</table>")[0]
    # last_seen_at is set in the fixture -- the per-row age badge renders a real age, not "unknown"
    # (the screen's own "Data age: unknown" badge just above the table is a separate, pre-existing
    # element that `og.js` ticks live client-side from `data-since`, not part of the table itself).
    assert "age: unknown" not in table_html


def test_fleet_hub_drilldown_fragment(client: TestClient, fake_api: Callable[[dict[str, Any]], None]) -> None:
    fake_api({"/og/api/fleet/hubs/hub-0001": load_fixture("hub_detail.json")})

    response = client.get("/og/fleet/hubs/hub-0001")

    assert response.status_code == 200
    assert "hub-0001" in response.text
    assert "bank-01" in response.text


def test_health_screen_renders_process_grid_and_alerts(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api({"/og/api/health": load_fixture("health.json")})

    response = client.get("/og/health")

    assert response.status_code == 200
    body = response.text
    assert "engine" in body
    assert "guardian" in body
    assert "ERCOT price feed approaching staleness" in body


def test_static_assets_are_served() -> None:
    from fastapi import FastAPI

    from opengrid.ui import build_router

    app = FastAPI()
    app.include_router(build_router(), prefix="/og")
    client = TestClient(app)

    css = client.get("/og/static/og.css")
    js = client.get("/og/static/og.js")

    assert css.status_code == 200
    assert "--status-critical" in css.text
    assert js.status_code == 200
    assert "og.sse" in js.text
    assert "og.chart" in js.text
