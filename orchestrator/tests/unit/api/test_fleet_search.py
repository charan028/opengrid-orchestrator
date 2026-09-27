"""`opengrid.api.routers.fleet_search` (owner review R3): keyset paging, filters, typeahead, select-all
matching, pending releases and the hub detail aggregate -- against a recording fake (no database).

Query-plan note: the SQL is asserted structurally here (keyset row-value comparison, no OFFSET, bound
parameters, estimate via pg_class/EXPLAIN, prefix ILIKE); its plans against a production-sized table are
documented in the router's module docstring and need the indexes listed there."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opengrid.api.proposals import ProposalStore
from opengrid.api.routers import fleet_search
from opengrid.api.routers.fleet_search import (
    HubFilter,
    Thresholds,
    decode_cursor,
    encode_cursor,
    page_query,
    search_query,
    shape_hub,
    where_clause,
)
from opengrid.health.model import HealthThresholds

from .conftest import VIEWER_HEADERS

TH = Thresholds(HealthThresholds(hub_stale_s=4.0, hub_offline_s=30.0))
NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


class RecordingStore:
    """`FleetRowsStore` fake: records every statement and answers from a queue (or a default)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.answers: list[list[dict[str, Any]]] = []

    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if sql.startswith("EXPLAIN"):
            return [{"QUERY PLAN": [{"Plan": {"Plan Rows": 2500}}]}]
        if "pg_class" in sql:
            return [{"n": 2500}]
        return self.answers.pop(0) if self.answers else []


def _row(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "hub_id": f"hub-{i:04d}",
        "bank_id": "bank-01",
        "zone": "LZ_SOUTH",
        "e_kwh": 39.2,
        "r_kwh": 7.84,
        "soc_kwh": 19.6,
        "p_kw": 1.0,
        "stored_health": "offline",  # a stale cached column must not win over last_seen_at
        "fault_code": None,
        "last_seen_at": datetime.now(UTC) - timedelta(seconds=1),
        "activity": "serving_home",
        "sort_value": f"hub-{i:04d}",
        **extra,
    }


@pytest.fixture
def rows_store() -> RecordingStore:
    return RecordingStore()


@pytest.fixture
def search_client(client: TestClient, rows_store: RecordingStore) -> TestClient:
    from opengrid.api.deps import get_store

    client.app.include_router(fleet_search.router)  # type: ignore[attr-defined]
    client.app.dependency_overrides[get_store] = lambda: rows_store  # type: ignore[attr-defined]
    return client


# -- SQL builders -------------------------------------------------------------------------------------


def test_page_query_is_keyset_not_offset() -> None:
    q = page_query(
        HubFilter(), TH, sort="hub", descending=False, cursor=("after", "hub-0050", "hub-0050"), limit=50
    )
    assert "OFFSET" not in q.text.upper()
    assert "(h.hub_id, h.hub_id) > (%s::text, %s)" in q.text
    assert "ORDER BY sort_value ASC, h.hub_id ASC LIMIT %s" in q.text
    assert q.params[-3:] == ["hub-0050", "hub-0050", 51]


def test_before_cursor_reads_backwards_and_age_is_inverted() -> None:
    q = page_query(
        HubFilter(), TH, sort="hub", descending=False, cursor=("before", "hub-0051", "hub-0051"), limit=25
    )
    assert "< (%s::text, %s)" in q.text and "DESC" in q.text
    age = page_query(HubFilter(), TH, sort="age", descending=False, cursor=None, limit=25)
    assert "ORDER BY sort_value DESC" in age.text  # ascending age = newest telemetry first


