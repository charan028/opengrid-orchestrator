"""Integration tests for the ogsim.control FastAPI app over ASGI. httpx's
ASGITransport does not drive the lifespan protocol, so the random engine's
background loops never start here - only its REST-exposed state is
exercised, keeping these tests fast and deterministic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ogsim.control.app import BASE_PATH_ENV, create_app

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
RANDOM_CONFIG = Path(__file__).resolve().parents[1] / "config" / "random.yaml"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        scenarios_dir=SCENARIOS_DIR,
        random_config_path=RANDOM_CONFIG,
        # Demo gap #16: an explicit per-test path, so tests never share (or pollute each other via)
        # the real persisted-pause-state file.
        random_pause_state_path=tmp_path / "random_pause_state.json",
    )
    transport = httpx.ASGITransport(app=app)
    # R3.1 LOW-review fix, 2026-09-26: every state-changing POST/DELETE now requires the
    # X-OGSim-Request CSRF header (app.py's `_require_csrf_header`) -- sent here as a default header
    # so the tests above (which predate the CSRF check and cover unrelated behaviour) keep working
    # unchanged. The dedicated CSRF tests below build their own client without this default.
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control.test", headers={"X-OGSim-Request": "1"}
    ) as c:
        yield c


@pytest.fixture
async def client_no_csrf_header(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Same app, but with NO default CSRF header -- for tests proving the header is actually enforced."""
    app = create_app(
        scenarios_dir=SCENARIOS_DIR,
        random_config_path=RANDOM_CONFIG,
        random_pause_state_path=tmp_path / "random_pause_state.json",
    )
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


# ---- demo gap #17: absolute /api/... URLs break behind a reverse-proxy mount prefix -------------


async def test_index_never_calls_api_by_bare_absolute_path(client: httpx.AsyncClient):
    resp = await client.get("/")
    assert "fetch('/api" not in resp.text
    assert 'fetch("/api' not in resp.text


async def test_index_defaults_to_the_domain_root(client: httpx.AsyncClient):
    resp = await client.get("/")
    assert 'const API_BASE = "";' in resp.text


async def test_index_uses_x_forwarded_prefix_when_present(client: httpx.AsyncClient):
    resp = await client.get("/", headers={"X-Forwarded-Prefix": "/ogsim"})
    assert 'const API_BASE = "/ogsim";' in resp.text


