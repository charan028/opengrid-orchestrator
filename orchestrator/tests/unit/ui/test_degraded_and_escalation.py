"""Degraded-mode banner and guardian escalations on Health and Control room (health agent backend:
`GET /og/api/health` `degraded_modes`, ALR-SAFE-STOP-REQUESTED / ALR-SCOPE-CONSERVATIVE)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.control_room as control_room_route
import opengrid.ui.routes.fleet as fleet_route
import opengrid.ui.routes.health as health_route
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.fleet import safestop_prefill
from opengrid.ui.routes.health import (
    degraded_mode_banner_text,
    degraded_modes_of,
    guardian_attention,
)

from .conftest import load_fixture

STOP_ALERT = {
    "id": 41,
    "rule": "ALR-SAFE-STOP-REQUESTED",
    "severity": "critical",
    "summary": "BANK:bank-007: 3 consecutive CONSERVATIVE ticks (40% vetoed): safe stop requested",
    "opened_at": "2026-09-26T12:00:00+00:00",
}
CONSERVATIVE_ALERT = {
    "id": 40,
    "rule": "ALR-SCOPE-CONSERVATIVE",
    "severity": "warning",
    "summary": "ZONE:LZ_NORTH CONSERVATIVE: 12% of commands vetoed",
    "opened_at": "2026-09-26T11:59:00+00:00",
}


def _health(**extra: Any) -> dict[str, Any]:
    return {**load_fixture("health.json"), **extra}


def _serve(monkeypatch: pytest.MonkeyPatch, health: dict[str, Any]) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/health":
            return health
        if path == "/og/api/fleet/hubs":
            return {"items": []}
        raise ApiUnavailable(f"no fixture for {path}")

    monkeypatch.setattr(health_route, "get_json", fake_get_json)
    monkeypatch.setattr(control_room_route, "get_json", fake_get_json)


def test_banner_text_joins_labels_and_keeps_unknown_codes() -> None:
    assert degraded_mode_banner_text([]) is None
    assert degraded_mode_banner_text(["NO_NEW_COMMITMENTS", "HOLD"]) == "Feed stale + Guardian down"
    assert degraded_mode_banner_text(["HOLD_LOCAL_AUTONOMY", "DIST_DEFERRAL_OPEN_LOOP"]) == (
        "Engine down + SCADA silent"
    )
    assert degraded_mode_banner_text(["SOMETHING_NEW"]) == "SOMETHING_NEW"


def test_degraded_modes_of_tolerates_an_older_payload() -> None:
    assert degraded_modes_of({}) == []
    assert degraded_modes_of({"degraded_modes": None}) == []
    assert degraded_modes_of({"degraded_modes": ["HOLD", "HOLD", ""]}) == ["HOLD"]


@pytest.mark.parametrize(("path", "banner_id"), [("/og/health", "health"), ("/og/", "control-room")])
def test_banner_shows_the_active_modes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, banner_id: str
) -> None:
    _serve(monkeypatch, _health(degraded_modes=["NO_NEW_COMMITMENTS", "HOLD"]))
    body = client.get(path, headers={"X-Remote-User": "viewer"}).text
    banner = re.search(
        rf'<div class="og-degraded-banner" id="{banner_id}-degraded-banner" role="alert"[^>]*>([^<]*)<', body
    )
    assert banner is not None
    assert " hidden" not in banner.group(0)
    assert banner.group(1) == "Degraded mode: Feed stale + Guardian down"


@pytest.mark.parametrize(("path", "banner_id"), [("/og/health", "health"), ("/og/", "control-room")])
def test_banner_is_hidden_when_nothing_is_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, banner_id: str
) -> None:
    _serve(monkeypatch, _health(degraded_modes=[]))
    body = client.get(path, headers={"X-Remote-User": "viewer"}).text
    banner = re.search(rf'<div class="og-degraded-banner" id="{banner_id}-degraded-banner"[^>]*>', body)
    assert banner is not None and " hidden>" in banner.group(0)
    assert "Degraded mode:" not in body
    assert "Guardian escalations" not in body


def test_guardian_attention_orders_stop_requests_first_and_links_the_two_step_flow() -> None:
    items = guardian_attention([CONSERVATIVE_ALERT, {"rule": "ALR-PROCESS-DOWN"}, STOP_ALERT])
    # only a safe-stop REQUEST is an escalation item now; conservative scopes are the posture strip's job
    assert [i["rule"] for i in items] == ["ALR-SAFE-STOP-REQUESTED"]
    (stop,) = items
    assert stop["scope_label"] == "bank bank-007"
    assert (
        stop["review_url"] == "/og/fleet?safestop_scope=bank&safestop_scope_id=bank-007#safestop-propose-form"
    )
    assert "confirm" not in stop["review_url"]


def test_unparseable_stop_request_still_links_to_the_unfilled_form() -> None:
    (item,) = guardian_attention([{**STOP_ALERT, "summary": "safe stop requested"}])
    assert item["review_url"] == "/og/fleet#safestop-propose-form"
    assert item["scope_label"] is None


@pytest.mark.parametrize(("path", "prefix"), [("/og/health", "health"), ("/og/", "control-room")])
def test_operator_sees_escalations_with_a_review_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, prefix: str
) -> None:
    _serve(monkeypatch, _health(alerts=[CONSERVATIVE_ALERT, STOP_ALERT]))
    body = client.get(path, headers={"X-Remote-User": "operator"}).text
    assert f'id="{prefix}-guardian-attention"' in body
    assert "Guardian requests a safe stop: bank bank-007" in body
    assert "Scope held conservative" not in body  # summarised by the posture strip instead
    assert 'href="/og/fleet?safestop_scope=bank&amp;safestop_scope_id=bank-007#safestop-propose-form"' in body
    assert "/safestop/" not in body.split(f'id="{prefix}-guardian-attention"')[1].split("</section>")[0]


def test_viewer_sees_escalations_without_an_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _health(alerts=[STOP_ALERT]))
    body = client.get("/og/health", headers={"X-Remote-User": "viewer"}).text
    assert "Guardian requests a safe stop: bank bank-007" in body
    assert "Review safe stop" not in body


def test_safestop_prefill_only_fills_the_step_one_form() -> None:
    assert safestop_prefill(None, None) is None
    assert safestop_prefill("everything", "x") is None
    assert safestop_prefill("bank", "bank-007") == {
        "scope": "bank",
        "scope_id": "bank-007",
        "reason": "Guardian escalation: safe stop requested",
    }
    assert safestop_prefill("fleet", "ignored")["scope_id"] == ""  # type: ignore[index]


def test_fleet_form_is_prefilled_but_nothing_is_proposed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        return {"items": []}

    async def fake_post_json(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        posted.append(path)
        return {}

    monkeypatch.setattr(fleet_route, "get_json", fake_get_json)
    monkeypatch.setattr(fleet_route, "post_json", fake_post_json)
    body = client.get(
        "/og/fleet",
        params={"safestop_scope": "bank", "safestop_scope_id": "bank-007"},
        headers={"X-Remote-User": "operator"},
    ).text
    assert '<option value="bank" selected>Bank</option>' in body
    assert 'name="scope_id" value="bank-007"' in body
    assert 'id="safestop-prefill-note"' in body
    assert posted == []
