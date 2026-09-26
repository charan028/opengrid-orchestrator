"""Gitea #19: `GET /og/api/fleet/map`, `/og/api/customers/map`, `/og/api/grid/layers` -- shapes, the
activity rule, coordinates, eligibility, caching, authz, and the 2,000-hub performance budget."""

from __future__ import annotations

import math
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opengrid.api.views_ext import (
    ZoneCentroid,
    alert_scopes,
    derive_activity,
    derived_coordinates,
)
from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS
from .ui19_fakes import FakeExtViews, FleetFakeStore, build_app, fleet_rows, hub_row, make_client

CUSTOMER_HEADERS = {"X-Remote-User": "cust-user"}


@pytest.fixture
def ui19_config() -> Config:
    return Config(
        {
            "api": {
                "sse_heartbeat_s": 15,
                "roles": {
                    "operator": [],
                    "viewer": [],
                    "customer": {"cust-user": "00000000-0000-7000-8000-0000000000c6"},
                },
            }
        }
    )


@pytest.fixture
def views() -> FakeExtViews:
    return FakeExtViews()


@pytest.fixture
def ui19_client(views, fake_trace_store, fake_proposals, ui19_config) -> TestClient:
    app = build_app(views, FleetFakeStore(views.rows), fake_trace_store, fake_proposals, ui19_config)
    return make_client(app, PROXY_HEADERS)


# -- pure rules ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("health", "kw", "grant", "expected"),
    [
        ("fault", 5.0, True, "FAULT"),
        (None, None, False, "OFFLINE"),
        ("stale", 5.0, True, "OFFLINE"),
        ("offline", 0.0, False, "OFFLINE"),
        ("online", -3.0, True, "CHARGING"),
        ("online", 3.0, True, "DELIVERING"),
        ("online", 3.0, False, "HOME_USE"),
        ("online", 0.2, True, "IDLE"),
        ("online", -0.2, False, "IDLE"),
    ],
)
def test_derive_activity(health, kw, grant, expected) -> None:
    assert derive_activity(health=health, kw=kw, bank_has_grant=grant, epsilon_kw=0.5) == expected


def test_derived_coordinates_are_deterministic_and_inside_the_zone_disc() -> None:
    centroid = ZoneCentroid(lat=29.76, lon=-95.37, radius_km=35)
    points = [derived_coordinates(f"hub-{i:05d}", centroid) for i in range(500)]
    assert points == [derived_coordinates(f"hub-{i:05d}", centroid) for i in range(500)]
    assert len(set(points)) == 500
    for lat, lon in points:
        dy = (lat - centroid.lat) * 111.32
        dx = (lon - centroid.lon) * 111.32 * math.cos(math.radians(centroid.lat))
        assert math.hypot(dx, dy) <= 35.01


def test_alert_scopes_reads_structured_columns_and_detail_keys() -> None:
    rows = [
        {"scope_kind": "BANK", "scope_ref": "bank-001", "detail": None},
        {"scope_kind": None, "scope_ref": None, "detail": {"hub_id": "hub-00007"}},
        {"scope_kind": None, "scope_ref": None, "detail": '{"scope_kind": "ZONE", "scope_ref": "LZ_WEST"}'},
    ]
    assert alert_scopes(rows) == {("bank", "bank-001"), ("hub", "hub-00007"), ("zone", "LZ_WEST")}


# -- fleet map ---------------------------------------------------------------------------------------


def test_fleet_map_shape_and_activity_counts(ui19_client) -> None:
    resp = ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2000
    assert body["activity_counts"] == {
        "DELIVERING": 50,
        "IDLE": 1795,
        "HOME_USE": 50,
        "CHARGING": 50,
        "FAULT": 5,
        "OFFLINE": 50,
    }
    hub = body["hubs"][0]
    assert set(hub) >= {
        "hub_id",
        "lat",
        "lon",
        "health",
        "activity",
        "kw",
        "soc_pct",
        "bank_id",
        "zone",
        "serving_obligations",
        "can_serve_services",
    }
    assert hub["activity"] == "DELIVERING"
    assert hub["serving_obligations"][0]["state"] == "DELIVERING"
    assert hub["soc_pct"] == pytest.approx(51.0, abs=0.1)
    assert hub["coord_source"] == "zone_centroid"
    assert body["coord_sources"] == {"zone_centroid": 2000}


