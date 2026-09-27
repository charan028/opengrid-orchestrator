"""Utility customer API v1 (D-33): Austin Energy issues and follows its own toll calls. Identity and
role mapping, utility isolation (another utility's obligation or call answers 404 and is traced as
AUTHZ_DENY), the checks shared with the operator path, idempotency and the call read-back."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from opengrid.api.deps import get_call_store
from opengrid.platform.config import Config

from ..calls.fakes import FakeCallStore, make_award

BASE = "/og/api/customer/v1/utility"
AEN = {"X-Remote-User": "og-util-aen"}
CPS = {"X-Remote-User": "og-util-cps"}
BAD = {"X-Remote-User": "og-util-bad"}
LCRA = {"X-Remote-User": "og-util-lcra"}  # D-37: mapped, not enabled (sample contract only)
CUSTOMER = {"X-Remote-User": "og-cust-a"}
OPERATOR = {"X-Remote-User": "operator"}


@pytest.fixture
def config() -> Config:
    return Config(
        {
            "api": {
                "roles": {
                    "operator": [],
                    "viewer": [],
                    "customer": {"og-cust-a": "00000000-0000-7000-8000-0000000000c6"},
                    "utility": {
                        "og-util-aen": "AUSTIN_ENERGY",
                        "og-util-cps": "CPS_ENERGY",
                        "og-util-bad": "not a utility id",
                        "og-util-lcra": "LCRA",
                    },
                },
                "utility_api": {"enabled_utilities": ["AUSTIN_ENERGY", "CPS_ENERGY"]},
            }
        }
    )


@pytest.fixture
def calls() -> FakeCallStore:
    return FakeCallStore()


@pytest.fixture
def api(client: TestClient, calls: FakeCallStore) -> Iterator[TestClient]:
    client.app.dependency_overrides[get_call_store] = lambda: calls  # type: ignore[attr-defined]
    yield client


def _toll_now(calls: FakeCallStore, *, utility_id: str = "AUSTIN_ENERGY", minutes_left: int = 120):
    now = datetime.now(UTC)
    return calls.add(
        make_award(
            window_start=now - timedelta(minutes=5),
            window_end=now + timedelta(minutes=minutes_left),
            utility_id=utility_id,
            committed_kw=26_000.0,
        )
    )


def _call(api: TestClient, headers=AEN, **body):
    payload = {"kw": -20_000, "duration_minutes": 60, "idempotency_key": str(uuid4()), **body}
    return api.post(f"{BASE}/calls", headers=headers, json=payload)


def test_ts_d33_30_me_maps_the_identity_to_its_utility(api) -> None:
    assert api.get(f"{BASE}/me", headers=AEN).json() == {
        "user": "og-util-aen",
        "utility_id": "AUSTIN_ENERGY",
        "api_version": "v1",
    }


@pytest.mark.parametrize("headers", [CUSTOMER, OPERATOR, BAD, LCRA])
def test_ts_d33_31_only_a_mapped_utility_identity_reaches_the_utility_api(
    api, trace_backend, headers
) -> None:
    assert api.get(f"{BASE}/me", headers=headers).status_code == 403
    assert _call(api, headers=headers).status_code == 403


def test_ts_d33_32_a_utility_cannot_read_operator_endpoints(api) -> None:
    assert api.get("/og/api/dispatch/as-deployments", headers=AEN).status_code == 403


def test_ts_d33_33_obligations_are_mine_only_with_todays_window(api, calls) -> None:
    mine = _toll_now(calls)
    _toll_now(calls, utility_id="CPS_ENERGY")
    body = api.get(f"{BASE}/obligations", headers=AEN).json()
    assert [o["obligation_id"] for o in body["obligations"]] == [str(mine)]
    assert body["today"]["obligation_id"] == str(mine)
    assert body["today"]["held_kw"] == 0.0 and body["today"]["max_call_minutes"] == 90


def test_ts_d33_34_issue_status_history_and_cancel(api, calls) -> None:
    oid = _toll_now(calls)
    created = _call(api, obligation_id=str(oid), idempotency_key="aen-001")
    assert created.status_code == 201
    call = created.json()
    assert call["state"] in ("ACTIVE", "ACCEPTED") and call["origin"] == "UTILITY"
    assert call["target_kw"] == -20_000
    (deployment,) = calls.deployments.values()
    assert deployment.source == "UTILITY" and deployment.requested_by == "og-util-aen"
    assert [a["rule"] for a in calls.alerts] == ["ALR-UTILITY-CALL"]  # the operator is alerted

    held = api.get(f"{BASE}/obligations", headers=AEN).json()["today"]
    assert held["active_call_id"] == call["call_id"] and held["held_kw"] == -20_000

    replay = _call(api, obligation_id=str(oid), idempotency_key="aen-001")
    assert replay.status_code == 200 and replay.json()["call_id"] == call["call_id"]

    status = api.get(f"{BASE}/calls/{call['call_id']}", headers=AEN).json()
    assert status["state"] == "ACTIVE" and status["granted_kwh"] == 0.0
    assert status["delivery_measured"] is False and "delivered_kw" not in status  # grants, not metered
    assert status["granted_description"].startswith("planned/granted, not measured")
    history = api.get(f"{BASE}/calls", headers=AEN).json()["calls"]
    assert [c["call_id"] for c in history] == [call["call_id"]]

    cancelled = api.post(f"{BASE}/calls/{call['call_id']}/cancel", headers=AEN, json={})
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "COMPLETED"
    again = api.post(f"{BASE}/calls/{call['call_id']}/cancel", headers=AEN, json={})
    assert again.status_code == 409


def test_ts_d33_35_obligation_is_resolved_when_omitted(api, calls) -> None:
    oid = _toll_now(calls)
    assert _call(api).json()["obligation_id"] == str(oid)


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"kw": 20_000}, 422, "R-CALL-CHARGE-REFUSED"),
        ({"kw": 0}, 422, "R-CALL-CHARGE-REFUSED"),
        ({"duration_minutes": 91}, 409, "R-CALL-DURATION-CAP"),
        ({"kw": -30_000}, 409, "R-CALL-OVER-COMMITTED"),
    ],
)
def test_ts_d33_36_refusals_match_the_operator_path(api, calls, body, status, reason) -> None:
    _toll_now(calls, minutes_left=200)
    resp = _call(api, **body)
    assert resp.status_code == status
    detail = resp.json()["detail"]
    assert detail["reason_code"] == reason and "call_id" in detail
    refused = api.get(f"{BASE}/calls/{detail['call_id']}", headers=AEN).json()
    assert refused["state"] == "REFUSED" and refused["reason_code"] == reason
    assert calls.alerts[-1]["rule"] == "ALR-UTILITY-CALL-REFUSED"


def test_ts_d33_37_overlap_is_409(api, calls) -> None:
    _toll_now(calls)
    assert _call(api, duration_minutes=30).status_code == 201
    overlap = _call(api, duration_minutes=10)
    assert overlap.status_code == 409 and overlap.json()["detail"]["reason_code"] == "R-CALL-OVERLAP"


def test_ts_d33_38_another_utilitys_obligation_and_call_are_404_and_audited(
    api, calls, trace_backend
) -> None:
    cps_obligation = _toll_now(calls, utility_id="CPS_ENERGY")
    resp = _call(api, obligation_id=str(cps_obligation))
    assert resp.status_code == 404 and resp.json()["detail"]["reason_code"] == "R-CALL-NOT-FOUND"
    missing = _call(api, obligation_id=str(uuid4()))
    assert missing.json()["detail"]["detail"] == resp.json()["detail"]["detail"]  # indistinguishable

    cps_call = _call(api, headers=CPS, obligation_id=str(cps_obligation)).json()
    for probe in (
        api.get(f"{BASE}/calls/{cps_call['call_id']}", headers=AEN),
        api.post(f"{BASE}/calls/{cps_call['call_id']}/cancel", headers=AEN, json={}),
    ):
        assert probe.status_code == 404
    assert cps_call["call_id"] not in {
        c["call_id"] for c in api.get(f"{BASE}/calls", headers=AEN).json()["calls"]
    }
    assert calls.deployments  # CPS's call still stands
    denies = [r for r in trace_backend.rows if r["stream_id"] == "authz_deny:og-util-aen"]
    assert len(denies) == 3 and {r["event_class"] for r in denies} == {"TRACE_AUTHZ_DENY"}


def test_ts_d33_39_every_call_is_traced_with_origin_and_principal(api, calls, trace_backend) -> None:
    _toll_now(calls)
    _call(api)
    (row,) = [r for r in trace_backend.rows if r["stream_id"] == "dispatch_call:og-util-aen"]
    assert row["event_class"] == "DISPATCH_CALL"  # the pre-image, before the deployment exists (K10)
    assert row["payload"]["origin"] == "UTILITY" and row["payload"]["principal"] == "og-util-aen"


def test_ts_d33_40_a_missing_idempotency_key_is_rejected(api, calls) -> None:
    _toll_now(calls)
    resp = api.post(f"{BASE}/calls", headers=AEN, json={"kw": -1000, "duration_minutes": 10})
    assert resp.status_code == 422


# --- W1 review (r3.4.3) --------------------------------------------------------------------------------


def test_ts_d33_41_a_timestamp_without_utc_offset_is_422_not_500(api, calls) -> None:
    _toll_now(calls)
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    resp = _call(api, start_at=naive)
    assert resp.status_code == 422 and "timezone" in resp.text
    call_id = _call(api).json()["call_id"]
    cancel = api.post(f"{BASE}/calls/{call_id}/cancel", headers=AEN, json={"end_at": naive})
    assert cancel.status_code == 422
    assert api.get(f"{BASE}/calls", headers=AEN, params={"since": naive}).status_code == 422


async def test_ts_d33_42_a_utility_reads_but_cannot_cancel_an_operator_call(
    api, calls, trace_backend
) -> None:
    from opengrid.calls import CallLimits, CallOrigin, CallRequest, issue_call
    from opengrid.trace.store import TraceStore

    oid = _toll_now(calls)
    operator_call = await issue_call(
        calls,
        TraceStore(trace_backend),
        CallRequest(
            origin=CallOrigin.OPERATOR,
            principal="operator",
            reason="op",
            duration_minutes=30,
            obligation_id=oid,
        ),
        limits=CallLimits(),
    )
    path = f"{BASE}/calls/{operator_call.call_id}"
    assert api.get(path, headers=AEN).status_code == 200  # transparency: its own toll
    denied = api.post(f"{path}/cancel", headers=AEN, json={})
    assert denied.status_code == 403 and denied.json()["detail"]["reason_code"] == "R-CALL-NOT-ISSUER"
    assert calls.deployments[operator_call.deployment_id].cancelled_at is None
    denies = [r for r in trace_backend.rows if r["stream_id"] == "authz_deny:og-util-aen"]
    assert denies and denies[-1]["payload"]["action"] == "dispatch.call.cancel"


def test_ts_d33_43_utility_read_denials_are_traced(api, trace_backend) -> None:
    for headers in (CUSTOMER, LCRA, BAD):
        assert api.get(f"{BASE}/me", headers=headers).status_code == 403
    denied = {r["stream_id"] for r in trace_backend.rows if r["event_class"] == "TRACE_AUTHZ_DENY"}
    assert {"authz_deny:og-cust-a", "authz_deny:og-util-lcra", "authz_deny:og-util-bad"} <= denied
