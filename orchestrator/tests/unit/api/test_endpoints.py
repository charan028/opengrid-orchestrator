"""Shape/status smoke tests for every read endpoint (02b S7.1) plus the customers/contracts/retention
CRUD `api` owns directly."""

from __future__ import annotations

import pytest

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS
from .fakes import SAMPLE_BANK_ID, SAMPLE_CONTRACT_ID, SAMPLE_HUB_ID

READ_ENDPOINTS = [
    "/og/api/fleet/hubs",
    f"/og/api/fleet/hubs/{SAMPLE_HUB_ID}",
    f"/og/api/fleet/banks/{SAMPLE_BANK_ID}",
    "/og/api/markets/series",
    "/og/api/markets/feed-status",
    "/og/api/forecast?series_key=HB_HUBAVG&kind=price",
    "/og/api/dispatch/opportunities",
    "/og/api/dispatch/plan/latest",
    f"/og/api/ledger/{SAMPLE_BANK_ID}/timeline",
    "/og/api/profitability/summary",
    "/og/api/billing/invoice-lines?from=2026-09-01&to=2026-09-30",
    "/og/api/trace/events",
    "/og/api/customers",
    "/og/api/contracts",
    f"/og/api/contracts/{SAMPLE_CONTRACT_ID}",
    "/og/api/retention",
]


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoint_ok_for_viewer(client, path: str) -> None:
    resp = client.get(path, headers=VIEWER_HEADERS)
    assert resp.status_code == 200, resp.text


