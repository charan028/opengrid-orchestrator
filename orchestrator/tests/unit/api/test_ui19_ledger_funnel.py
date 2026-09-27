"""Gitea #19 `GET /og/api/dispatch/ledger` and `GET /og/api/markets/bid-funnel`, plus 09 S11.8
`GET /og/api/profitability/per-kw`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from opengrid.api.routers.profitability_kw import current_settlement_month
from opengrid.api.views_ext import CommitmentSlice, FunnelRow, MmsFunnelRow, ReservationSlice
from opengrid.market.pg_backend import contract_totals_from_row
from opengrid.platform.config import Config

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS
from .ui19_fakes import FakeExtViews, FleetFakeStore, build_app, hub_row, make_client

OBL_A = UUID("00000000-0000-7000-8000-00000000b001")
OBL_B = UUID("00000000-0000-7000-8000-00000000b002")


@pytest.fixture
def views() -> FakeExtViews:
    now = datetime.now(UTC)
    start, end = now - timedelta(hours=1), now + timedelta(hours=1)
    return FakeExtViews(
        rows=[
            hub_row(i, banks=2) for i in range(4)
        ],  # bank-000 (LZ_NORTH): hubs 0, 2; bank-001 (LZ_SOUTH): 1, 3
        reservations=[
            ReservationSlice(OBL_A, "bank-000", 20.0, start, end),
            ReservationSlice(OBL_A, "bank-001", 10.0, start, end),
        ],
        commitments=[
            CommitmentSlice(OBL_A, "ERCOT_ENERGY", 24.0, start, end),
            CommitmentSlice(OBL_B, "ERCOT_AS", 5.0, start, end),
        ],
    )


@pytest.fixture
def client(views, fake_trace_store, fake_proposals) -> TestClient:
    cfg = Config({"api": {"roles": {"operator": [], "viewer": []}}})
    return make_client(
        build_app(views, FleetFakeStore(views.rows), fake_trace_store, fake_proposals, cfg), PROXY_HEADERS
    )


# -- ledger ------------------------------------------------------------------------------------------


def test_fleet_ledger_aggregates_and_labels_uncommitted_capacity(client) -> None:
    body = client.get("/og/api/dispatch/ledger", headers=VIEWER_HEADERS).json()
    assert body["labels"] == {"uncommitted_capacity_kw": "Uncommitted capacity"}
    now = body["now"]
    assert now["capacity_kw"] == 44.0
    assert now["reserved_kw"] == 30.0
    assert now["committed_kw"] == 29.0
    assert now["uncommitted_capacity_kw"] == 14.0
    assert now["unallocated_committed_kw"] == 5.0
    assert now["committed_by_service"] == {"ERCOT_AS": 5.0, "ERCOT_ENERGY": 24.0}
    assert len(body["timeline"]) == (26 * 60) // 15
    assert [c["id"] for c in body["children"]] == ["LZ_NORTH", "LZ_SOUTH"]
    assert body["children"][0]["level"] == "zone"


def test_zone_bank_and_hub_ledgers_allocate_by_reservation_share(client) -> None:
    zone = client.get("/og/api/dispatch/ledger?level=zone&id=LZ_NORTH", headers=VIEWER_HEADERS).json()["now"]
    assert (zone["capacity_kw"], zone["reserved_kw"], zone["committed_kw"]) == (22.0, 20.0, 16.0)
    assert zone["uncommitted_capacity_kw"] == 2.0 and "unallocated_committed_kw" not in zone
    bank = client.get("/og/api/dispatch/ledger?level=bank&id=bank-001", headers=VIEWER_HEADERS).json()
    assert (bank["now"]["reserved_kw"], bank["now"]["committed_kw"]) == (10.0, 8.0)
    assert [c["id"] for c in bank["children"]] == ["hub-00001", "hub-00003"]
    hub = client.get("/og/api/dispatch/ledger?level=hub&id=hub-00000", headers=VIEWER_HEADERS).json()
    assert (hub["now"]["capacity_kw"], hub["now"]["reserved_kw"], hub["now"]["committed_kw"]) == (
        11.0,
        10.0,
        8.0,
    )
    assert hub["children"] == [] and hub["notes"]


def test_ledger_window_and_bucket(client) -> None:
    t0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    resp = client.get(
        "/og/api/dispatch/ledger",
        params={"from": t0.isoformat(), "to": (t0 + timedelta(hours=2)).isoformat(), "bucket_minutes": 30},
        headers=VIEWER_HEADERS,
    )
    body = resp.json()
    assert [p["t"] for p in body["timeline"]][:2] == [
        t0.isoformat(),
        (t0 + timedelta(minutes=30)).isoformat(),
    ]
    assert len(body["timeline"]) == 4


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("level=zone", 422),
        ("level=zone&id=LZ_NOWHERE", 404),
        ("level=hub&id=hub-99999", 404),
        ("level=planet&id=x", 422),
        ("from=2026-09-26T12:00:00Z&to=2026-09-26T11:00:00Z", 422),
        ("from=2026-01-01T00:00:00Z&to=2026-09-01T00:00:00Z&bucket_minutes=5", 422),
    ],
)
def test_ledger_rejects_bad_queries(client, query, code) -> None:
    assert client.get(f"/og/api/dispatch/ledger?{query}", headers=VIEWER_HEADERS).status_code == code


# -- bid funnel --------------------------------------------------------------------------------------


def test_bid_funnel_counts_stages_products_and_reasons(views, client) -> None:
    b = datetime(2026, 9, 25, tzinfo=UTC)
    views.funnel = [
        FunnelRow(b, "ERCOT_ENERGY", "ENERGY", "SELECTED", "COMMITTED", None, 3),
        FunnelRow(b, "ERCOT_ENERGY", "ENERGY", "SELECTED", "SELECTED", None, 2),
        FunnelRow(b, "ERCOT_AS", "ECRS", "REJECTED", "REJECTED", "R-GATE-REJECT", 4),
        FunnelRow(b, "ERCOT_AS", "ECRS", "EXPIRED", "EXPIRED", "R-EXPIRED-UNSELECTED", 1),
        FunnelRow(b, "DATA_CENTER", "DATA_CENTER", None, None, "R-ADMIT-REJECT", 2),
    ]
    body = client.get(
        "/og/api/markets/bid-funnel?from=2026-09-20T00:00:00Z&to=2026-09-27T00:00:00Z", headers=VIEWER_HEADERS
    ).json()
    assert body["bucket"] == "day"
    assert body["totals"] == {"available": 12, "submitted": 5, "awarded": 3, "rejected": 6, "expired": 1}
    products = {(p["service_type"], p["product"]): p for p in body["by_product"]}
    assert products[("ERCOT_AS", "ECRS")]["rejected"] == 4
    assert products[("ERCOT_ENERGY", "ENERGY")]["awarded"] == 3
    assert [(r["reason_code"], r["count"]) for r in body["rejection_reasons"]] == [
        ("R-GATE-REJECT", 4),
        ("R-ADMIT-REJECT", 2),
    ]
    assert body["series"][0]["bucket_start"] == b.isoformat()
    assert body["mms"] is None and body["sources"]["submitted"] == ["gate decisions"]


def test_bid_funnel_includes_mms_submissions_when_the_table_exists(views, client) -> None:
    b = datetime(2026, 9, 26, 10, tzinfo=UTC)
    views.mms = [
        MmsFunnelRow(b, "ECRS", "ACCEPTED", None, 2),
        MmsFunnelRow(b, "ECRS", "REJECTED", "MMS-BID-INVALID", 1),
    ]
    body = client.get(
        "/og/api/markets/bid-funnel?from=2026-09-26T00:00:00Z&to=2026-09-27T00:00:00Z", headers=VIEWER_HEADERS
    ).json()
    assert body["bucket"] == "hour"
    assert body["mms"] == {"submitted": 3, "accepted": 2, "rejected": 1}
    assert body["rejection_reasons"][0]["reason_code"] == "MMS-BID-INVALID"
    assert "ercot_mms" in body["sources"]["submitted"]


def test_bid_funnel_rejects_bad_windows(client) -> None:
    assert (
        client.get(
            "/og/api/markets/bid-funnel?from=2026-09-27T00:00:00Z&to=2026-09-26T00:00:00Z",
            headers=VIEWER_HEADERS,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/og/api/markets/bid-funnel?from=2026-01-01T00:00:00Z&to=2026-09-26T00:00:00Z",
            headers=VIEWER_HEADERS,
        ).status_code
        == 422
    )


# -- per-kW profitability ------------------------------------------------------------------------------


def _totals():
    row = {
        "contract_id": "00000000-0000-7000-8000-000000000d02",
        "service_type": "ERCOT_ENERGY",
        "market": "FREE",
        "utility_id": None,
        "revenue": Decimal("900"),
        "energy_cost": Decimal("300"),
        "degradation_cost": Decimal("20"),
        "penalty": Decimal("0"),
        "delivery_charge": Decimal("15"),
        "kwh_held": Decimal("72000"),
        "hours_held": Decimal("720"),
    }
    return [contract_totals_from_row(row, Decimal("720"))]


def test_per_kw_returns_the_market_view(views, client) -> None:
    views.totals = _totals()
    resp = client.get(
        "/og/api/profitability/per-kw?start=2026-09-01T05:00:00Z&end=2026-10-01T05:00:00Z",
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "method_version",
        "period_hours",
        "hardware_view_usd_per_kw",
        "target_payback_years",
        "contracts",
        "markets",
        "fleet",
        "illustrative_home_unit",
    }
    assert len(body["contracts"]) == 1 and set(body["markets"]) == {"REGULATED", "FREE"}
    assert views.calls["contract_totals_window"] == 720


def test_per_kw_defaults_to_the_settlement_month(views, client) -> None:
    assert client.get("/og/api/profitability/per-kw", headers=OPERATOR_HEADERS).status_code == 200
    assert views.calls["contract_totals_window"] in (743, 744, 745, 671, 672, 719, 720, 721, 695, 696)


def test_current_settlement_month_is_chicago_midnight() -> None:
    start, end = current_settlement_month(datetime(2026, 9, 26, 3, tzinfo=UTC))  # still Sept 25 in Chicago
    assert (start, end) == (datetime(2026, 9, 1, 5, tzinfo=UTC), datetime(2026, 10, 1, 5, tzinfo=UTC))
    start, end = current_settlement_month(datetime(2026, 12, 15, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 12, 1, 6, tzinfo=UTC), datetime(2027, 1, 1, 6, tzinfo=UTC))


def test_per_kw_rejects_end_before_start_and_is_viewer_readable(client) -> None:
    resp = client.get(
        "/og/api/profitability/per-kw?start=2026-10-01T00:00:00Z&end=2026-09-01T00:00:00Z",
        headers=OPERATOR_HEADERS,
    )
    assert resp.status_code == 400
    # r3.4: the $/kW view is read-only, so a viewer may read it
    assert client.get("/og/api/profitability/per-kw", headers=VIEWER_HEADERS).status_code == 200


# -- authz for the read endpoints in this file --------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/og/api/dispatch/ledger", "/og/api/markets/bid-funnel", "/og/api/profitability/per-kw"]
)
def test_reads_need_an_identity(client, path) -> None:
    assert client.get(path).status_code == 401
