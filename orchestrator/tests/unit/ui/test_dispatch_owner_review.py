"""Owner review of the live Dispatch page (R2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.dispatch as dispatch_route
from opengrid.ui.routes.dispatch import (
    HELD_SERIES,
    apply_ledger_counts,
    as_awards_view,
    ledger_api_view,
)

from .conftest import load_fixture

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
PRODUCTS = {"d03": "ECRS", "d09": "NSPIN"}


def _award(oid: str, state: str, contract: str = "d03") -> dict[str, Any]:
    return {
        "service_type": "ERCOT_AS",
        "obligation_id": oid,
        "contract_id": contract,
        "state": state,
        "committed_qty_kw": "500",
    }


def test_only_awarded_as_obligations_are_held_awards_with_a_product_cap() -> None:
    rows = as_awards_view(
        [_award("o1", "OFFERED"), _award("o2", "COMMITTED"), _award("o3", "DELIVERING", "d09")],
        [],
        now=NOW,
        product_by_contract=PRODUCTS,
    )
    assert [(r["obligation_id"], r["product"], r["max_minutes"]) for r in rows] == [
        ("o2", "ECRS", 60),
        ("o3", "NSPIN", 240),
    ]


@pytest.mark.parametrize(
    ("form", "text"),
    [
        (
            {"obligation_id": "", "duration_minutes": "15", "reason": "r", "product": "ECRS"},
            "Choose the held award",
        ),
        (
            {"obligation_id": "o2", "duration_minutes": "90", "reason": "r", "product": "ECRS"},
            "between 1 and 60 minutes for ECRS",
        ),
        (
            {"obligation_id": "o3", "duration_minutes": "241", "reason": "r", "product": "NSPIN"},
            "between 1 and 240 minutes",
        ),
    ],
)
def test_propose_refuses_all_scope_and_caps_duration(
    client: TestClient, form: dict[str, str], text: str
) -> None:
    html = client.post(
        "/og/dispatch/as-deployments/propose", data=form, headers={"X-Remote-User": "alice"}
    ).text
    assert text in html and "all held" not in html


def test_propose_names_the_award_and_product(client: TestClient) -> None:
    form = {
        "obligation_id": "o2-abcdefgh",
        "duration_minutes": "60",
        "reason": "ERCOT instruction",
        "product": "ECRS",
    }
    html = client.post(
        "/og/dispatch/as-deployments/propose", data=form, headers={"X-Remote-User": "alice"}
    ).text
    assert "Deploy award o2-abcde (ECRS) for 60 minutes" in html


def test_ledger_plots_held_capacity_and_uses_the_bucket_for_staleness() -> None:
    payload = {
        "bucket_minutes": 15,
        "hub_count": 2500,
        "available_hub_count": 2500,
        "now": {"capacity_kw": 32000.0, "reserved_kw": 2500.0, "committed_kw": 0.0},
        "timeline": [
            {
                "t": "2026-09-26T20:00:00+00:00",
                "capacity_kw": 32000.0,
                "reserved_kw": 2500.0,
                "committed_kw": 0.0,
                "uncommitted_capacity_kw": 29500.0,
                "over_committed_kw": 0.0,
                "committed_by_service": {},
            },
        ],
        "children": [],
    }
    view = ledger_api_view(payload, "fleet", now=NOW)
    assert view is not None
    held = next(s for s in view["chart_option"]["series"] if s["name"] == HELD_SERIES)
    assert held["data"] == [2500.0] and view["held_kw_now"] == 2500.0
    assert view["stale_after_s"] == 1800


def test_scope_counts_and_zones_come_from_the_ledger_response() -> None:
    payload = load_fixture("ui19_dispatch_ledger_fleet.json")
    payload["children"].append({"level": "zone", "id": "LZ_AEN", "hub_count": 500})
    payload["hub_count"] = 2500
    scope = {
        "level": "fleet",
        "id": "",
        "title": "fleet (40 feeder segments, 200 homes)",
        "zones": ["LZ_NORTH"],
        "banks": [],
    }
    apply_ledger_counts(scope, payload)
    assert scope["title"] == "fleet (5 zones, 2,500 homes)"
    assert scope["zones"] == ["LZ_HOUSTON", "LZ_NORTH", "LZ_SOUTH", "LZ_WEST", "LZ_AEN"]
    zone = {"level": "zone", "id": "LZ_AEN", "title": "x", "zones": [], "banks": []}
    apply_ledger_counts(zone, {"hub_count": 500, "children": [{"id": f"bank-{i:03d}"} for i in range(10)]})
    assert zone["title"] == "LZ_AEN (10 feeder segments, 500 homes)" and len(zone["banks"]) == 10


def test_dispatch_page_uses_the_ledger_counts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = load_fixture("ui19_dispatch_ledger_fleet.json")
    ledger["hub_count"] = 2500
    ledger["available_hub_count"] = 2500

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        return {
            "/og/api/dispatch/ledger": ledger,
            "/og/api/fleet/map": load_fixture("ui19_fleet_map.json"),
            "/og/api/dispatch/opportunities": [],
            "/og/api/contracts": [],
            "/og/api/dispatch/as-deployments": [],
        }.get(path, {})

    monkeypatch.setattr(dispatch_route, "get_json", fake_get_json)
    html = client.get("/og/dispatch", headers={"X-Remote-User": "viewer"}).text
    assert "fleet (4 zones, 2,500 homes)" in html and "200 homes" not in html
    assert "level=zone&amp;id=LZ_WEST" in html
