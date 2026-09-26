"""#23's screens parse the UI-API's (#19) exact response shapes (the api's own ui19 fixtures)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.control_room as control_room_route
import opengrid.ui.routes.fleet as fleet_route
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.dispatch import UNCOMMITTED_SERIES, ledger_api_view
from opengrid.ui.routes.fleet import api_double_confirm_reasons, bulk_risk_reasons, map_hubs_from
from opengrid.ui.routes.markets import bid_funnel_view

from .conftest import load_fixture

NOW = datetime(2026, 9, 26, 19, 40, tzinfo=UTC)


def test_fleet_map_hubs_are_read_from_hubs() -> None:
    hubs = map_hubs_from(load_fixture("ui19_fleet_map.json"))
    assert [h["hub_id"] for h in hubs][:2] == ["hub-00000", "hub-00001"]
    first = hubs[0]
    assert first["activity"] == "DELIVERING" and first["lat"] == pytest.approx(32.682638)
    assert first["serving_obligations"][0]["service_type"] == "ERCOT_ENERGY"
    assert map_hubs_from({"unexpected": 1}) == []


def test_bulk_reasons_come_from_the_api_and_the_console_only_adds() -> None:
    proposal = load_fixture("ui19_bulk_propose.json")
    assert api_double_confirm_reasons(proposal) == [
        "1 hub in a fault state",
        "1 hub serving a committed obligation (ERCOT_ENERGY)",
    ]
    # the console's own reading of a hub list without obligations flags nothing -- the API still does
    assert bulk_risk_reasons([{"hub_id": "hub-00000", "health": "online", "p_kw": 0}], ["hub-00000"]) == []


def _api(monkeypatch: pytest.MonkeyPatch, gets: dict[str, Any], posts: dict[str, list[Any]]) -> None:
    calls: dict[str, int] = {}

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path in gets:
            return gets[path]
        raise ApiUnavailable(f"no fixture for {path}", status_code=404)

    async def fake_post_json(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        n = calls.setdefault(path, 0)
        calls[path] = n + 1
        return posts[path][min(n, len(posts[path]) - 1)]

    for module in (fleet_route, control_room_route):
        monkeypatch.setattr(module, "get_json", fake_get_json)
    monkeypatch.setattr(fleet_route, "post_json", fake_post_json)


def test_bulk_flow_needs_the_apis_second_confirm(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = load_fixture("ui19_bulk_propose.json")
    pid = proposal["proposal_id"]
    confirm_path = f"/og/api/fleet/commands/bulk/{pid}/confirm"
    _api(
        monkeypatch,
        {"/og/api/fleet/hubs": {"items": []}},
        {
            "/og/api/fleet/commands/bulk": [proposal],
            confirm_path: [
                load_fixture("ui19_bulk_confirm_1.json"),
                load_fixture("ui19_bulk_confirm_2.json"),
            ],
        },
    )
    op = {"X-Remote-User": "alice"}
    dialog = client.post(
        "/og/fleet/command/bulk/propose",
        data={"hub_ids": "hub-00000,hub-00003,hub-00005", "p_kw_setpoint": "0", "reason": "feeder work"},
        headers=op,
    ).text
    assert "confirm-dialog-ack" in dialog and "serving a committed obligation (ERCOT_ENERGY)" in dialog
    first = client.post(f"/og/fleet/command/bulk/{pid}/confirm", headers=op).text
    assert (
        "SECOND CONFIRM" in first
        and 'id="bulk-second-confirm"' in first
        and "1 hub in a fault state" in first
    )
    second = client.post(f"/og/fleet/command/bulk/{pid}/confirm", headers=op).text
    assert "3 of 3 hubs passed the guardian" in second and "PASS" in second


def test_executed_with_vetoes_lists_the_refused_hubs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed = load_fixture("ui19_bulk_confirm_2.json")
    executed["outcome_counts"] = {"PASS": 2, "VETOED": 1}
    executed["results"][1] = {**executed["results"][1], "outcome": "VETOED", "vetoed_rule_ids": ["G-02"]}
    _api(monkeypatch, {}, {"/og/api/fleet/commands/bulk/x/confirm": [executed]})
    html = client.post("/og/fleet/command/bulk/x/confirm", headers={"X-Remote-User": "alice"}).text
    assert "PARTIAL" in html and "2 of 3 hubs passed" in html and "hub-00003: VETOED (G-02)" in html


def test_control_room_map_uses_fleet_map_and_customer_sites(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api(
        monkeypatch,
        {
            "/og/api/health": {},
            "/og/api/fleet/hubs": {"items": []},
            "/og/api/fleet/map": load_fixture("ui19_fleet_map.json"),
            "/og/api/customers/map": load_fixture("ui19_customers_map.json"),
        },
        {},
    )
    html = client.get("/og/", headers={"X-Remote-User": "viewer"}).text
    assert "hub-00005" in html and "Data center (demo)" in html
    assert "/og/api/grid/layers?min_kv=200" in html


def test_bid_funnel_parses_the_api_shape() -> None:
    view = bid_funnel_view(load_fixture("ui19_markets_bid_funnel.json"), now=NOW)
    assert view["has_data"] and [r["product"] for r in view["rows"]] == ["DATA_CENTER", "ECRS", "ENERGY"]
    assert view["totals"]["available"] == 6 and view["totals"]["awarded"] == 3
    assert [(r["reason"], r["count"]) for r in view["reasons"]] == [
        ("R-GATE-REJECT", 2),
        ("R-ADMIT-REJECT", 1),
    ]


@pytest.mark.parametrize("name", ["ui19_dispatch_ledger_fleet.json", "ui19_dispatch_ledger_bank.json"])
def test_ledger_api_view_stacks_services_and_uncommitted(name: str) -> None:
    payload = load_fixture(name)
    view = ledger_api_view(payload, "scope", now=NOW)
    assert view is not None
    names = [s["name"] for s in view["chart_option"]["series"]]
    assert UNCOMMITTED_SERIES in names and "ERCOT_ENERGY" in names
    assert len(view["chart_option"]["xAxis"]["data"]) == len(payload["timeline"])
    assert view["capacity_kw"] == pytest.approx(float(payload["now"]["capacity_kw"]))
    assert ledger_api_view({"reservations": []}, "x", now=NOW) is None