def test_fleet_map_uses_hub_coordinates_when_present(views, ui19_client) -> None:
    views.rows = [hub_row(0, lat=30.1, lon=-97.2), hub_row(1)]
    hubs = ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS).json()["hubs"]
    assert (hubs[0]["lat"], hubs[0]["lon"], hubs[0]["coord_source"]) == (30.1, -97.2, "hub")
    assert hubs[1]["coord_source"] == "zone_centroid"


def test_fleet_map_can_serve_follows_territory(views, ui19_client) -> None:
    aen = {**hub_row(0), "zone": "LZ_AEN", "bank_id": "bank-aen"}
    faulted = {**hub_row(1), "health": "fault"}
    views.rows = [aen, hub_row(2), faulted]
    hubs = {
        h["hub_id"]: h for h in ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS).json()["hubs"]
    }
    assert hubs["hub-00000"]["can_serve_services"] == ["REGULATED_CAPACITY"]
    assert hubs["hub-00002"]["can_serve_services"] == ["ERCOT_ENERGY"]
    assert hubs["hub-00001"]["can_serve_services"] == []


def test_fleet_map_filters(ui19_client) -> None:
    body = ui19_client.get("/og/api/fleet/map?activity=CHARGING", headers=VIEWER_HEADERS).json()
    assert body["count"] == 50 and {h["activity"] for h in body["hubs"]} == {"CHARGING"}
    body = ui19_client.get("/og/api/fleet/map?bank=bank-000&zone=LZ_NORTH", headers=VIEWER_HEADERS).json()
    assert body["count"] == 50
    assert ui19_client.get("/og/api/fleet/map?activity=NAPPING", headers=VIEWER_HEADERS).status_code == 422


def test_fleet_map_is_cached_between_calls(views, ui19_client) -> None:
    for _ in range(3):
        assert ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS).status_code == 200
    assert views.calls["fleet_map_rows"] == 1


def test_fleet_map_2000_hubs_under_300_ms(ui19_client) -> None:
    # Warm the test harness itself (anyio portal, authz policy load) on another route, so the timed
    # calls measure the endpoint: the first one builds the snapshot (cache cold), the rest hit the cache.
    assert ui19_client.get("/og/api/customers/map", headers=VIEWER_HEADERS).status_code == 200
    started = time.perf_counter()
    cold = ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS)
    cold_ms = (time.perf_counter() - started) * 1000
    assert cold.status_code == 200 and cold.json()["count"] == 2000
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        resp = ui19_client.get("/og/api/fleet/map", headers=VIEWER_HEADERS)
        timings.append((time.perf_counter() - started) * 1000)
        assert resp.status_code == 200
    assert cold_ms < 300, f"cold fleet map took {cold_ms:.0f} ms"
    assert max(timings) < 300, f"cached fleet map took up to {max(timings):.0f} ms"


# -- customers map -----------------------------------------------------------------------------------


def test_customers_map_joins_config_readings_and_contracts(ui19_client) -> None:
    body = ui19_client.get("/og/api/customers/map", headers=VIEWER_HEADERS).json()
    sites = {s["site_id"]: s for s in body["sites"]}
    dc = sites["site-dc-01"]
    assert dc["service_type"] == "DATA_CENTER"
    assert dc["consuming"] is True and dc["kw"] == 2400.0
    assert dc["lat"] is not None and dc["coord_source"] == "placeholder"
    assert sites["site-warehouse-01"]["kw"] is None and sites["site-warehouse-01"]["consuming"] is False
    unknown = sites["site-unknown"]
    assert unknown["lat"] is None and unknown["coord_source"] == "unlocated" and unknown["consuming"] is False
    assert body["consuming_count"] == 1


# -- grid layers -------------------------------------------------------------------------------------


