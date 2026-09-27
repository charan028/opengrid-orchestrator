"""r3.4: telemetry-age thresholds from `[health]` config, and 0042 dropping the HOT-killing indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import opengrid.ui as ui
from opengrid.platform.config import Config

from .conftest import PROXY_HEADERS

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


def _client(monkeypatch: Any, cfg: Config) -> TestClient:
    import opengrid.ui.routes.fleet as fleet_route

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/fleet/table":
            return {"items": [{"hub_id": "hub-1", "last_seen_at": "2026-09-26T12:00:00+00:00"}]}
        if path == "/og/api/fleet/hubs/hub-1":
            return {"hub_id": "hub-1", "last_seen_at": "2026-09-26T12:00:00+00:00"}
        raise fleet_route.ApiUnavailable(path)

    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    app = FastAPI()
    app.state.config = cfg
    app.include_router(ui.build_router(), prefix="/og")
    return TestClient(app, headers=PROXY_HEADERS)


def test_age_badges_use_the_configured_hub_stale_s(monkeypatch: Any) -> None:
    cfg = Config({"health": {"hub_stale_s": 25, "hub_offline_s": 60}, "api": {"roles": {}}})
    client = _client(monkeypatch, cfg)
    page = client.get("/og/fleet").text
    assert 'data-stale-after="25' in page
    assert 'data-stale-after="10' not in page
    drawer = client.get("/og/fleet/hubs/hub-1").text
    assert 'data-stale-after="25' in drawer and 'data-stale-after="6"' not in drawer


def test_0042_drops_both_hub_state_indexes_safely() -> None:
    sql = (MIGRATIONS / "0042_drop_hot_indexes.sql").read_text()
    assert "SET lock_timeout = '5s';" in sql
    assert "DROP INDEX IF EXISTS og.hub_state_last_seen_hub_idx;" in sql
    assert "DROP INDEX IF EXISTS og.hub_state_p_kw_hub_idx;" in sql


def test_only_active_targets_are_markers() -> None:
    from opengrid.ui.routes.fleet import target_is_active

    assert target_is_active({"status": "ACTIVE"}) and target_is_active({})
    for status in ("CANCELLED_BY_OPERATOR", "CANCELLED_BY_SAFE_STOP", "CANCELLED_LATE_RECORD", "EXPIRED"):
        assert not target_is_active({"status": status})


def _post_client(monkeypatch: Any, exc: Exception) -> TestClient:
    import opengrid.ui.routes.fleet as fleet_route

    async def fake_post_json(path: str, payload: dict[str, Any], **_: Any) -> Any:
        raise exc

    monkeypatch.setattr(fleet_route, "post_json", fake_post_json)
    app = FastAPI()
    app.state.config = Config({"api": {"roles": {"operator": ["alice"]}}})
    app.include_router(ui.build_router(), prefix="/og")
    return TestClient(app, headers={**PROXY_HEADERS, "X-Remote-User": "alice"})


def test_cancel_of_an_ended_target_says_why(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    ended = ApiUnavailable(
        "409",
        status_code=409,
        detail={"detail": {"message": "not active", "status": ["CANCELLED_BY_SAFE_STOP"]}},
    )
    body = _post_client(monkeypatch, ended).post("/og/fleet/manual-targets/t-1/cancel").text
    assert "ENDED" in body and "cancelled by safe stop" in body


def test_cancel_not_recorded_offers_a_retry(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    body = (
        _post_client(monkeypatch, ApiUnavailable("503", status_code=503))
        .post("/og/fleet/manual-targets/t-1/cancel")
        .text
    )
    assert "NOT RECORDED" in body and "Retry cancel" in body and "/og/fleet/manual-targets/t-1/cancel" in body


def test_confirm_not_recorded_is_a_retryable_failure(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    body = (
        _post_client(monkeypatch, ApiUnavailable("503", status_code=503))
        .post("/og/fleet/command/33333333-3333-3333-3333-333333333333/confirm")
        .text
    )
    assert "NOT RECORDED" in body and "nothing is ramping" in body
