"""Degraded-mode labels and guardian escalations (health agent backend: `GET /og/api/health`
`degraded_modes`, ALR-SAFE-STOP-REQUESTED / ALR-SCOPE-CONSERVATIVE). Where they are shown -- the header
bell's popover, not above the map -- is test_notification_centre.py."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.fleet as fleet_route
from opengrid.ui.routes.fleet import safestop_prefill
from opengrid.ui.routes.health import (
    degraded_modes_of,
    guardian_attention,
)

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


def test_degraded_modes_of_tolerates_an_older_payload() -> None:
    assert degraded_modes_of({}) == []
    assert degraded_modes_of({"degraded_modes": None}) == []
    assert degraded_modes_of({"degraded_modes": ["HOLD", "HOLD", ""]}) == ["HOLD"]


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
