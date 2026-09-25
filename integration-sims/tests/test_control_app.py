"""Integration tests for the ogsim.control FastAPI app over ASGI. httpx's
ASGITransport does not drive the lifespan protocol, so the random engine's
background loops never start here - only its REST-exposed state is
exercised, keeping these tests fast and deterministic."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ogsim.control.app import create_app

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
RANDOM_CONFIG = Path(__file__).resolve().parents[1] / "config" / "random.yaml"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(scenarios_dir=SCENARIOS_DIR, random_config_path=RANDOM_CONFIG)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.test") as c:
        yield c


async def test_anomaly_types_lists_the_full_catalogue(client: httpx.AsyncClient):
    resp = await client.get("/api/anomaly-types")
    assert resp.status_code == 200
    assert len(resp.json()["types"]) >= 30


async def test_inject_then_list_active_round_trips(client: httpx.AsyncClient):
    inject_resp = await client.post(
        "/api/inject", json={"type": "price_spike", "target": "*", "duration": 60.0}
    )
    assert inject_resp.status_code == 200
    anomaly_id = inject_resp.json()["anomaly"]["id"]

    active_resp = await client.get("/api/anomalies")
    assert any(a["id"] == anomaly_id for a in active_resp.json()["active"])


async def test_inject_unknown_type_returns_422(client: httpx.AsyncClient):
    resp = await client.post("/api/inject", json={"type": "nonsense", "target": "*"})
    assert resp.status_code == 422


async def test_cancel_removes_the_anomaly(client: httpx.AsyncClient):
    inject_resp = await client.post(
        "/api/inject", json={"type": "price_spike", "target": "*", "duration": 60.0}
    )
    anomaly_id = inject_resp.json()["anomaly"]["id"]
    cancel_resp = await client.delete(f"/api/anomalies/{anomaly_id}")
    assert cancel_resp.json()["ok"] is True


async def test_log_endpoint_reflects_injections(client: httpx.AsyncClient):
    await client.post("/api/inject", json={"type": "price_spike", "target": "*", "duration": 60.0})
    log_resp = await client.get("/api/log")
    assert any(row["action"] == "inject" for row in log_resp.json()["log"])


async def test_scenarios_are_listed(client: httpx.AsyncClient):
    resp = await client.get("/api/scenarios")
    names = {s["name"] for s in resp.json()["scenarios"]}
    assert "tampered_command" in names


async def test_running_an_unknown_scenario_returns_404(client: httpx.AsyncClient):
    resp = await client.post("/api/scenarios/does-not-exist/run", json={"speed": 1.0})
    assert resp.status_code == 404


async def test_random_status_reports_shipped_config(client: httpx.AsyncClient):
    resp = await client.get("/api/random/status")
    body = resp.json()
    assert body["paused"] is False
    assert body["profile"] == "normal"


async def test_random_pause_and_resume_round_trip(client: httpx.AsyncClient):
    paused = await client.post("/api/random/pause")
    assert paused.json()["paused"] is True
    resumed = await client.post("/api/random/resume")
    assert resumed.json()["paused"] is False


async def test_random_profile_can_be_changed(client: httpx.AsyncClient):
    resp = await client.post("/api/random/profile", json={"profile": "chaos"})
    assert resp.json()["profile"] == "chaos"


async def test_random_profile_rejects_unknown_profile(client: httpx.AsyncClient):
    resp = await client.post("/api/random/profile", json={"profile": "extreme"})
    assert resp.status_code == 422


async def test_random_sim_toggle_updates_status(client: httpx.AsyncClient):
    resp = await client.post("/api/random/sims/market", json={"enabled": False})
    assert resp.json()["sims"]["market"] is False


async def test_index_page_renders(client: httpx.AsyncClient):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "ogsim.control" in resp.text
