"""Integration tests hitting the real ogsim.market FastAPI app in-process
over ASGI (no separate uvicorn process needed) with httpx, per BUILD.md's
"integration test hitting the running market app with httpx". Routes, query
params and response shapes are checked against
interfaces/http/market-api.md."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from ogsim.market.app import create_app
from ogsim.market.config import MarketConfig

TEST_USER = "test@example.com"
TEST_PASSWORD = "test"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    cfg = MarketConfig(data_mode="synthetic", seed=7, test_users={TEST_USER: TEST_PASSWORD})
    app = create_app(cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://market.test") as c:
        yield c


def _headers() -> dict[str, str]:
    # interfaces/http/market-api.md §1: the sim accepts any non-empty
    # Authorization header, no cryptographic validation required.
    return {"Authorization": "Bearer anything-non-empty", "Ocp-Apim-Subscription-Key": "test-primary-key"}


async def test_token_endpoint_rejects_unknown_user(client: httpx.AsyncClient):
    resp = await client.post("/token", data={"username": "nobody", "password": "x", "grant_type": "password"})
    assert resp.status_code == 400


async def test_token_endpoint_accepts_configured_test_user(client: httpx.AsyncClient):
    resp = await client.post(
        "/token", data={"username": TEST_USER, "password": TEST_PASSWORD, "grant_type": "password"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == body["id_token"]
    assert body["expires_in"] == 3600


async def test_spp_endpoint_requires_subscription_key(client: httpx.AsyncClient):
    resp = await client.get(
        "/ercot/np6-905-cd/spp_node_zone_hub", headers={"Authorization": "Bearer anything"}
    )
    assert resp.status_code == 401


async def test_spp_endpoint_rejects_empty_authorization_header(client: httpx.AsyncClient):
    resp = await client.get(
        "/ercot/np6-905-cd/spp_node_zone_hub",
        headers={"Authorization": "", "Ocp-Apim-Subscription-Key": "test-primary-key"},
    )
    assert resp.status_code == 401


async def test_spp_endpoint_returns_the_documented_envelope(client: httpx.AsyncClient):
    resp = await client.get("/ercot/np6-905-cd/spp_node_zone_hub", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "_meta" in body and "fields" in body and "data" in body
    assert body["_meta"]["currentPage"] == 1
    assert body["fields"] == [
        {"name": "deliveryDate", "dataType": "DATE"},
        {"name": "deliveryDateTime", "dataType": "TIMESTAMP"},
        {"name": "settlementPoint", "dataType": "STRING"},
        {"name": "settlementPointPrice", "dataType": "STRING"},
    ]
    assert len(body["data"][0]) == len(body["fields"])
    assert isinstance(body["data"][0][3], str)


async def test_spp_endpoint_defaults_to_load_zone_points(client: httpx.AsyncClient):
    resp = await client.get("/ercot/np6-905-cd/spp_node_zone_hub", headers=_headers())
    points = {row[2] for row in resp.json()["data"]}
    assert points <= {"LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST"}


async def test_load_endpoint_uses_operating_date_params(client: httpx.AsyncClient):
    resp = await client.get(
        "/ercot/np6-345-cd/act_sys_load_by_wzn",
        params={"operatingDateFrom": "2026-09-24", "operatingDateTo": "2026-09-26"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    fields = [f["name"] for f in resp.json()["fields"]]
    assert fields == ["operatingDate", "operatingDateTime", "weatherZone", "load"]


async def test_admin_price_spike_shows_up_in_spp_response(client: httpx.AsyncClient):
    await client.post(
        "/admin/anomalies",
        json={
            "id": "spike-1",
            "type": "price_spike",
            "target": "np6-905-cd",
            "params": {"value_usd_per_mwh": 5000.0},
            "duration": 120.0,
        },
    )
    resp = await client.get(
        "/ercot/np6-905-cd/spp_node_zone_hub", params={"settlementPoint": "LZ_NORTH"}, headers=_headers()
    )
    prices = [row[3] for row in resp.json()["data"]]
    assert "5000.0" in prices


async def test_admin_http_5xx_outage_short_circuits_the_route(client: httpx.AsyncClient):
    await client.post(
        "/admin/anomalies",
        json={
            "id": "outage-1",
            "type": "http_5xx",
            "target": "np6-345-cd",
            "params": {"status": 503},
            "duration": 60.0,
        },
    )
    resp = await client.get("/ercot/np6-345-cd/act_sys_load_by_wzn", headers=_headers())
    assert resp.status_code == 503


async def test_admin_http_429_sets_retry_after_header(client: httpx.AsyncClient):
    await client.post(
        "/admin/anomalies",
        json={
            "id": "throttle-1",
            "type": "http_429",
            "target": "np4-732-cd",
            "params": {"retry_after_s": 42},
            "duration": 60.0,
        },
    )
    resp = await client.get("/ercot/np4-732-cd/wpp_hrly_avrg_actl_fcast", headers=_headers())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "42"


async def test_admin_401_primary_only_rejects_the_primary_key():
    cfg = MarketConfig(data_mode="synthetic", seed=1, test_users={TEST_USER: TEST_PASSWORD})
    app = create_app(cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://market.test") as client:
        await client.post(
            "/admin/anomalies",
            json={
                "id": "key-rot-1",
                "type": "http_401_primary",
                "target": "np6-905-cd",
                "params": {},
                "duration": 60.0,
            },
        )
        primary_resp = await client.get(
            "/ercot/np6-905-cd/spp_node_zone_hub",
            headers={"Authorization": "Bearer x", "Ocp-Apim-Subscription-Key": "test-primary-key"},
        )
        secondary_resp = await client.get(
            "/ercot/np6-905-cd/spp_node_zone_hub",
            headers={"Authorization": "Bearer x", "Ocp-Apim-Subscription-Key": "test-secondary-key"},
        )
    assert primary_resp.status_code == 401
    assert secondary_resp.status_code == 200


async def test_admin_malformed_payload_returns_broken_json(client: httpx.AsyncClient):
    await client.post(
        "/admin/anomalies",
        json={
            "id": "malformed-1",
            "type": "malformed_payload",
            "target": "np4-188-cd",
            "params": {},
            "duration": 60.0,
        },
    )
    resp = await client.get("/ercot/np4-188-cd/dam_clear_price_for_cap", headers=_headers())
    assert resp.status_code == 200
    with pytest.raises(Exception):  # noqa: B017 - deliberately malformed JSON body
        resp.json()


async def test_admin_cancel_removes_an_active_anomaly(client: httpx.AsyncClient):
    await client.post(
        "/admin/anomalies",
        json={"id": "cancel-me", "type": "price_spike", "target": "*", "params": {}, "duration": 300.0},
    )
    listing = await client.get("/admin/anomalies")
    assert any(a["id"] == "cancel-me" for a in listing.json()["anomalies"])
    cancel_resp = await client.delete("/admin/anomalies/cancel-me")
    assert cancel_resp.json()["ok"] is True


async def test_as_endpoint_uses_ancillary_type_codes(client: httpx.AsyncClient):
    resp = await client.get("/ercot/np4-188-cd/dam_clear_price_for_cap", headers=_headers())
    ancillary_types = {row[2] for row in resp.json()["data"]}
    assert ancillary_types == {"REGUP", "REGDN", "RRS", "NSPIN", "ECRS"}


async def test_eia_endpoint_is_mounted_under_eia_prefix_and_requires_api_key(client: httpx.AsyncClient):
    resp = await client.get("/eia/electricity/rto/region-data/data")
    assert resp.status_code == 403


async def test_eia_endpoint_returns_hourly_demand(client: httpx.AsyncClient):
    resp = await client.get("/eia/electricity/rto/region-data/data", params={"api_key": "test-eia-key"})
    assert resp.status_code == 200
    assert resp.json()["response"]["data"]


async def test_nws_points_is_mounted_under_nws_prefix_and_requires_user_agent(client: httpx.AsyncClient):
    # httpx always sends a default User-Agent, so explicitly blank it out to
    # exercise NWS's real "reject requests without one" behaviour.
    resp = await client.get("/nws/points/29.76,-95.37", headers={"User-Agent": ""})
    assert resp.status_code == 400


async def test_nws_hourly_forecast_returns_periods(client: httpx.AsyncClient):
    resp = await client.get(
        "/nws/gridpoints/EWX/156,91/forecast/hourly", headers={"User-Agent": "test-agent (ops@example.com)"}
    )
    assert resp.status_code == 200
    assert len(resp.json()["properties"]["periods"]) == 48


async def test_nws_hourly_forecast_returns_304_when_not_modified(client: httpx.AsyncClient):
    first = await client.get(
        "/nws/gridpoints/EWX/156,91/forecast/hourly", headers={"User-Agent": "test-agent"}
    )
    updated = first.json()["properties"]["updated"]
    from datetime import datetime
    from email.utils import format_datetime

    if_modified_since = format_datetime(datetime.fromisoformat(updated))
    second = await client.get(
        "/nws/gridpoints/EWX/156,91/forecast/hourly",
        headers={"User-Agent": "test-agent", "If-Modified-Since": if_modified_since},
    )
    assert second.status_code == 304