def test_where_binds_every_filter_value() -> None:
    flt = HubFilter(
        zones=("LZ_NORTH", "LZ_WEST"),
        bank="bank-07",
        health=("online", "fault"),
        activity=("charging", "idle"),
        soc_min=20,
        soc_max=80,
        q="hub-00_",
    )
    w = where_clause(flt, TH)
    assert "h.zone = ANY(%s)" in w.text and "h.bank_id = %s" in w.text and "lower(h.hub_id) LIKE %s" in w.text
    assert ["LZ_NORTH", "LZ_WEST"] in w.params and "bank-07" in w.params and ["online", "fault"] in w.params
    assert "hub-00\\_%" in w.params  # LIKE metacharacters in the search text are escaped
    assert w.text.count("%s") == len(w.params)
    assert "LZ_NORTH" not in w.text


def test_search_is_a_prefix_match() -> None:
    q = search_query("hub", "HUB-1", limit=20)
    assert "lower(h.hub_id) LIKE %s" in q.text and q.params == ["hub-1%", 20]
    assert "og.bank" in search_query("zone", "", limit=5).text


def test_cursor_round_trip_and_mismatch_is_refused() -> None:
    token = encode_cursor("after", "soc", True, 0.5, "hub-0009")
    assert decode_cursor(token, sort="soc", desc=True) == ("after", 0.5, "hub-0009")
    with pytest.raises(Exception, match="400"):
        decode_cursor(token, sort="hub", desc=True)


def test_health_comes_from_last_seen_not_the_cached_column() -> None:
    fresh = shape_hub(_row(1, last_seen_at=NOW - timedelta(seconds=1)), TH, now=NOW)
    assert fresh["health"] == "online" and fresh["health_label"] == "OK"
    stale = shape_hub(_row(1, last_seen_at=NOW - timedelta(seconds=10)), TH, now=NOW)
    assert stale["health_label"] == "WATCH"
    gone = shape_hub(_row(1, last_seen_at=NOW - timedelta(minutes=5)), TH, now=NOW)
    assert gone["health_label"] == "OFFLINE"
    faulted = shape_hub(_row(1, fault_code="F12", last_seen_at=NOW), TH, now=NOW)
    assert faulted["health_label"] == "FAULT"
    assert fresh["soc_pct"] == 50.0 and "sort_value" not in fresh


# -- endpoints ----------------------------------------------------------------------------------------


