"""Power quality & assets screen (`/og/pq`, WP-J): reads only the WP-J API, relays its two-step writes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.pq as pq_route
from opengrid.ui.api_client import ApiUnavailable
from opengrid.ui.routes.pq import spectrum_bars, spectrum_trend, summary_view, work_orders_view

from .conftest import load_fixture

HUB = "hub-01998"
GETS = {
    "/og/api/work-orders": "pq_work_orders.json",
    f"/og/api/hubs/{HUB}/waveform": "pq_waveform.json",
    f"/og/api/hubs/{HUB}/spectrum": "pq_spectrum.json",
    f"/og/api/hubs/{HUB}/asset-health": "pq_asset_health.json",
    f"/og/api/hubs/{HUB}/calibration-history": "pq_calibration.json",
    f"/og/api/fleet/hubs/{HUB}": "pq_hub_location.json",
    "/og/api/banks/bank-038/pq": "pq_bank.json",
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], str | None]]:
    posts: list[tuple[str, dict[str, Any], str | None]] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path in GETS:
            return load_fixture(GETS[path])
        raise ApiUnavailable(f"no fixture for {path}", status_code=404)

    async def fake_post_json(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        posts.append((path, payload, remote_user))
        if path.endswith("/confirm"):
            if "calibrate" in path:
                return {"calibration_id": "cal-1", "outcome": "PENDING"}
            return {"request_id": "req-1"}
        return {
            "proposal_id": "p-1",
            "summary": "Request an on-demand raw waveform capture",
            "expires_in_s": 60,
        }

    monkeypatch.setattr(pq_route, "get_json", fake_get_json)
    monkeypatch.setattr(pq_route, "post_json", fake_post_json)
    return posts


def test_summary_view_keeps_only_reported_phases() -> None:
    view = summary_view(load_fixture("pq_waveform.json")["summary"])
    assert view is not None and [p["phase"] for p in view["phases"]] == ["A"]
    assert view["phases"][0]["thd_i"] == pytest.approx(2.04) and view["freq_hz"] == pytest.approx(59.998)
    assert summary_view(None) is None


def test_spectrum_charts_order_harmonics_numerically() -> None:
    bars = spectrum_bars(load_fixture("pq_waveform.json")["summary"])
    assert bars["xAxis"]["data"] == ["H3", "H5", "H7"]
    assert bars["series"][0]["data"] == pytest.approx([1.18, 0.59, 0.20])
    assert bars["series"][1]["data"][2] is None  # no H7 voltage reported: gap, not zero
    trend = spectrum_trend(load_fixture("pq_spectrum.json")["points"])
    assert [s["name"] for s in trend["series"]] == ["I H3", "I H5", "I H7"]
    assert trend["series"][1]["data"][0][1] is None


def test_work_orders_open_first() -> None:
    rows = work_orders_view(list(reversed(load_fixture("pq_work_orders.json"))))
    assert rows[0]["status"] == "OPEN" and "calibration_id" not in rows[1]["evidence_text"]


def test_screen_defaults_to_the_first_open_work_order(client: TestClient, api: list[Any]) -> None:
    html = client.get("/og/pq", headers={"X-Remote-User": "viewer"}).text
    assert "Waveform summary: hub-01998" in html
    assert 'id="pq-phases"' in html and "59.998 Hz" in html
    assert "Bank bank-038 measured PQ" in html and "48 of 50 hubs reporting" in html
    assert "DEGRADED" in html and "WORSE_ROLLED_BACK" in html and "SIGNED" in html
    assert 'id="pq-capture-form"' not in html and 'id="pq-calibrate-form"' not in html  # viewer: read-only


def test_unknown_hub_says_so(client: TestClient, api: list[Any]) -> None:
    html = client.get("/og/pq", params={"hub": "hub-99999"}, headers={"X-Remote-User": "viewer"}).text
    assert 'id="pq-hub-missing"' in html


@pytest.mark.parametrize(
    ("action", "api_path"), [("capture", "waveform-capture"), ("calibrate", "calibrate")]
)
def test_operator_writes_are_two_step(client: TestClient, api: list[Any], action: str, api_path: str) -> None:
    op = {"X-Remote-User": "alice"}
    proposed = client.post(f"/og/pq/{HUB}/{action}/propose", data={"reason": "test"}, headers=op)
    assert proposed.status_code == 200 and f"/og/pq/{HUB}/{action}/p-1/confirm" in proposed.text
    assert api == [(f"/og/api/hubs/{HUB}/{api_path}", {"reason": "test"}, "alice")]
    confirmed = client.post(f"/og/pq/{HUB}/{action}/p-1/confirm", headers=op)
    assert confirmed.status_code == 200 and 'id="pq-action-result"' in confirmed.text
    assert api[-1] == (f"/og/api/hubs/{HUB}/{api_path}/p-1/confirm", {}, "alice")


def test_rate_limited_capture_is_explained(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, api: list[Any]
) -> None:
    async def limited(path: str, payload: dict[str, Any], *, remote_user: str | None = None) -> Any:
        raise ApiUnavailable("429", status_code=429, detail={"detail": "waveform capture rate limit"})

    monkeypatch.setattr(pq_route, "post_json", limited)
    html = client.post(f"/og/pq/{HUB}/capture/p-1/confirm", headers={"X-Remote-User": "alice"}).text
    assert "Rate limited" in html


@pytest.mark.parametrize("path", [f"/og/pq/{HUB}/capture/propose", f"/og/pq/{HUB}/calibrate/p-1/confirm"])
def test_viewer_cannot_write(client: TestClient, api: list[Any], path: str) -> None:
    assert client.post(path, data={"reason": "x"}, headers={"X-Remote-User": "carol"}).status_code == 403
    assert api == []
