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
    "/og/api/billing/performance?from=2026-09-01&to=2026-09-30",
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


def test_hub_not_found(client) -> None:
    resp = client.get("/og/api/fleet/hubs/does-not-exist", headers=VIEWER_HEADERS)
    assert resp.status_code == 404


def test_bank_not_found(client) -> None:
    resp = client.get("/og/api/fleet/banks/does-not-exist", headers=VIEWER_HEADERS)
    assert resp.status_code == 404


def test_trace_verify(client) -> None:
    resp = client.post("/og/api/trace/verify", headers=VIEWER_HEADERS, params={"stream_id": "engine:cycle-1"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "broken_at_seq": None, "reason": None}


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


def test_opportunity_creation_surfaces_not_implemented_as_503(client) -> None:
    """`opengrid.contracts.admit` is still a BUILD.md stub; the API must surface that as a clear 503,
    not a bare 500 traceback (`opengrid.api.app`'s `NotImplementedError` handler)."""
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