def test_table_pages_with_next_cursor_and_estimate(
    search_client: TestClient, rows_store: RecordingStore
) -> None:
    rows_store.answers.append([_row(i) for i in range(1, 27)])  # limit 25 + 1 -> there is a next page
    resp = search_client.get(
        "/og/api/fleet/table?limit=25&zone=LZ_SOUTH&health=OK&activity=idle&soc_min=10",
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 25 and body["items"][0]["health"] == "online"
    assert body["next_cursor"] and body["prev_cursor"] is None
    assert body["approx_total"] == 2500 and body["total_is_estimate"] is True
    assert rows_store.calls[1][0].startswith("EXPLAIN")

    rows_store.answers.append([_row(i) for i in range(26, 30)])
    nxt = search_client.get(
        f"/og/api/fleet/table?limit=25&cursor={body['next_cursor']}", headers=VIEWER_HEADERS
    )
    assert nxt.status_code == 200 and nxt.json()["prev_cursor"]


def test_unfiltered_total_uses_pg_class(search_client: TestClient, rows_store: RecordingStore) -> None:
    search_client.get("/og/api/fleet/table", headers=VIEWER_HEADERS)
    assert any("pg_class" in sql for sql, _ in rows_store.calls)


@pytest.mark.parametrize(
    "query", ["limit=30", "health=GREEN", "activity=flying", "soc_min=150", "sort=nope", "cursor=%21%21"]
)
def test_table_rejects_bad_params(search_client: TestClient, query: str) -> None:
    assert search_client.get(f"/og/api/fleet/table?{query}", headers=VIEWER_HEADERS).status_code in (400, 422)


def test_table_needs_a_role(search_client: TestClient) -> None:
    assert search_client.get("/og/api/fleet/table").status_code in (401, 403)


def test_selection_is_capped(search_client: TestClient, rows_store: RecordingStore) -> None:
    rows_store.answers.append([{"hub_id": f"hub-{i:04d}"} for i in range(1, 12)])
    body = search_client.get("/og/api/fleet/selection?zone=LZ_WEST&max=10", headers=VIEWER_HEADERS).json()
    assert body == {"hub_ids": [f"hub-{i:04d}" for i in range(1, 11)], "count": 10, "capped": True, "max": 10}
    assert rows_store.calls[-1][1][-1] == 11  # asks for cap+1 to know whether it was capped


def test_search_hubs_carries_zone_health_and_limit(
    search_client: TestClient, rows_store: RecordingStore
) -> None:
    rows_store.answers.append(
        [
            {
                "id": "hub-0001",
                "bank_id": "bank-01",
                "zone": "LZ_SOUTH",
                "rated_p_kw": 11,
                "fault_code": None,
                "last_seen_at": datetime.now(UTC),
                "stored_health": "offline",
            }
        ]
    )
    body = search_client.get("/og/api/fleet/search?kind=hub&q=hub-0&limit=20", headers=VIEWER_HEADERS).json()
    assert body["items"] == [
        {
            "id": "hub-0001",
            "zone": "LZ_SOUTH",
            "bank_id": "bank-01",
            "health": "online",
            "health_label": "OK",
            "rated_p_kw": 11.0,
        }
    ]
    assert search_client.get("/og/api/fleet/search?kind=meter&q=x", headers=VIEWER_HEADERS).status_code == 422
    assert (
        search_client.get("/og/api/fleet/search?kind=hub&limit=500", headers=VIEWER_HEADERS).status_code
        == 422
    )


def test_pending_release_requests_are_listed(
    search_client: TestClient, fake_proposals: ProposalStore
) -> None:
    from opengrid.api.routers.safestop import _ReleaseRequest

    fake_proposals.create(
        "safestop-release", _ReleaseRequest("ZONE", "LZ_SOUTH", "storm passed", "alice", NOW), "s", "alice"
    )
    fake_proposals.create("fleet_command", object(), "s", "bob")
    items = search_client.get("/og/api/fleet/release-requests", headers=VIEWER_HEADERS).json()["items"]
    assert len(items) == 1
    assert items[0]["scope"] == "ZONE" and items[0]["requested_by"] == "alice"


def test_hub_detail_aggregates_and_tolerates_missing_tables(
    search_client: TestClient, rows_store: RecordingStore
) -> None:
    seen = datetime.now(UTC).isoformat()
    rows_store.answers.extend(
        [
            [
                {
                    "hub": {
                        "hub_id": "hub-0001",
                        "bank_id": "bank-01",
                        "zone": "LZ_SOUTH",
                        "e_kwh": 39.2,
                        "r_kwh": 7.84,
                        "p_kw": 11,
                        "lat": 29.7,
                        "lon": -95.3,
                    },
                    "state": {
                        "soc_kwh": 19.6,
                        "p_kw": -2.0,
                        "health": "offline",
                        "last_seen_at": seen,
                        "lease_epoch": 3,
                    },
                    "bank": {"bank_id": "bank-01", "feeder_id": "F-1"},
                }
            ],
            [{"row": {"ts": seen, "p_kw": -2.0, "cell_temp_c": 27.0, "extra": 1}}],
            [],
            [],
        ]
    )
    body = search_client.get("/og/api/fleet/hubs/hub-0001/detail", headers=VIEWER_HEADERS).json()
    assert (
        body["status"]["health_label"] == "OK" and body["status"]["activity"] == "serving_home"
    )  # -2 kW = discharging, no lease
    assert body["status"]["soc_pct"] == 50.0 and body["status"]["above_reserve_kwh"] == 11.76
    assert body["telemetry"]["fields"] == {"ts": seen, "p_kw": -2.0, "cell_temp_c": 27.0}
    assert body["location"]["feeder_id"] == "F-1"
    assert body["asset"]["installed_at"] is None and body["asset"]["device_info"] == {}


def test_hub_detail_404(search_client: TestClient) -> None:
    assert search_client.get("/og/api/fleet/hubs/nope/detail", headers=VIEWER_HEADERS).status_code == 404


def test_hw_fw_filters_are_guarded_reads_and_bound() -> None:
    w = where_clause(HubFilter(hw=("C1",), fw=("4.2.1", "4.3.0"), fw_not="4.3.0"), TH)
    assert "(to_jsonb(h.*) ->> 'hardware_revision') = ANY(%s)" in w.text
    assert "(to_jsonb(h.*) ->> 'firmware_version') IS DISTINCT FROM %s" in w.text
    assert ["C1"] in w.params and ["4.2.1", "4.3.0"] in w.params and "4.3.0" in w.params
    assert not HubFilter(fw_not="x").empty
    q = page_query(HubFilter(), TH, sort="fw", descending=True, cursor=None, limit=25)
    assert "AS firmware_version" in q.text and "ORDER BY sort_value DESC" in q.text


def test_search_firmware_lists_distinct_versions(
    search_client: TestClient, rows_store: RecordingStore
) -> None:
    assert "DISTINCT (to_jsonb(h.*) ->> 'firmware_version')" in search_query("firmware", "4", limit=5).text
    rows_store.answers.append([{"id": "4.2.1"}, {"id": "4.3.0"}])
    body = search_client.get("/og/api/fleet/search?kind=firmware&q=4", headers=VIEWER_HEADERS).json()
    assert body["items"] == [{"id": "4.2.1"}, {"id": "4.3.0"}]
    ok = search_client.get("/og/api/fleet/table?hw=C1&fw=4.2.1&fw_not=4.3.0&sort=hw", headers=VIEWER_HEADERS)
    assert ok.status_code == 200, ok.text


def test_asset_class_is_derived_and_filterable() -> None:
    th = Thresholds(HealthThresholds(hub_stale_s=4.0, hub_offline_s=30.0), mobile=("trailer-mb-01",))
    w = where_clause(HubFilter(asset_class=("MOBILE", "UTILITY_SCALE")), th)
    assert "og.asset a WHERE a.asset_class = 'SUBSTATION'" in w.text
    assert w.params == [["trailer-mb-01"], ["trailer-mb-01"], ["MOBILE", "UTILITY_SCALE"]]
    q = page_query(HubFilter(), th, sort="hub", descending=False, cursor=None, limit=25)
    assert "AS asset_class" in q.text
    assert q.text.count("%s") == len(q.params)


def test_truck_detail_names_its_home_station(
    search_client: TestClient, rows_store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet_search, "mobile_units", lambda: {"trailer-mb-01": "LZ_AEN"})
    station = {
        "home_station_id": "hs-1",
        "zone": "LZ_AEN",
        "lat": 30.401,
        "lon": -97.719,
        "units": ["trailer-mb-01"],
    }
    monkeypatch.setattr(fleet_search, "home_stations", lambda: [station])
    seen = datetime.now(UTC).isoformat()
    rows_store.answers.append(
        [
            {
                "hub": {
                    "hub_id": "trailer-mb-01",
                    "bank_id": "trailer-mb-01",
                    "zone": "LZ_AEN",
                    "e_kwh": 600,
                    "r_kwh": 60,
                    "p_kw": 250,
                    "lat": 30.401,
                    "lon": -97.719,
                },
                "state": {"soc_kwh": 300, "p_kw": 0, "last_seen_at": seen},
                "bank": {},
            }
        ]
    )
    # the seeded og.hub lat/lon IS the home station; with no device report the position is UNKNOWN
    body = search_client.get("/og/api/fleet/hubs/trailer-mb-01/detail", headers=VIEWER_HEADERS).json()
    assert body["asset_class"] == "MOBILE"
    assert body["mobile"]["status"] == "UNKNOWN" and body["mobile"]["charging_allowed"] is False
    assert body["mobile"]["location"]["lat"] is None and body["mobile"]["location"]["source"] == "device"
    assert body["mobile"]["home_station"]["home_station_id"] == "hs-1"
    stations = search_client.get("/og/api/fleet/home-stations", headers=VIEWER_HEADERS).json()
    assert stations["items"] == [station]


def test_substation_detail_from_og_asset(search_client: TestClient, rows_store: RecordingStore) -> None:
    rows_store.answers.extend(
        [
            [
                {
                    "hub": {
                        "hub_id": "sub-LZ_AEN-00",
                        "bank_id": "b-sub",
                        "zone": "LZ_AEN",
                        "e_kwh": 16000,
                        "r_kwh": 0,
                        "p_kw": 4000,
                    },
                    "state": {"soc_kwh": 8000, "p_kw": 0},
                    "bank": {},
                }
            ],
            [],
            [],
            [],
            [
                {
                    "row": {
                        "asset_id": "sub-LZ_AEN-00",
                        "p_kw": 4000,
                        "e_kwh": 16000,
                        "poi_import_kva": 4000,
                        "poi_export_kva": 3500,
                        "feeder_id": "F-7",
                        "substation_id": "S-1",
                        "status": "ACTIVE",
                    }
                }
            ],
        ]
    )
    body = search_client.get("/og/api/fleet/hubs/sub-LZ_AEN-00/detail", headers=VIEWER_HEADERS).json()
    assert body["asset_class"] == "UTILITY_SCALE"
    assert body["utility_scale"]["mw"] == 4.0 and body["utility_scale"]["poi_export_kva"] == 3500.0


def test_classify_asset_matches_the_sql_rule() -> None:
    from opengrid.api.routers.fleet_search import classify_asset

    kw: dict[str, Any] = {"mobile": {"trailer-1"}, "substations": {"bank-sub"}}
    assert classify_asset("trailer-1", "trailer-1", **kw) == "MOBILE"
    assert classify_asset("sub-00", "bank-sub", **kw) == "UTILITY_SCALE"
    assert classify_asset("hub-1", "bank-1", **kw) == "HOME"


def test_production_thresholds_10s_reports_25s_stale_60s_offline() -> None:
    """R1 (r3.4 review): with the real config -- 10 s reports, hub_stale_s 25, hub_offline_s 60 -- a hub
    between reports is OK (never WATCH next to a fresh badge), and the SQL health filter binds 60/25."""
    from opengrid.platform.config import Config

    cfg = Config({"fleet": {"telemetry_interval_s": 10}, "health": {"hub_stale_s": 25, "hub_offline_s": 60}})
    th = Thresholds.from_config(cfg)
    assert (th.online_s, th.offline_s) == (25.0, 60.0)
    label = {
        s: shape_hub(_row(1, last_seen_at=NOW - timedelta(seconds=s)), th, now=NOW)["health_label"]
        for s in (9, 15, 24, 30, 59, 70)
    }
    assert label == {9: "OK", 15: "OK", 24: "OK", 30: "WATCH", 59: "WATCH", 70: "OFFLINE"}
    w = where_clause(HubFilter(health=("online",)), th)
    assert w.params[:2] == [60.0, 25.0] and ["online"] in w.params


def test_availability_is_read_guarded_filterable_and_shaped() -> None:
    """D-37: bank availability joins og.bank via to_jsonb (NULL before 0046 reads AVAILABLE), filters bind
    the state, and every row carries the four owner-worded fields from opengrid.market.availability."""
    w = where_clause(HubFilter(availability=("UNAVAILABLE",)), TH)
    assert "coalesce(to_jsonb(b.*) ->> 'availability', 'AVAILABLE') = ANY(%s)" in w.text
    assert ["UNAVAILABLE"] in w.params
    q = page_query(HubFilter(), TH, sort="hub", descending=False, cursor=None, limit=25)
    assert "LEFT JOIN og.bank b ON b.bank_id = h.bank_id" in q.text and "AS availability_reason" in q.text
    shaped = shape_hub(
        _row(1, availability="UNAVAILABLE", availability_reason="REGULATED_NO_CONTRACT"), TH, now=NOW
    )
    assert shaped["availability"] == "UNAVAILABLE"
    assert shaped["availability_badge"].startswith("Regulated market")
    assert "NOIE" in shaped["availability_text"]
    plain = shape_hub(_row(2), TH, now=NOW)
    assert plain["availability"] == "AVAILABLE" and plain["availability_badge"] is None


def test_availability_filter_rejects_unknown_states(search_client: TestClient) -> None:
    assert (
        search_client.get("/og/api/fleet/table?availability=MAYBE", headers=VIEWER_HEADERS).status_code == 422
    )


def _truck_detail(
    search_client: TestClient,
    rows_store: RecordingStore,
    monkeypatch: pytest.MonkeyPatch,
    report: dict[str, Any],
) -> dict[str, Any]:
    monkeypatch.setattr(fleet_search, "mobile_units", lambda: {"trailer-mb-01": "LZ_AEN"})
    station = {
        "home_station_id": "hs-1",
        "zone": "LZ_AEN",
        "lat": 30.401,
        "lon": -97.719,
        "units": ["trailer-mb-01"],
    }
    monkeypatch.setattr(fleet_search, "home_stations", lambda: [station])
    seed = {
        "hub_id": "trailer-mb-01",
        "bank_id": "trailer-mb-01",
        "zone": "LZ_AEN",
        "e_kwh": 600,
        "r_kwh": 60,
        "p_kw": 250,
        "lat": 30.401,
        "lon": -97.719,
    }
    rows_store.answers.extend(
        [
            [
                {
                    "hub": seed,
                    "state": {"soc_kwh": 300, "p_kw": 0, "last_seen_at": datetime.now(UTC).isoformat()},
                    "bank": {},
                }
            ],
            [],
            [],
            [],
            [{"hub_id": "trailer-mb-01", "bank_id": "trailer-mb-01", **report}],
        ]
    )
    body: dict[str, Any] = search_client.get(
        "/og/api/fleet/hubs/trailer-mb-01/detail", headers=VIEWER_HEADERS
    ).json()
    assert any("device_lat" in sql and "og.hub.lat" not in sql for sql, _ in rows_store.calls)
    return body["mobile"]


def test_truck_uses_the_device_reported_position_not_the_seed(
    search_client: TestClient, rows_store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    away = {
        "device_lat": 30.27,
        "device_lon": -97.74,
        "device_info_at": datetime.now(UTC) - timedelta(seconds=20),
    }
    m = _truck_detail(search_client, rows_store, monkeypatch, away)
    assert m["status"] == "AWAY"  # seeded lat/lon is at the station; the device says it left
    assert (m["location"]["lat"], m["location"]["lon"]) == (30.27, -97.74) and m["location"]["fresh"] is True
    assert 15 <= m["location"]["age_s"] <= 60


def test_truck_at_home_by_the_core_geo_rule(
    search_client: TestClient, rows_store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = {
        "device_lat": 30.4015,
        "device_lon": -97.7192,
        "device_info_at": datetime.now(UTC) - timedelta(seconds=5),
    }
    assert _truck_detail(search_client, rows_store, monkeypatch, home)["status"] == "AT_HOME_STATION"


def test_truck_with_a_stale_report_is_unknown(
    search_client: TestClient, rows_store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = {
        "device_lat": 30.401,
        "device_lon": -97.719,
        "device_info_at": datetime.now(UTC) - timedelta(minutes=10),
    }
    m = _truck_detail(search_client, rows_store, monkeypatch, stale)
    assert m["status"] == "UNKNOWN" and m["location"]["fresh"] is False and m["location"]["lat"] == 30.401
