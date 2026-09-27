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


def test_safestop_confirm_after_the_window_reads_expired(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    body = (
        _post_client(monkeypatch, ApiUnavailable("409", status_code=409))
        .post("/og/fleet/safestop/11111111-1111-1111-1111-111111111111/confirm")
        .text
    )
    assert "EXPIRED" in body and "FAILED" not in body


def test_bulk_propose_over_the_cap_is_refused_clearly(monkeypatch: Any) -> None:
    import opengrid.ui.routes.fleet as fleet_route

    async def fake_get_json(path: str, *, params: Any = None) -> Any:
        return {"items": []}

    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    client = _post_client(monkeypatch, AssertionError("must not post"))
    client.app.state.config = Config(
        {"api": {"roles": {"operator": ["alice"]}, "bulk_commands": {"max_hubs": 3}}}
    )
    body = client.post(
        "/og/fleet/command/bulk/propose",
        data={"hub_ids": "h1,h2,h3,h4", "p_kw_setpoint": "1", "reason": "x"},
    ).text
    assert "at most 3" in body


def test_live_power_reads_the_map_snapshot(monkeypatch: Any) -> None:
    import opengrid.ui.routes.fleet as fleet_route

    async def fake_get_json(path: str, *, params: Any = None) -> Any:
        assert path == "/og/api/fleet/map"
        return {"hubs": [{"hub_id": "h1", "kw": -2.5, "last_seen_at": "t"}, {"hub_id": "h9", "kw": 1}]}

    client = _client(monkeypatch, Config({"api": {"roles": {}}}))
    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    body = client.get("/og/fleet/live-power?ids=h1,h2").json()
    assert body == {"hubs": {"h1": {"p_kw": -2.5, "last_seen_at": "t"}}}


def test_feed_quality_is_stale_by_age() -> None:
    from opengrid.ui.routes.health import feed_quality

    old = {
        "source": "ERCOT",
        "product": "np6-905-cd",
        "quality": "GOOD",
        "last_value_at": "2020-01-01T00:00:00+00:00",
    }
    assert feed_quality(old, {}) == "STALE"
    assert feed_quality(old, {"ercot_price_fresh_s": 10**10}) == "GOOD"
    assert feed_quality({**old, "last_value_at": None}, {}) == "GOOD"


def test_tolling_obligations_are_utility_calls_capped_at_90() -> None:
    from datetime import UTC, datetime

    from opengrid.ui.routes.dispatch import as_awards_view

    rows = as_awards_view(
        [
            {
                "obligation_id": "t1",
                "contract_id": "c1",
                "service_type": "REGULATED_CAPACITY",
                "state": "COMMITTED",
            },
            {
                "obligation_id": "r1",
                "contract_id": "c2",
                "service_type": "REGULATED_CAPACITY",
                "state": "COMMITTED",
            },
        ],
        [],
        now=datetime.now(UTC),
        product_by_contract={"c1": "TOLLING", "c2": "CAPACITY"},
    )
    assert [(r["obligation_id"], r["utility_call"], r["max_minutes"]) for r in rows] == [("t1", True, 90)]


def test_control_room_tiles_age_from_the_data() -> None:
    text = (Path(__file__).resolve().parents[3] / "src/opengrid/ui/templates/control_room.html").read_text()
    assert "health.invariants_checked_at or health.as_of" in text
    assert 'setSince("kpi-fleet-mw", data.as_of)' in text


def test_unavailable_hub_badge_filter_and_chip(monkeypatch: Any) -> None:
    """D-37: the owner's badge (with the tooltip text) on the row, an Availability filter and its chip."""
    import opengrid.ui.routes.fleet as fleet_route
    from opengrid.market.availability import availability_fields

    row = {
        "hub_id": "hub-9",
        "last_seen_at": "2026-09-26T12:00:00+00:00",
        **availability_fields("UNAVAILABLE", "REGULATED_NO_CONTRACT"),
    }

    async def fake_get_json(path: str, *, params: Any = None) -> Any:
        if path == "/og/api/fleet/table":
            return {"items": [row]}
        raise fleet_route.ApiUnavailable(path)

    client = _client(monkeypatch, Config({"api": {"roles": {}}}))
    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    page = client.get("/og/fleet?availability=UNAVAILABLE").text
    assert 'class="fl-unavail" title="Unavailable: regulated (NOIE) territory' in page
    assert "Regulated market" in page and 'name="availability" value="UNAVAILABLE" checked' in page
    assert "Availability: Regulated market" in page


def test_confirm_outcome_unknown_is_distinct_from_not_recorded(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    unknown = ApiUnavailable(
        "503",
        status_code=503,
        detail={
            "detail": "manual target outcome unknown (the trace store failed and could not be re-checked)"
        },
    )
    body = (
        _post_client(monkeypatch, unknown)
        .post("/og/fleet/command/33333333-3333-3333-3333-333333333333/confirm")
        .text
    )
    assert "OUTCOME UNKNOWN" in body and "check the target list before retrying" in body
    assert "NOT RECORDED" not in body
    plain = ApiUnavailable("503", status_code=503, detail={"detail": "manual target not recorded"})
    body = (
        _post_client(monkeypatch, plain)
        .post("/og/fleet/command/33333333-3333-3333-3333-333333333333/confirm")
        .text
    )
    assert "NOT RECORDED" in body and "OUTCOME UNKNOWN" not in body


def test_cancel_outcome_unknown(monkeypatch: Any) -> None:
    from opengrid.ui.api_client import ApiUnavailable

    unknown = ApiUnavailable("503", status_code=503, detail={"detail": "outcome unknown"})
    body = _post_client(monkeypatch, unknown).post("/og/fleet/manual-targets/t-1/cancel").text
    assert "OUTCOME UNKNOWN" in body and "NOT RECORDED" not in body
