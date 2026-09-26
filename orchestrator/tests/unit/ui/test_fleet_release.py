"""Fleet screen: two-person safe-stop RELEASE (K8). Operator 1 requests, operator 2 reviews in a confirm
dialog and approves; the UI forwards each operator's own identity to the API and never assumes a release."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

import opengrid.ui.api_client as api_client

_ALICE = {"X-OG-Role": "operator", "X-Remote-User": "alice"}
_BOB = {"X-OG-Role": "operator", "X-Remote-User": "bob"}
_PID = "22222222-2222-2222-2222-222222222222"


def test_request_forwards_the_operator_and_shows_the_request_id(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api(
        {
            "/og/api/safestop/bank/bank-01/release": {
                "proposal_id": _PID,
                "summary": "Release safe stop on BANK:bank-01 (clear)",
                "expires_in_s": 60.0,
            }
        }
    )
    response = client.post(
        "/og/fleet/safestop/release/request",
        data={"scope": "bank", "scope_id": "bank-01", "reason": "clear"},
        headers=_ALICE,
    )
    assert response.status_code == 200
    assert "REQUESTED" in response.text and _PID in response.text
    assert fake_post_api.posted[-1]["remote_user"] == "alice"  # type: ignore[attr-defined]


def test_fleet_scope_without_an_id_uses_the_fleet_path(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({"/og/api/safestop/fleet/FLEET/release": {"proposal_id": _PID, "summary": "s"}})
    response = client.post(
        "/og/fleet/safestop/release/request",
        data={"scope": "fleet", "scope_id": "", "reason": "x"},
        headers=_ALICE,
    )
    assert "REQUESTED" in response.text


def test_review_opens_a_confirm_dialog_without_calling_the_api(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({})
    response = client.post("/og/fleet/safestop/release/review", data={"proposal_id": _PID}, headers=_BOB)
    assert response.status_code == 200
    assert f"/og/fleet/safestop/release/{_PID}/approve" in response.text
    assert fake_post_api.posted == []  # type: ignore[attr-defined]


def test_approve_renders_pending_released_and_refused(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    path = f"/og/api/safestop/release/{_PID}/approve"
    pending = {"released": False, "scope": "BANK", "scope_ref": "bank-01", "trace_id": "t1"}
    fake_post_api({path: pending})
    response = client.post(f"/og/fleet/safestop/release/{_PID}/approve", headers=_BOB)
    assert "PENDING" in response.text
    assert fake_post_api.posted[-1]["remote_user"] == "bob"  # type: ignore[attr-defined]

    fake_post_api({path: {**pending, "released": True}})
    assert "RELEASED" in client.post(f"/og/fleet/safestop/release/{_PID}/approve", headers=_BOB).text

    fake_post_api({path: api_client.ApiUnavailable("POST failed: 403", status_code=403)})
    assert "REFUSED" in client.post(f"/og/fleet/safestop/release/{_PID}/approve", headers=_ALICE).text


def test_release_routes_are_forbidden_for_a_viewer(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({})
    response = client.post(f"/og/fleet/safestop/release/{_PID}/approve")
    assert response.status_code == 403
