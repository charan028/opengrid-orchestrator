"""The shared alerts component (owner UX review, R3): grouping, filters, pagination, posture strip, and the
bulk acknowledgement (bulk API, sequential fallback), on both Control room and System Health."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.alerts as alerts_route
import opengrid.ui.routes.control_room as control_room_route
import opengrid.ui.routes.health as health_route
from opengrid.ui.alerts import alerts_panel, parse_ids, posture_strip
from opengrid.ui.api_client import ApiUnavailable

OP = {"X-Remote-User": "alice"}


def _alerts(n: int = 25) -> list[dict[str, Any]]:
    out = []
    for i in range(1, n + 1):
        bank = f"bank-{i % 3:03d}"  # three banks -> the conservative alerts collapse into 3 groups
        out.append(
            {
                "id": i,
                "rule": "ALR-SCOPE-CONSERVATIVE",
                "severity": "warning",
                "scope_kind": "BANK",
                "scope_ref": bank,
                "summary": f"BANK:{bank} CONSERVATIVE: 40% of commands vetoed",
                "opened_at": f"2026-09-26T17:{i:02d}:00+00:00",
                "acked_by": None,
            }
        )
    for i in range(n + 1, n + 31):
        out.append(
            {
                "id": i,
                "rule": "ALR-PROCESS-DOWN",
                "severity": "critical",
                "scope_kind": None,
                "scope_ref": None,
                "summary": f"Process p{i} heartbeat missing",
                "opened_at": f"2026-09-26T16:{i - n:02d}:00+00:00",
                "acked_by": "bob" if i == n + 1 else None,
            }
        )
    return out


def test_grouping_sorting_filtering_and_pages() -> None:
    panel = alerts_panel(_alerts(), size=20)
    assert panel["group_count"] == 3 + 30 and panel["alert_count"] == 55 and panel["pages"] == 2
    first = panel["groups"][0]
    assert (
        first["rule"] == "ALR-SCOPE-CONSERVATIVE"
        and first["count"] > 1
        and first["latest"] >= panel["groups"][1]["latest"]
    )
    critical = alerts_panel(_alerts(), severity="critical", size=10, page=3)
    assert critical["page"] == 3 and all(g["severity"] == "critical" for g in critical["groups"])
    assert alerts_panel(_alerts(), rule="ALR-SCOPE-CONSERVATIVE")["group_count"] == 3
    assert alerts_panel(_alerts(), page=99)["page"] == 2 and alerts_panel(_alerts(), size=7)["size"] == 20
    acked = next(
        g
        for g in alerts_panel(_alerts(), size=100)["groups"]
        if g["summary"] == "Process p26 heartbeat missing"
    )
    assert acked["ids_csv"] == ""  # already acknowledged: nothing to select
    assert alerts_panel(_alerts())["all_matching_count"] == 54


def test_posture_strip_only_while_conservative() -> None:
    assert posture_strip({"conservative": []}) is None and posture_strip(None) is None
    rows = [{"scope_kind": "BANK", "scope_ref": f"bank-{i:03d}", "stop_requested": i == 0} for i in range(9)]
    strip = posture_strip({"conservative": rows})
    assert strip == {"count": 9, "shown": [f"bank-{i:03d}" for i in range(6)], "more": 3, "stop_requested": 1}


def test_parse_ids() -> None:
    assert parse_ids("3,5, 9,x,3") == [3, 5, 9]


def _serve(
    monkeypatch: pytest.MonkeyPatch, alerts: list[dict[str, Any]], posture: Any, bulk: Any
) -> list[tuple[str, Any]]:
    posted: list[tuple[str, Any]] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/og/api/health":
            return {"alerts": alerts}
        if path == "/og/api/views/scope-posture":
            return posture
        raise ApiUnavailable("no fixture", status_code=404)

    async def fake_post_json(
        path: str, payload: dict[str, Any], *, remote_user: str | None = None, timeout_s: float | None = None
    ) -> Any:
        posted.append((path, payload))
        if path == "/og/api/alerts/ack-bulk":
            if isinstance(bulk, int):
                raise ApiUnavailable("x", status_code=bulk)
            return bulk
        return {"id": int(path.split("/")[-2]), "acked_by": remote_user}

    for module in (alerts_route, control_room_route, health_route):
        monkeypatch.setattr(module, "get_json", fake_get_json)
        if hasattr(module, "post_json"):
            monkeypatch.setattr(module, "post_json", fake_post_json)
    return posted


@pytest.mark.parametrize(("path", "pid"), [("/og/", "control-room"), ("/og/health", "health")])
def test_both_screens_render_the_same_component_and_no_stacked_notices(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, pid: str
) -> None:
    _serve(monkeypatch, _alerts(), {"conservative": [{"scope_kind": "BANK", "scope_ref": "bank-003"}]}, 404)
    html = client.get(path, headers=OP).text
    assert (
        f'id="{pid}-alerts-panel"' in html and f'id="{pid}-ack-form"' in html and "og-alerts-scroll" in html
    )
    # the posture lives in the header bell's popover now (test_notification_centre.py), not above the map
    assert "posture-strip" not in html and "Scope held conservative" not in html
    assert 'id="og-notify"' in html
    assert "&times;" in html and "Select all 54 matching filter" in html


def test_panel_fragment_filters_and_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _alerts(), None, 404)
    html = client.get(
        "/og/alerts/panel",
        params={"panel_id": "health", "alert_severity": "critical", "alert_page": 2, "alert_size": 10},
        headers=OP,
    ).text
    assert 'id="health-alerts-panel"' in html and "ALR-SCOPE-CONSERVATIVE</td>" not in html
    assert 'aria-current="page"' in html


def test_bulk_ack_is_one_confirm_then_the_bulk_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = _serve(
        monkeypatch, _alerts(), None, {"results": [{"alert_id": i, "status": "acked"} for i in (1, 4, 7)]}
    )
    dialog = client.post(
        "/og/alerts/ack-bulk/propose", data={"panel_id": "control-room", "sel": ["1,4", "7"]}, headers=OP
    ).text
    assert "Acknowledge 3 alerts" in dialog and "ids=1,4,7" in dialog and posted == []
    result = client.post(
        "/og/alerts/ack-bulk/confirm", params={"ids": "1,4,7", "panel_id": "control-room"}, headers=OP
    )
    assert "3 alerts acknowledged" in result.text and result.headers.get("HX-Trigger") == "og-alerts-changed"
    assert posted == [("/og/api/alerts/ack-bulk", {"alert_ids": [1, 4, 7]})]


def test_select_all_matching_uses_every_id_behind_the_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _alerts(), None, 404)
    dialog = client.post(
        "/og/alerts/ack-bulk/propose",
        data={"panel_id": "health", "all_matching": "1", "all_ids": "1,2,3,4,5"},
        headers=OP,
    ).text
    assert "Acknowledge 5 alerts" in dialog


def test_bulk_ack_falls_back_to_single_acks(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _serve(monkeypatch, _alerts(), None, 404)
    html = client.post(
        "/og/alerts/ack-bulk/confirm", params={"ids": "1,2", "panel_id": "health"}, headers=OP
    ).text
    assert "2 alerts acknowledged (one at a time" in html
    assert [p for p, _ in posted] == [
        "/og/api/alerts/ack-bulk",
        "/og/api/alerts/1/ack",
        "/og/api/alerts/2/ack",
    ]


@pytest.mark.parametrize("path", ["/og/alerts/ack-bulk/propose", "/og/alerts/ack-bulk/confirm?ids=1"])
def test_viewer_cannot_ack(client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    posted = _serve(monkeypatch, _alerts(), None, 404)
    assert client.post(path, data={"sel": ["1"]}, headers={"X-Remote-User": "carol"}).status_code == 403
    assert posted == []
