"""Route-level tests for the Fleet screen's two-step confirmations (BUILD.md code-review round items
1-3): scoped safe stop and manual command each drive a real UI-owned `.../propose` route, then a real
`.../{proposal_id}/confirm` route, with `opengrid.ui.api_client.post_json` mocked -- never by hand-
rendering `_partials/confirm_dialog.html` with a fabricated context dict."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import opengrid.ui.api_client as api_client

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

_OPERATOR = {"X-Remote-User": "alice"}


def _load(name: str) -> Any:
    with (_FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


# -- scoped safe stop ------------------------------------------------------------------------------


def test_propose_safestop_renders_real_proposal_as_an_open_confirm_dialog(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    proposal = _load("fleet_safestop_propose.json")
    fake_post_api({"/og/api/safestop": proposal})

    response = client.post(
        "/og/fleet/safestop/propose",
        data={"scope": "fleet", "scope_id": "", "reason": "planned maintenance"},
        headers=_OPERATOR,
    )

    assert response.status_code == 200
    body = response.text
    # the real summary/proposal_id/expires_in_s from the mocked API response, not a static string
    assert proposal["summary"] in body
    assert f"/og/fleet/safestop/{proposal['proposal_id']}/confirm" in body
    assert "remaining: 60.0" in body or "remaining: 60" in body
    # rendered already open (open_default=True, show_trigger=False): no separate trigger button
    assert "open: true" in body
    # the real Apache-authenticated identity is forwarded to the API, never left off
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_propose_safestop_rejects_a_spoofed_x_og_role_header(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    """A caller with no real operator identity must not gain access by setting `X-OG-Role` themselves --
    Apache's `/og/` fragment never sets or strips that header, so it must never be trusted here."""
    fake_post_api({"/og/api/safestop": _load("fleet_safestop_propose.json")})

    response = client.post(
        "/og/fleet/safestop/propose",
        data={"scope": "fleet", "scope_id": "", "reason": "planned maintenance"},
        headers={"X-OG-Role": "operator"},
    )

    assert response.status_code == 403
    assert fake_post_api.posted == []  # type: ignore[attr-defined]


