"""D-38 delivery-record routes: a utility sees only its own calls' records, a customer only its own
contracts', and the operator API lists, details and summarizes them (measured values, never grants)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from opengrid.api.deps import get_pool
from opengrid.delivery import store
from opengrid.delivery.models import CallKind, DeliveryRecord, SeriesPoint
from opengrid.platform.config import Config

T0 = datetime(2026, 9, 27, 21, 30, tzinfo=UTC)
CUSTOMER_ID = UUID("00000000-0000-7000-8000-0000000000c6")
AEN = {"X-Remote-User": "og-util-aen"}
CPS = {"X-Remote-User": "og-util-cps"}
CUSTOMER = {"X-Remote-User": "og-cust-a"}
VIEWER = {"X-Remote-User": "op-view"}


@pytest.fixture
def config() -> Config:
    return Config(
        {
            "api": {
                "roles": {
                    "operator": [],
                    "viewer": ["op-view"],
                    "customer": {"og-cust-a": str(CUSTOMER_ID)},
                    "utility": {"og-util-aen": "AUSTIN_ENERGY", "og-util-cps": "CPS_ENERGY"},
                },
                "utility_api": {"enabled_utilities": ["AUSTIN_ENERGY", "CPS_ENERGY"]},
            }
        }
    )


def _record(utility_id: str | None, customer_id: UUID | None = None) -> DeliveryRecord:
    return DeliveryRecord(
        call_id=str(uuid4()),
        call_kind=CallKind.UTILITY_CALL,
        utility_id=utility_id,
        customer_id=customer_id or uuid4(),
        service_type="REGULATED_CAPACITY",
        window_start=T0,
        window_end=T0 + timedelta(minutes=15),
        committed_kw=-20_000.0,
        delivered_kw_last=-19_700.0,
        discharged_kwh=4_800.0,
        committed_kwh=5_000.0,
        ramp_time_s=600.0,
        result="PASS",
        series=[SeriesPoint(t=T0, s=30.0, c=-20_000.0, m=-20_000.0, d=-19_700.0)],
        final=True,
    )


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> dict[str, DeliveryRecord]:
    rows = {r.call_id: r for r in (_record("AUSTIN_ENERGY", CUSTOMER_ID), _record("CPS_ENERGY"))}

    async def fetch_record(_pool: Any, call_id: str) -> DeliveryRecord | None:
        return rows.get(call_id)

    async def list_records(_pool: Any, **kw: Any) -> list[DeliveryRecord]:
        out = list(rows.values())
        for key in ("utility_id", "customer_id"):
            if kw.get(key) is not None:
                out = [r for r in out if getattr(r, key) == kw[key]]
        return [r.model_copy(update={"series": []}) for r in out]

    async def summary(_pool: Any, **_kw: Any) -> list[dict[str, Any]]:
        return [{"contract_id": None, "day": "2026-09-27", "calls": 2, "passed": 2, "compliance_pct": 100.0}]

    monkeypatch.setattr(store, "fetch_record", fetch_record)
    monkeypatch.setattr(store, "list_records", list_records)
    monkeypatch.setattr(store, "summary", summary)
    return rows


@pytest.fixture
def api(client: TestClient, records: dict[str, DeliveryRecord]) -> Iterator[TestClient]:
    client.app.dependency_overrides[get_pool] = lambda: object()  # type: ignore[attr-defined]
    yield client


def _by_utility(records: dict[str, DeliveryRecord], utility_id: str) -> DeliveryRecord:
    return next(r for r in records.values() if r.utility_id == utility_id)


def test_a_utility_lists_only_its_own_calls_measured_delivery(api, records) -> None:
    body = api.get("/og/api/customer/v1/utility/delivery-records", headers=AEN).json()

    assert body["utility_id"] == "AUSTIN_ENERGY"
    assert [r["utility_id"] for r in body["records"]] == ["AUSTIN_ENERGY"]
    assert body["records"][0]["delivered_kw_last"] == -19_700.0 and "series" not in body["records"][0]


def test_another_utilitys_record_is_404_like_a_missing_one(api, records) -> None:
    cps = _by_utility(records, "CPS_ENERGY")
    aen = _by_utility(records, "AUSTIN_ENERGY")

    assert (
        api.get(f"/og/api/customer/v1/utility/delivery-records/{cps.call_id}", headers=AEN).status_code == 404
    )
    assert api.get("/og/api/customer/v1/utility/delivery-records/nope", headers=AEN).status_code == 404
    detail = api.get(f"/og/api/customer/v1/utility/delivery-records/{aen.call_id}", headers=AEN).json()
    assert detail["series"][0]["d"] == -19_700.0 and detail["sign_convention"] == "+charge/-discharge"


def test_a_customer_sees_only_its_contracts_records(api, records) -> None:
    body = api.get("/og/api/customer/delivery-records", headers=CUSTOMER).json()

    assert [r["customer_id"] for r in body["records"]] == [str(CUSTOMER_ID)]
    cps = _by_utility(records, "CPS_ENERGY")
    assert api.get(f"/og/api/customer/delivery-records/{cps.call_id}", headers=CUSTOMER).status_code == 404


def test_the_utility_routes_refuse_other_roles(api) -> None:
    assert api.get("/og/api/customer/v1/utility/delivery-records", headers=CUSTOMER).status_code == 403
    assert api.get("/og/api/customer/delivery-records", headers=AEN).status_code == 403


def test_the_operator_api_lists_details_and_summarizes(api, records) -> None:
    listed = api.get("/og/api/delivery/records", headers=VIEWER)
    assert listed.status_code == 200 and len(listed.json()) == 2
    one = next(iter(records))
    detail = api.get(f"/og/api/delivery/records/{one}", headers=VIEWER).json()
    assert detail["result"] == "PASS" and detail["series"]
    assert api.get("/og/api/delivery/records/missing", headers=VIEWER).status_code == 404
    assert api.get("/og/api/delivery/summary?days=7", headers=VIEWER).json()[0]["compliance_pct"] == 100.0
    assert api.get("/og/api/delivery/records", headers=AEN).status_code == 403
