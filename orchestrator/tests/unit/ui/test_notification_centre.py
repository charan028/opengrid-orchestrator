"""Notification centre (owner UX review R3.1): every notice that used to stack above the map -- safe-stop
requests, the conservative-scope posture, degraded modes, alerts -- lives behind the header bell; at most one
critical line stays at the top, and only for an active critical state."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.alerts as alerts_route
import opengrid.ui.routes.control_room as control_room_route
import opengrid.ui.routes.health as health_route
from opengrid.ui.alerts import notification_centre
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.health import DEGRADED_MODE_LABELS, guardian_attention

from .conftest import load_fixture

OP = {"X-Remote-User": "operator"}
VIEWER = {"X-Remote-User": "viewer"}
STOP = {
    "id": 41,
    "rule": "ALR-SAFE-STOP-REQUESTED",
    "severity": "critical",
    "summary": "BANK:bank-007: 3 consecutive CONSERVATIVE ticks (40% vetoed): safe stop requested",
    "opened_at": "2026-09-26T12:00:00+00:00",
}
CONSERVATIVE = {
    "id": 40,
    "rule": "ALR-SCOPE-CONSERVATIVE",
    "severity": "warning",
    "scope_kind": "BANK",
    "scope_ref": "bank-003",
    "summary": "BANK:bank-003 CONSERVATIVE: 12% vetoed",
    "opened_at": "2026-09-26T11:59:00+00:00",
}
PROCESS = [
    {
        "id": 100 + i,
        "rule": "ALR-PROCESS-DOWN",
        "severity": "critical" if i % 2 else "warning",
        "summary": f"Process p{i} heartbeat missing",
        "opened_at": f"2026-09-26T10:{i:02d}:00+00:00",
        "acked_by": "op" if i == 9 else None,
    }
    for i in range(1, 12)
]
POSTURE = {"conservative": [{"scope_kind": "BANK", "scope_ref": f"bank-00{i}"} for i in range(3, 10)]}


def _centre(**kw: Any) -> dict[str, Any]:
    alerts = kw.pop("alerts", [])
    args: dict[str, Any] = {
        "guardian_items": guardian_attention(alerts),
        "degraded_modes": [],
        "mode_labels": DEGRADED_MODE_LABELS,
        "posture": None,
        "alerts": alerts,
        "base_path": "/og",
    }
    args.update(kw)
    return notification_centre(**args)


def test_nothing_to_report() -> None:
    nc = _centre()
    assert nc["count"] == 0 and nc["top_severity"] is None and nc["critical"] is None


def test_groups_severity_and_the_single_critical_line() -> None:
    posture = {"count": 7, "shown": ["bank-003", "bank-004"], "more": 5, "stop_requested": 0}
    nc = _centre(
        alerts=[STOP, CONSERVATIVE, *PROCESS], degraded_modes=["NO_NEW_COMMITMENTS", "HOLD"], posture=posture
    )
    safety, degraded, alerts = nc["groups"]
    assert [g["key"] for g in nc["groups"]] == ["safety", "degraded", "alerts"]
    assert safety["entries"][0]["title"] == "Guardian requests a safe stop: bank bank-007"
    assert (
        safety["entries"][0]["href"]
        == "/og/fleet?safestop_scope=bank&safestop_scope_id=bank-007#safestop-propose-form"
    )
    assert safety["entries"][1]["title"] == "7 scopes held conservative"
    assert "bank-003, bank-004, +5" in safety["entries"][1]["detail"]
    assert [i["title"] for i in degraded["entries"]] == [
        "Degraded mode: Feed stale",
        "Degraded mode: Guardian down",
    ]
    assert [i["severity"] for i in degraded["entries"]] == ["warning", "critical"]
    # conservative/safe-stop alerts are not repeated under Alerts; acked ones are not listed; critical first
    assert all(i["title"].startswith("ALR-PROCESS-DOWN") for i in alerts["entries"])
    assert alerts["total"] == 10 and len(alerts["entries"]) == 8 and alerts["more"] == 2
    assert [i["severity"] for i in alerts["entries"]][:5] == ["critical"] * 5
    assert nc["count"] == 2 + 2 + 10 and nc["top_severity"] == "critical"
    assert nc["critical"] == {
        "title": "Guardian requests a safe stop: bank bank-007",
        "href": safety["entries"][0]["href"],
        "more": 1,  # the HOLD degraded mode
    }


def test_warnings_only_never_produce_the_critical_line() -> None:
    nc = _centre(alerts=[CONSERVATIVE], degraded_modes=["NO_NEW_COMMITMENTS"])
    assert nc["top_severity"] == "warning" and nc["critical"] is None


def _serve(monkeypatch: pytest.MonkeyPatch, health: Any, posture: Any) -> None:
    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/health":
            if isinstance(health, Exception):
                raise health
            return health
        if path == "/og/api/views/scope-posture":
            return posture
        if path == "/og/api/fleet/hubs":
            return {"items": []}
        raise ApiUnavailable(f"no fixture for {path}", status_code=404)

    for module in (alerts_route, control_room_route, health_route):
        monkeypatch.setattr(module, "get_json", fake_get_json)


def test_fragment_bell_popover_and_critical_line(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    health = {
        **load_fixture("health.json"),
        "alerts": [STOP, *PROCESS],
        "degraded_modes": ["NO_NEW_COMMITMENTS"],
    }
    _serve(monkeypatch, health, POSTURE)
    html = client.get("/og/alerts/notifications", headers=OP).text
    bell = re.search(
        r'<button type="button" class="og-notify-bell sev-critical" id="og-notify-bell"[^>]*>', html
    )
    assert bell is not None and 'popovertarget="og-notify-panel"' in bell.group(0)
    assert 'data-count="13"' in html and ">13</span>" in html
    assert 'id="og-notify-panel" class="og-notify-panel" popover role="dialog"' in html
    for key in ("safety", "degraded", "alerts"):
        assert f'id="og-notify-{key}"' in html
    assert "Review (two-step)" in html and "/safestop/" not in html  # a link to step one only (K8)
    assert "/og/alerts/ack-bulk/confirm?panel_id=notify&ids=" in html
    critical = html.split('<template id="og-critical-next">')[1].split("</template>")[0]
    assert "Guardian requests a safe stop: bank bank-007" in critical and ">View</a>" in critical


def test_viewer_gets_links_but_no_actions(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"alerts": [STOP, *PROCESS]}, None)
    html = client.get("/og/alerts/notifications", headers=VIEWER).text
    assert "Guardian requests a safe stop" in html
    assert "Review (two-step)" not in html and "ack-bulk" not in html


def test_api_down_says_so_instead_of_all_clear(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, ApiUnavailable("connect refused"), None)
    html = client.get("/og/alerts/notifications", headers=OP).text
    assert "Status unavailable" in html and "Nothing needs attention" not in html


def test_notify_panel_id_is_accepted_for_acks(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []

    async def fake_post_json(path: str, payload: dict[str, Any], **_: Any) -> Any:
        posted.append(path)
        return {"results": [{"alert_id": 101, "status": "acked"}]}

    monkeypatch.setattr(alerts_route, "post_json", fake_post_json)
    resp = client.post("/og/alerts/ack-bulk/confirm", params={"ids": "101", "panel_id": "notify"}, headers=OP)
    assert 'id="notify-ack-result"' in resp.text and "1 alert acknowledged" in resp.text
    assert resp.headers.get("HX-Trigger") == "og-alerts-changed" and posted == ["/og/api/alerts/ack-bulk"]


@pytest.mark.parametrize("path", ["/og/", "/og/health"])
def test_no_notices_stack_above_the_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    health = {**load_fixture("health.json"), "alerts": [STOP, CONSERVATIVE], "degraded_modes": ["HOLD"]}
    _serve(monkeypatch, health, POSTURE)
    html = client.get(path, headers=OP).text
    for gone in ("og-degraded-banner", "guardian-attention", "posture-strip", "Guardian escalations"):
        assert gone not in html
    assert 'id="og-notify"' in html and 'hx-get="/og/alerts/notifications"' in html
    assert re.search(r'id="og-critical-strip" aria-live="assertive"', html)