def test_propose_safestop_renders_error_fragment_when_api_unavailable(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({})  # nothing registered -> ApiUnavailable

    response = client.post(
        "/og/fleet/safestop/propose",
        data={"scope": "fleet", "scope_id": "", "reason": "planned maintenance"},
        headers=_OPERATOR,
    )

    assert response.status_code == 200
    assert "UNAVAILABLE" in response.text


def test_propose_safestop_is_forbidden_for_a_viewer(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/safestop": _load("fleet_safestop_propose.json")})

    response = client.post(
        "/og/fleet/safestop/propose", data={"scope": "fleet", "scope_id": "", "reason": "x"}
    )

    assert response.status_code == 403


def test_confirm_safestop_renders_engaged_result(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    result = _load("fleet_safestop_confirm.json")
    fake_post_api({"/og/api/safestop/11111111-1111-1111-1111-111111111111/confirm": result})

    response = client.post(
        "/og/fleet/safestop/11111111-1111-1111-1111-111111111111/confirm", headers=_OPERATOR
    )

    assert response.status_code == 200
    body = response.text
    assert "ENGAGED" in body
    assert result["trace_id"] in body
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_confirm_safestop_rejects_a_spoofed_x_og_role_header(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    result = _load("fleet_safestop_confirm.json")
    fake_post_api({"/og/api/safestop/11111111-1111-1111-1111-111111111111/confirm": result})

    response = client.post(
        "/og/fleet/safestop/11111111-1111-1111-1111-111111111111/confirm",
        headers={"X-OG-Role": "operator"},
    )

    assert response.status_code == 403
    assert fake_post_api.posted == []  # type: ignore[attr-defined]


def test_confirm_safestop_renders_timeout_result_on_503(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    timeout = api_client.ApiUnavailable("POST failed: 503", status_code=503, detail=None)
    fake_post_api({"/og/api/safestop/aaaa/confirm": timeout})

    response = client.post("/og/fleet/safestop/aaaa/confirm", headers=_OPERATOR)

    assert response.status_code == 200
    assert "TIMEOUT" in response.text


def test_confirm_safestop_renders_expired_result_on_410(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    expired = api_client.ApiUnavailable("POST failed: 410", status_code=410, detail=None)
    fake_post_api({"/og/api/safestop/bbbb/confirm": expired})

    response = client.post("/og/fleet/safestop/bbbb/confirm", headers=_OPERATOR)

    assert response.status_code == 200
    assert "EXPIRED" in response.text


# -- manual command ---------------------------------------------------------------------------------


def test_propose_command_renders_real_proposal_as_an_open_confirm_dialog(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    proposal = _load("fleet_command_propose.json")
    fake_post_api({"/og/api/fleet/command": proposal})

    response = client.post(
        "/og/fleet/command/propose",
        data={"bank_id": "bank-01", "hub_id": "", "p_kw_setpoint": "5.0", "reason": "load test"},
        headers=_OPERATOR,
    )

    assert response.status_code == 200
    body = response.text
    assert proposal["summary"] in body
    assert f"/og/fleet/command/{proposal['proposal_id']}/confirm" in body
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_propose_command_rejects_a_spoofed_x_og_role_header(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/fleet/command": _load("fleet_command_propose.json")})

    response = client.post(
        "/og/fleet/command/propose",
        data={"bank_id": "bank-01", "hub_id": "", "p_kw_setpoint": "5.0", "reason": "load test"},
        headers={"X-OG-Role": "operator"},
    )

    assert response.status_code == 403
    assert fake_post_api.posted == []  # type: ignore[attr-defined]


def test_propose_command_requires_bank_or_hub_id(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/fleet/command": _load("fleet_command_propose.json")})

    response = client.post(
        "/og/fleet/command/propose",
        data={"bank_id": "", "hub_id": "", "p_kw_setpoint": "5.0", "reason": "load test"},
        headers=_OPERATOR,
    )

    assert response.status_code == 200
    assert "bank id or hub id is required" in response.text


def test_confirm_command_renders_pass_result(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    result = _load("fleet_command_confirm_pass.json")
    fake_post_api({"/og/api/fleet/command/33333333-3333-3333-3333-333333333333/confirm": result})

    response = client.post(
        "/og/fleet/command/33333333-3333-3333-3333-333333333333/confirm", headers=_OPERATOR
    )

    assert response.status_code == 200
    body = response.text
    assert "PASS" in body
    assert result["trace_id"] in body
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_confirm_command_renders_veto_result_on_409(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    veto_body = {
        "proposal_id": "33333333-3333-3333-3333-333333333333",
        "outcome": "VETOED",
        "vetoed_rule_ids": ["R-RESERVE-FLOOR"],
        "trace_id": "55555555-5555-5555-5555-555555555555",
    }
    veto = api_client.ApiUnavailable("POST failed: 409", status_code=409, detail=veto_body)
    fake_post_api({"/og/api/fleet/command/cccc/confirm": veto})

    response = client.post("/og/fleet/command/cccc/confirm", headers=_OPERATOR)

    assert response.status_code == 200
    body = response.text
    assert "VETOED" in body
    assert "R-RESERVE-FLOOR" in body


def test_confirm_command_is_forbidden_for_a_viewer(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/fleet/command/x/confirm": _load("fleet_command_confirm_pass.json")})

    response = client.post("/og/fleet/command/x/confirm")

    assert response.status_code == 403


# -- alert ack (Health + Control room) --------------------------------------------------------------


def test_health_ack_alert_relays_the_real_path_param_endpoint(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    alert = _load("alert_ack.json")
    fake_post_api({"/og/api/alerts/7/ack": alert})

    response = client.post("/og/health/alerts/ack", data={"alert_id": "7"}, headers=_OPERATOR)

    assert response.status_code == 200
    body = response.text
    assert "ACKED" in body
    assert alert["acked_by"] in body
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_control_room_ack_alert_relays_the_real_path_param_endpoint(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    alert = _load("alert_ack.json")
    fake_post_api({"/og/api/alerts/7/ack": alert})

    response = client.post("/og/alerts/ack", data={"alert_id": "7"}, headers=_OPERATOR)

    assert response.status_code == 200
    assert "ACKED" in response.text
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_ack_alert_is_forbidden_for_a_viewer(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/alerts/7/ack": _load("alert_ack.json")})

    response = client.post("/og/health/alerts/ack", data={"alert_id": "7"})

    assert response.status_code == 403


def test_ack_alert_rejects_a_spoofed_x_og_role_header(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    """Covers both the Health and Control room copies of the ack action (BUILD.md code-review round
    item 5): a client-supplied `X-OG-Role` must not stand in for the missing real identity."""
    fake_post_api({"/og/api/alerts/7/ack": _load("alert_ack.json")})

    for path in ("/og/health/alerts/ack", "/og/alerts/ack"):
        response = client.post(path, data={"alert_id": "7"}, headers={"X-OG-Role": "operator"})
        assert response.status_code == 403

    assert fake_post_api.posted == []  # type: ignore[attr-defined]