async def test_index_falls_back_to_the_base_path_env_var(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(BASE_PATH_ENV, "/ogsim")
    resp = await client.get("/")
    assert 'const API_BASE = "/ogsim";' in resp.text


async def test_index_x_forwarded_prefix_wins_over_the_env_var(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(BASE_PATH_ENV, "/from-env")
    resp = await client.get("/", headers={"X-Forwarded-Prefix": "/from-header"})
    assert 'const API_BASE = "/from-header";' in resp.text


# ---- demo gap #18: no "stop scenario" verb ------------------------------------------------------


async def test_stopping_an_unknown_scenario_returns_404(client: httpx.AsyncClient):
    resp = await client.post("/api/scenarios/does-not-exist/stop")
    assert resp.status_code == 404


async def test_stop_scenario_ends_its_already_injected_anomalies(client: httpx.AsyncClient):
    run_resp = await client.post("/api/scenarios/tampered_command/run", json={"speed": 1_000_000.0})
    assert run_resp.status_code == 200
    for _ in range(50):  # let the background task actually inject (speed collapses the delay to ~0)
        await asyncio.sleep(0)

    active = (await client.get("/api/anomalies")).json()["active"]
    assert any(a["id"].startswith("tampered_command:") for a in active)

    stop_resp = await client.post("/api/scenarios/tampered_command/stop")
    assert stop_resp.status_code == 200
    body = stop_resp.json()
    assert body["ok"] is True
    assert any(aid.startswith("tampered_command:") for aid in body["anomalies_ended"])

    active_after = (await client.get("/api/anomalies")).json()["active"]
    assert not any(a["id"].startswith("tampered_command:") for a in active_after)


async def test_stop_all_scenarios_ends_every_scenario_anomaly(client: httpx.AsyncClient):
    await client.post("/api/scenarios/tampered_command/run", json={"speed": 1_000_000.0})
    for _ in range(50):
        await asyncio.sleep(0)

    resp = await client.post("/api/scenarios/stop-all")
    assert resp.status_code == 200
    stopped_names = {s["scenario"] for s in resp.json()["stopped"]}
    assert "tampered_command" in stopped_names

    active_after = (await client.get("/api/anomalies")).json()["active"]
    assert not any(a["id"].startswith("tampered_command:") for a in active_after)


async def test_stop_all_scenarios_is_a_no_op_when_nothing_is_running(client: httpx.AsyncClient):
    resp = await client.post("/api/scenarios/stop-all")
    assert resp.status_code == 200
    assert resp.json()["stopped"] == []


# ---- owner feedback, 2026-09-26: "when I select a scenario there's no apply" -- explicit UX --------


async def test_index_scenario_controls_use_api_base_not_bare_fetch(client: httpx.AsyncClient):
    resp = await client.get("/")
    text = resp.text
    assert "${API_BASE}/api/scenarios" in text
    assert "fetch('/api" not in text and 'fetch("/api' not in text


async def test_index_has_a_scenario_select_and_explicit_run_stop_buttons(client: httpx.AsyncClient):
    resp = await client.get("/")
    text = resp.text
    assert 'id="scenarioSelect"' in text
    assert 'id="runScenarioBtn"' in text
    assert 'id="stopScenarioBtn"' in text
    assert 'id="stopAllScenariosBtn"' in text
    assert 'id="scenarioStatus"' in text


async def test_index_has_an_explicit_apply_profile_button_not_a_silent_onchange(
    client: httpx.AsyncClient,
):
    resp = await client.get("/")
    text = resp.text
    assert 'id="applyProfileBtn"' in text
    # The profile <select> itself must not auto-apply on change (no inline onchange="setProfile()").
    assert 'id="profileSelect" style="width:auto;" onchange="setProfile()"' not in text


async def test_index_has_an_inject_button_and_a_toast_element(client: httpx.AsyncClient):
    resp = await client.get("/")
    text = resp.text
    assert 'id="injectBtn"' in text
    assert 'id="toast"' in text


async def test_index_has_visible_error_handling_for_scenario_load_failures(
    client: httpx.AsyncClient,
):
    resp = await client.get("/")
    text = resp.text
    assert 'id="scenarioError"' in text
    assert "could not load scenarios" in text


# ---- R3.1 LOW-review fix, 2026-09-26: state-changing POST/DELETE endpoints require a CSRF header ---


async def test_get_endpoints_do_not_require_the_csrf_header(client_no_csrf_header: httpx.AsyncClient):
    """Only state-changing requests are gated -- GET must keep working with no header at all."""
    resp = await client_no_csrf_header.get("/api/random/status")
    assert resp.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/api/inject", {"type": "price_spike", "target": "*", "duration": 60.0}),
        ("POST", "/api/scenarios/does-not-exist/run", {"speed": 1.0}),
        ("POST", "/api/scenarios/does-not-exist/stop", None),
        ("POST", "/api/scenarios/stop-all", None),
        ("POST", "/api/random/pause", None),
        ("POST", "/api/random/resume", None),
        ("POST", "/api/random/profile", {"profile": "chaos"}),
        ("POST", "/api/random/sims/market", {"enabled": False}),
        ("DELETE", "/api/anomalies/does-not-exist", None),
    ],
)
async def test_state_changing_endpoint_rejects_a_request_with_no_csrf_header(
    client_no_csrf_header: httpx.AsyncClient, method: str, path: str, json_body: dict | None
):
    resp = await client_no_csrf_header.request(method, path, json=json_body)
    assert resp.status_code == 403


async def test_state_changing_endpoint_rejects_the_wrong_csrf_header_value(
    client_no_csrf_header: httpx.AsyncClient,
):
    resp = await client_no_csrf_header.post("/api/random/pause", headers={"X-OGSim-Request": "yes-please"})
    assert resp.status_code == 403


async def test_state_changing_endpoint_accepts_the_correct_csrf_header(
    client_no_csrf_header: httpx.AsyncClient,
):
    resp = await client_no_csrf_header.post("/api/random/pause", headers={"X-OGSim-Request": "1"})
    assert resp.status_code == 200


async def test_control_page_fetch_helper_sends_the_csrf_header_on_non_get_requests(
    client: httpx.AsyncClient,
):
    """The page itself (templates/index.html's `fetchJson`) must send the header on every non-GET
    request, or every button on the deployed page would start 403ing the moment this ships."""
    resp = await client.get("/")
    text = resp.text
    assert "X-OGSim-Request" in text
    assert "method !== 'GET'" in text
