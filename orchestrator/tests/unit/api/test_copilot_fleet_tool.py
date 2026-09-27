"""The copilot's read-only fleet tool (`opengrid.api.routers.ai.run_fleet_query`) over the Fleet table's
own filter and aggregate layer (`fleet_search.summary_query` / `fleet_summary`), against a recording fake.

Pinned here: each of the owner's example questions becomes bound parameters on the one filter layer (no
second SQL), aggregates are exact and grouped, the tool refuses anything but a single SELECT, truck
positions never leave the tool, and `POST /og/api/ai/ask` wires the tool in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opengrid import ai_agent
from opengrid.ai_agent import CopilotService, FleetQuery
from opengrid.api.routers import ai as ai_routes
from opengrid.api.routers import fleet_search
from opengrid.api.routers.fleet_search import HubFilter, Thresholds, summary_query, unit_at_home, where_clause
from opengrid.health.model import HealthThresholds

from .conftest import VIEWER_HEADERS
from .fakes import FakeStore

TH = Thresholds(HealthThresholds(hub_stale_s=4.0, hub_offline_s=30.0), mobile=("bank-truck-aus-01",))


class RecordingRows:
    """`FleetRowsStore` fake: records each statement; answers aggregates and pages from queues."""

    def __init__(self, totals: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.totals = totals or [{"grp": "all", "hubs": 0}]
        self.pages: list[list[dict[str, Any]]] = []

    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if "GROUP BY 1" in sql:
            return self.totals
        return self.pages.pop(0) if self.pages else []


async def _run(query: FleetQuery, rows: RecordingRows | None = None) -> tuple[dict[str, Any], RecordingRows]:
    rows = rows or RecordingRows()
    result = await ai_routes.run_fleet_query(ai_routes.SelectOnlyRows(rows), TH, query)
    return result, rows


def _every_placeholder_bound(rows: RecordingRows) -> None:
    for sql, params in rows.calls:
        assert sql.count("%s") == len(params), sql


# -- the owner's examples, as bound filters -------------------------------------------------------------


async def test_capacity_78_4_kwh_is_an_exact_rating_with_tolerance() -> None:
    rows = RecordingRows([{"grp": "all", "hubs": 700, "rated_kwh": 54880.0}])
    result, rows = await _run(FleetQuery(capacity_min_kwh=78.4, capacity_max_kwh=78.4), rows)

    sql, params = rows.calls[0]
    assert "h.e_kwh >= %s" in sql and "h.e_kwh <= %s" in sql
    assert pytest.approx(78.35) in params and pytest.approx(78.45) in params
    assert result["total"]["hubs"] == 700
    _every_placeholder_bound(rows)


async def test_below_30_percent_in_lz_north_sorts_lowest_charge_first() -> None:
    _result, rows = await _run(FleetQuery(zones=("LZ_NORTH",), soc_max_pct=30.0))

    total_sql, total_params = rows.calls[0]
    assert "h.zone = ANY(%s)" in total_sql and ["LZ_NORTH"] in total_params
    assert "s.soc_kwh::float8 * 100 <= %s" in total_sql and 30.0 in total_params
    page_sql, _ = rows.calls[1]
    assert "ORDER BY sort_value ASC" in page_sql and "s.soc_kwh::float8 / nullif" in page_sql
    _every_placeholder_bound(rows)


async def test_trucks_at_home_uses_the_one_d31_rule_and_returns_no_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fleet_search, "home_station_sites", lambda: {"bank-truck-aus-01": (30.5195, -97.648)})
    rows = RecordingRows([{"grp": "all", "hubs": 1}])
    rows.pages = [
        [],  # the top-rows page
        [{"hub_id": "truck-aus-01", "bank_id": "bank-truck-aus-01"}],  # the mobile units matching
        # their DEVICE-REPORTED positions (core.geo.DEVICE_POSITIONS_SQL), never the seeded og.hub.lat/lon
        [
            {
                "hub_id": "truck-aus-01",
                "bank_id": "bank-truck-aus-01",
                "device_lat": 30.5196,
                "device_lon": -97.6481,
                "device_info_at": datetime.now(UTC),
                "last_seen_at": datetime.now(UTC),
            }
        ],
    ]

    result, rows = await _run(FleetQuery(asset_class="truck", at_home=True), rows)

    assert result["mobile"] == {
        "units": 1,
        "at_home": 1,
        "away": 0,
        "unknown": 0,
        "at_home_ids": ["truck-aus-01"],
        "away_ids": [],
    }
    assert "30.5196" not in repr(result) and "lat" not in repr(result)
    assert ["MOBILE"] in rows.calls[0][1]
    _every_placeholder_bound(rows)


async def test_total_available_kw_counts_dispatchable_hubs_only() -> None:
    rows = RecordingRows(
        [{"grp": "all", "hubs": 502, "available_hubs": 480, "available_kw": 5120.0, "rated_kw": 5720.0}]
    )
    result, rows = await _run(FleetQuery(zones=("LZ_AEN",), metric="available_kw"), rows)

    sql, _ = rows.calls[0]
    assert "FILTER (WHERE (health IN ('online', 'stale') AND availability = 'AVAILABLE'))" in sql
    assert result["total"]["available_kw"] == 5120.0 and result["total"]["available_hubs"] == 480


# -- the filter / aggregate layer ---------------------------------------------------------------------


def test_new_filters_are_bound_parameters() -> None:
    w = where_clause(HubFilter(p_kw_min=15.0, units=(2,), availability=("UNAVAILABLE",), e_kwh_max=40.0), TH)
    assert "h.p_kw >= %s" in w.text and "h.e_kwh <= %s" in w.text
    assert "(to_jsonb(h.*) ->> 'units')" in w.text and "to_jsonb(b.*) ->> 'availability'" in w.text
    assert w.params == [40.0, 15.0, [2], ["UNAVAILABLE"]]
    assert not HubFilter(units=(2,)).empty


@pytest.mark.parametrize("group_by", fleet_search.SUMMARY_GROUPS)
def test_summary_query_groups_and_binds_everything(group_by: str) -> None:
    q = summary_query(HubFilter(zones=("LZ_AEN",), health=("offline",)), TH, group_by=group_by, limit=50)
    assert q.text.startswith("SELECT ") and "GROUP BY 1 ORDER BY 1 LIMIT %s" in q.text
    assert q.text.count("%s") == len(q.params)
    assert ";" not in q.text


async def test_a_breakdown_is_returned_with_the_total() -> None:
    rows = RecordingRows([{"grp": "LZ_AEN", "hubs": 502}, {"grp": "LZ_NORTH", "hubs": 504}])
    result, _ = await _run(FleetQuery(group_by="zone"), rows)
    assert [g["key"] for g in result["groups"]] == ["LZ_AEN", "LZ_NORTH"]


def test_dual_unit_homes_are_home_hubs_with_two_units() -> None:
    flt = ai_routes.hub_filter(FleetQuery(asset_class="dual_unit"))
    assert flt.asset_class == ("HOME",) and flt.units == (2,)


def test_unit_at_home_is_the_geo_rule() -> None:
    sites = {"bank-t": (30.0, -97.0)}
    assert unit_at_home("t", "bank-t", (30.001, -97.001), sites) is True
    assert unit_at_home("t", "bank-t", (30.1, -97.0), sites) is False
    assert unit_at_home("t", "bank-t", None, sites) is None


async def test_the_tool_refuses_anything_but_one_select() -> None:
    guarded = ai_routes.SelectOnlyRows(RecordingRows())
    for sql in ("UPDATE og.hub SET p_kw = 0", "SELECT 1; DELETE FROM og.hub", "  delete from og.hub"):
        with pytest.raises(PermissionError):
            await guarded.fleet_rows(sql, ())


# -- wiring -------------------------------------------------------------------------------------------


class FleetFakeStore(FakeStore):
    """The API fake store plus the fleet read helper."""

    def __init__(self, rows: RecordingRows) -> None:
        super().__init__()
        self._rows = rows

    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return await self._rows.fleet_rows(sql, params)


def test_summary_endpoint(client: TestClient) -> None:
    from opengrid.api.deps import get_store

    rows = RecordingRows([{"grp": "all", "hubs": 700}])
    client.app.dependency_overrides[get_store] = lambda: FleetFakeStore(rows)  # type: ignore[attr-defined]
    body = client.get(
        "/og/api/fleet/summary?e_kwh_min=78.35&e_kwh_max=78.45&top=0", headers=VIEWER_HEADERS
    ).json()
    assert body["total"]["hubs"] == 700 and body["rows"] == []
    bad = client.get("/og/api/fleet/summary?availability=MAYBE", headers=VIEWER_HEADERS)
    assert bad.status_code == 422


def test_ask_wires_the_fleet_tool(client: TestClient) -> None:
    from opengrid.api.deps import get_store

    rows = RecordingRows([{"grp": "all", "hubs": 700}])
    client.app.dependency_overrides[get_store] = lambda: FleetFakeStore(rows)  # type: ignore[attr-defined]
    ai_agent.set_service(CopilotService())  # no model: the fleet answer is pure tier 1
    try:
        client.get("/og/api/ai/status", headers=VIEWER_HEADERS)  # sets the CSRF cookie
        response = client.post(
            "/og/api/ai/ask",
            json={"question": "how many units have capacity 78.4 kWh"},
            headers=VIEWER_HEADERS,
        )
    finally:
        ai_agent.set_service(None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "700 hubs rated 78.4 kWh."
    assert body["citations"][0]["source"] == "/og/api/fleet/summary"
    assert rows.calls, "the tool ran through the fleet read helper"


async def test_a_filter_lost_before_sql_is_refused_not_answered_unfiltered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces for "never the unfiltered total when the question names a filter"."""
    monkeypatch.setattr(ai_routes, "hub_filter", lambda _q: HubFilter())
    with pytest.raises(ValueError, match="filter was lost"):
        await _run(FleetQuery(zones=("LZ_NORTH",)))


def test_at_home_alone_implies_the_truck_class() -> None:
    assert ai_routes.hub_filter(FleetQuery(at_home=True)).asset_class == ("MOBILE",)