def test_invoice_lines_csv_export(client) -> None:
    resp = client.get(
        "/og/api/billing/invoice-lines?from=2026-09-01&to=2026-09-30&format=csv", headers=VIEWER_HEADERS
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "invoice_line_id" in resp.text


def test_invoice_lines_json_embeds_performance(client) -> None:
    resp = client.get("/og/api/billing/invoice-lines?from=2026-09-01&to=2026-09-30", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "lines" in body
    assert "performance" in body


def test_hub_not_found(client) -> None:
    resp = client.get("/og/api/fleet/hubs/does-not-exist", headers=VIEWER_HEADERS)
    assert resp.status_code == 404


def test_bank_not_found(client) -> None:
    resp = client.get("/og/api/fleet/banks/does-not-exist", headers=VIEWER_HEADERS)
    assert resp.status_code == 404


def test_trace_verify(client) -> None:
    resp = client.post("/og/api/trace/verify", headers=VIEWER_HEADERS, json={"stream_id": "engine:cycle-1"})
    assert resp.status_code == 200
    assert resp.json() == {"passed": True, "checked": 1, "first_broken": None}


def test_trace_verify_defaults_to_every_stream(client) -> None:
    resp = client.post("/og/api/trace/verify", headers=VIEWER_HEADERS, json={"class": "ALERT"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is True
    assert body["checked"] >= 1


def test_create_and_patch_contract(client) -> None:
    create_resp = client.post(
        "/og/api/contracts",
        headers=OPERATOR_HEADERS,
        json={
            "customer_id": "00000000-0000-7000-8000-000000000c09",
            "service_type": "HOME",
            "tier": "L1",
            "profile_ref": "home-profile@1",
            "start_at": "2026-09-01T00:00:00Z",
        },
    )
    assert create_resp.status_code == 201
    contract_id = create_resp.json()["contract_id"]

    patch_resp = client.patch(
        f"/og/api/contracts/{contract_id}", headers=OPERATOR_HEADERS, json={"status": "SUSPENDED"}
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "SUSPENDED"


def test_create_contract_requires_operator(client) -> None:
    resp = client.post(
        "/og/api/contracts",
        headers=VIEWER_HEADERS,
        json={
            "customer_id": "00000000-0000-7000-8000-000000000c09",
            "service_type": "HOME",
            "tier": "L1",
            "profile_ref": "home-profile@1",
            "start_at": "2026-09-01T00:00:00Z",
        },
    )
    assert resp.status_code == 403


def test_ack_alert(client) -> None:
    resp = client.post("/og/api/alerts/1/ack", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["acked_by"] == "operator"


def test_ack_missing_alert_404(client) -> None:
    resp = client.post("/og/api/alerts/999/ack", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


def test_retention_update_records_trace_and_operator_action(client, fake_store) -> None:
    resp = client.put(
        "/og/api/retention", headers=OPERATOR_HEADERS, json={"event_class": "ALERT", "retention_days": 45}
    )
    assert resp.status_code == 200
    assert fake_store.retention["ALERT"] == 45
    assert len(fake_store.operator_actions) == 1
    assert fake_store.operator_actions[0]["action_kind"] == "CONFIG_CHANGE"


def test_fleet_hubs_wrapped_in_items(client) -> None:
    """`opengrid.ui.routes.fleet`/`control_room` parse `raw.get("items", [])`."""
    resp = client.get("/og/api/fleet/hubs", headers=VIEWER_HEADERS)
    body = resp.json()
    assert "items" in body
    assert body["items"][0]["hub_id"] == SAMPLE_HUB_ID
    assert body["items"][0]["bank_id"] == SAMPLE_BANK_ID


def test_hub_drilldown_is_flat(client) -> None:
    """`templates/_partials/hub_drilldown.html` reads `hub.bank_id`/`hub.soc_kwh`/... at the top
    level, not nested under `hub`/`state` keys."""
    resp = client.get(f"/og/api/fleet/hubs/{SAMPLE_HUB_ID}", headers=VIEWER_HEADERS)
    body = resp.json()
    assert body["hub_id"] == SAMPLE_HUB_ID
    assert body["bank_id"] == SAMPLE_BANK_ID
    assert "soc_kwh" in body
    assert "hub" not in body


def test_dispatch_opportunities_returns_obligation_shaped_rows(client) -> None:
    """`opengrid.ui.routes.dispatch.pipeline_view` needs `committed_qty_kw`/`tier`/`at_risk` --
    `Obligation` fields, not `Opportunity` fields."""
    resp = client.get("/og/api/dispatch/opportunities", headers=VIEWER_HEADERS)
    body = resp.json()
    assert "committed_qty_kw" in body[0]
    assert "tier" in body[0]


def test_ledger_timeline_shape(client) -> None:
    resp = client.get(f"/og/api/ledger/{SAMPLE_BANK_ID}/timeline", headers=VIEWER_HEADERS)
    body = resp.json()
    assert set(body) >= {"reservations", "grants", "commitments", "bank_capacity_kw"}


def test_health_payload_has_control_room_kpis(client) -> None:
    resp = client.get("/og/api/health")
    body = resp.json()
    assert isinstance(body["processes"], dict)
    assert "fleet_mw" in body
    assert "active_commitments" in body
    assert isinstance(body["alerts"], list)


def test_health_payload_has_degraded_modes_field(client) -> None:
    """R2 item 1 (defect fix): `GET /og/api/health` used to have no `degraded_modes` field at all --
    `opengrid.health.evaluate_once` computed it every cycle but nothing persisted or exposed it. This
    test suite's `app.state.pool` is never set (no real `_lifespan`), so `_degraded_modes` degrades to an
    empty list (K7) rather than the endpoint crashing -- proving the field is present and safe without a
    database, not the persisted-value round trip (that's `tests/unit/health/test_queries.py`'s job)."""
    resp = client.get("/og/api/health")
    body = resp.json()
    assert body["degraded_modes"] == []


def test_health_payload_alerts_have_scope_fields(client) -> None:
    """R2 item 1: `og.alert.scope_kind`/`scope_ref` (migration 0024) must be present on every alert in
    the payload -- `None` here since this suite's `app.state.pool` is never set, so `_merge_alert_scopes`
    degrades (K7) rather than crashing; the real round trip is `tests/unit/health/test_queries.py`'s job."""
    resp = client.get("/og/api/health")
    body = resp.json()
    assert body["alerts"], "fixture must seed at least one alert for this to be meaningful"
    for alert in body["alerts"]:
        assert "scope_kind" in alert
        assert "scope_ref" in alert
        assert alert["scope_kind"] is None
        assert alert["scope_ref"] is None


def test_opportunity_creation_surfaces_not_implemented_as_503(client) -> None:
    """`FakeContractsRepo` (this suite's test double, `contracts_fake.py`) does not model
    `create_opportunity_and_obligation` -- the API must surface that as a clear 503, not a bare 500
    traceback (`opengrid.api.app`'s `NotImplementedError` handler); the real admission success path is
    covered by `tests/unit/contracts/` (selector agent)."""
    resp = client.post(
        "/og/api/opportunities",
        headers=OPERATOR_HEADERS,
        json={
            "contract_id": str(SAMPLE_CONTRACT_ID),
            "window_start": "2026-09-26T00:00:00Z",
            "window_end": "2026-09-26T01:00:00Z",
            "requested_kw": "100",
        },
    )
    assert resp.status_code == 503
