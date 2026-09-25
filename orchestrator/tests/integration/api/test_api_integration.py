"""Integration tests against real Postgres (`og_t_api` on the server, BUILD.md S5). Exercises
`PgStore`/`PgTraceBackend` end to end through the FastAPI app, not just the fakes the unit suite uses.
"""

from __future__ import annotations

from uuid import uuid4

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS


def test_health_ok(client) -> None:
    resp = client.get("/og/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert isinstance(resp.json()["processes"], dict)


def test_fleet_hubs_smoke(client) -> None:
    resp = client.get("/og/api/fleet/hubs", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_dispatch_opportunities_smoke(client) -> None:
    resp = client.get("/og/api/dispatch/opportunities", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_ledger_timeline_smoke(client) -> None:
    """Exercises the real `og.reservation`/`og.grant` queries end to end, including the known
    text-vs-uuid `bank_id` type mismatch flagged in `opengrid.api`'s README -- an empty result for a
    made-up bank id is the expected (non-crashing) outcome today."""
    resp = client.get("/og/api/ledger/no-such-bank/timeline", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reservations"] == []
    assert body["grants"] == []


def test_contract_crud_round_trip(client) -> None:
    customer_id = str(uuid4())
    create = client.post(
        "/og/api/contracts",
        headers=OPERATOR_HEADERS,
        json={
            "customer_id": customer_id,
            "service_type": "HOME",
            "tier": "L1",
            "profile_ref": "home-profile@1",
            "start_at": "2026-09-01T00:00:00Z",
        },
    )
    assert create.status_code == 201
    contract_id = create.json()["contract_id"]

    fetched = client.get(f"/og/api/contracts/{contract_id}", headers=VIEWER_HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "ACTIVE"

    patched = client.patch(
        f"/og/api/contracts/{contract_id}", headers=OPERATOR_HEADERS, json={"status": "ENDED"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "ENDED"


def test_retention_policy_round_trip(client) -> None:
    updated = client.put(
        "/og/api/retention",
        headers=OPERATOR_HEADERS,
        json={"event_class": "TEST_INTEGRATION_CLASS", "retention_days": 7},
    )
    assert updated.status_code == 200

    listed = client.get("/og/api/retention", headers=VIEWER_HEADERS)
    assert listed.status_code == 200
    by_class = {row["event_class"]: row["retention_days"] for row in listed.json()}
    assert by_class["TEST_INTEGRATION_CLASS"] == 7


def test_trace_write_then_verify_round_trip(client) -> None:
    """The retention PUT above writes a trace row on stream `operator_action:operator`; verify that
    stream's chain."""
    client.put(
        "/og/api/retention",
        headers=OPERATOR_HEADERS,
        json={"event_class": "TEST_TRACE_CLASS", "retention_days": 3},
    )
    result = client.post(
        "/og/api/trace/verify", headers=VIEWER_HEADERS, json={"stream_id": "operator_action:operator"}
    )
    assert result.status_code == 200
    assert result.json()["passed"] is True
