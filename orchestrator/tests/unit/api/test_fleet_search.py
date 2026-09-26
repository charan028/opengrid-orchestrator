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

from .conftest import VIEWER_HEADERS

TH = Thresholds(online_s=4.0, offline_s=30.0)
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
    assert body["status"]["health_label"] == "OK" and body["status"]["activity"] == "charging"
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
