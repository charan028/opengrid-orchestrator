"""AS deployment control on Dispatch (PR #18): a two-step flow in the UI over the single-step operator API.
Step 1 renders the summary and changes nothing; step 2 posts once, with the proxy-verified identity."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.dispatch as dispatch_route

FORM = {"obligation_id": "obl-1", "duration_minutes": "30", "reason": "ERCOT deployment instruction"}


@pytest.fixture
def api_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any] | None, str | None]]:
    calls: list[tuple[str, str, dict[str, Any] | None, str | None]] = []

    async def fake_post_json(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        calls.append(("POST", path, payload, remote_user))
        return {"deployment_id": "dep-1"}

    async def fake_delete_json(path: str, *, remote_user: str | None = None) -> Any:
        calls.append(("DELETE", path, None, remote_user))
        return {"deployment_id": "dep-1"}

    monkeypatch.setattr(dispatch_route, "post_json", fake_post_json)
    monkeypatch.setattr(dispatch_route, "delete_json", fake_delete_json)
    return calls


def test_propose_only_renders_the_summary(client: TestClient, api_calls: list[Any]) -> None:
    resp = client.post("/og/dispatch/as-deployments/propose", data=FORM, headers={"X-Remote-User": "alice"})
    assert resp.status_code == 200
    assert "Deploy award obl-1 (ERCOT_AS) for 30 minutes (ERCOT deployment instruction)" in resp.text
    assert 'hx-post="/og/dispatch/as-deployments/confirm"' in resp.text
    assert api_calls == []


def test_confirm_posts_once_with_the_verified_identity(client: TestClient, api_calls: list[Any]) -> None:
    resp = client.post("/og/dispatch/as-deployments/confirm", data=FORM, headers={"X-Remote-User": "alice"})
    assert resp.status_code == 200
    assert "Deployment dep-1 is active." in resp.text
    assert 'id="as-action-result"' not in resp.text  # swapped into that container; must not nest a second id
    assert api_calls == [
        (
            "POST",
            "/og/api/dispatch/as-deployments",
            {"obligation_id": "obl-1", "duration_minutes": 30, "reason": "ERCOT deployment instruction"},
            "alice",
        )
    ]


def test_stop_is_two_step_too(client: TestClient, api_calls: list[Any]) -> None:
    proposed = client.post(
        "/og/dispatch/as-deployments/dep-1/stop-propose", headers={"X-Remote-User": "alice"}
    )
    assert proposed.status_code == 200 and api_calls == []
    stopped = client.post(
        "/og/dispatch/as-deployments/dep-1/stop-confirm", headers={"X-Remote-User": "alice"}
    )
    assert stopped.status_code == 200
    assert api_calls == [("DELETE", "/og/api/dispatch/as-deployments/dep-1", None, "alice")]


@pytest.mark.parametrize(
    "path",
    [
        "/og/dispatch/as-deployments/propose",
        "/og/dispatch/as-deployments/confirm",
        "/og/dispatch/as-deployments/dep-1/stop-propose",
        "/og/dispatch/as-deployments/dep-1/stop-confirm",
    ],
)
def test_viewer_and_spoofed_identity_are_refused(client: TestClient, api_calls: list[Any], path: str) -> None:
    assert client.post(path, data=FORM, headers={"X-Remote-User": "carol"}).status_code == 403
    spoofed = TestClient(client.app).post(
        path, data=FORM, headers={"X-Remote-User": "alice"}
    )  # no proxy secret
    assert spoofed.status_code == 403
    assert api_calls == []


def test_out_of_range_duration_is_refused_before_the_dialog(client: TestClient, api_calls: list[Any]) -> None:
    resp = client.post(
        "/og/dispatch/as-deployments/propose",
        data={**FORM, "duration_minutes": "500"},
        headers={"X-Remote-User": "alice"},
    )
    assert "between 1 and 240 minutes" in resp.text and api_calls == []