def test_grid_layers_all_layers(ui19_client) -> None:
    resp = ui19_client.get("/og/api/grid/layers", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["layers"] == [
        "zones",
        "weather_zones",
        "utility_batteries",
        "transmission_lines",
        "grid_connection_points",
        "heat_cells",
    ]
    assert body["source"]["sha256"] == "eba8e48c85767a29bd5c046c181a2592d53aebecf4cb4e0efb93ece7c559b94d"
    zones = {z["zone"]: z for z in body["zones"]}
    assert zones["LZ_HOUSTON"]["load_mw"] == 18000.0 and zones["LZ_HOUSTON"]["live"] is True
    assert zones["LZ_NORTH"]["load_mw"] == 20500.0
    assert zones["LZ_WEST"]["live"] is False and zones["LZ_WEST"]["load_mw"] > 0
    assert len(body["utility_batteries"]) == 134
    assert sum(body["transmission_line_counts"].values()) == 7089
    assert set(body["transmission_lines_by_kv"]) == set(body["transmission_line_counts"])
    assert {p["kind"] for p in body["grid_connection_points"]} >= {"grid_entry_point", "corridor_chokepoint"}
    cells = {(c["level"], c["id"]): c for c in body["heat_cells"]}
    bank0 = cells[("bank", "bank-000")]
    assert (bank0["demand_kw"], bank0["served_kw"], bank0["unserved_kw"]) == (400.0, 55.0, 345.0)
    assert cells[("bank", "bank-001")]["demand_kw"] is None
    north = cells[("zone", "LZ_NORTH")]
    assert north["demand_kw"] == 20_500_000.0 and north["served_kw"] == 55.0


def test_grid_layers_subset_and_kv_filter(ui19_client) -> None:
    body = ui19_client.get(
        "/og/api/grid/layers?layers=transmission_lines&min_kv=345", headers=VIEWER_HEADERS
    ).json()
    assert body["layers"] == ["transmission_lines"]
    assert set(body["transmission_line_counts"]) == {"345", "500"}
    assert "zones" not in body and "heat_cells" not in body
    first = body["transmission_lines_by_kv"]["500"][0]
    assert set(first) == {"path", "owner", "status"} and len(first["path"][0]) == 2


def test_grid_layers_unknown_layer_is_422(ui19_client) -> None:
    assert (
        ui19_client.get("/og/api/grid/layers?layers=zones,pipes", headers=VIEWER_HEADERS).status_code == 422
    )


def test_grid_layers_missing_data_file_is_503(views, fake_trace_store, fake_proposals, tmp_path) -> None:
    cfg = Config({"api": {"roles": {"operator": [], "viewer": []}, "grid": {"config_dir": str(tmp_path)}}})
    app = build_app(views, FleetFakeStore(views.rows), fake_trace_store, fake_proposals, cfg)
    client = make_client(app, PROXY_HEADERS)
    assert client.get("/og/api/grid/layers", headers=VIEWER_HEADERS).status_code == 503


# -- authz -------------------------------------------------------------------------------------------

READ_PATHS = ("/og/api/fleet/map", "/og/api/customers/map", "/og/api/grid/layers?layers=zones")


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_need_an_identity(ui19_client, path) -> None:
    assert ui19_client.get(path).status_code == 401


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_refuse_a_customer(ui19_client, path) -> None:
    assert ui19_client.get(path, headers=CUSTOMER_HEADERS).status_code == 403


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_refuse_an_unproxied_identity(
    views, fake_trace_store, fake_proposals, ui19_config, path
) -> None:
    app = build_app(views, FleetFakeStore(views.rows), fake_trace_store, fake_proposals, ui19_config)
    client = make_client(app, {})
    assert client.get(path, headers=OPERATOR_HEADERS).status_code == 401


@pytest.mark.parametrize("headers", [VIEWER_HEADERS, OPERATOR_HEADERS])
def test_reads_allow_viewer_and_operator(ui19_client, headers: dict[str, Any]) -> None:
    for path in READ_PATHS:
        assert ui19_client.get(path, headers=headers).status_code == 200


def test_fleet_rows_fixture_is_2000_hubs() -> None:
    assert len(fleet_rows()) == 2000
