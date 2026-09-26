"""Two-step confirmation (02b S7.1/S7.3): propose -> confirm, expiry, unknown/wrong-kind proposals,
a guardian veto surfacing as 409, and the "dependency process not running" 503 paths.

`fleet`'s confirm writes a `command_batch` + K10 trace pre-image and polls `og.verdict` for the
independently-running `og-guardian` process's result; `safestop`'s propose/confirm NOTIFY
`opengrid.safestop.pg_backend.REQUEST_CHANNEL` and poll `og.stop_event` for `og-safestop`'s response.
`FakeStore` (`fakes.py`) simulates both processes responding instantly so these tests never wait out
the real poll timeouts, and can be toggled off to exercise the 503 "not running" path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS
from .fakes import SAMPLE_HUB_ID


def _propose_command(client) -> str:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"bank_id": "bank-01", "p_kw_setpoint": 3.5, "reason": "demo"},
    )
    assert resp.status_code == 202
    return resp.json()["proposal_id"]


def _manual_targets(fake_trace_store) -> list[dict]:
    records = []
    for stream in ("operator_action:operator",):
        records += asyncio.run(fake_trace_store._backend.fetch_range(stream, from_seq=0))
    return [r.payload for r in records if r.event_class == "MANUAL_TARGET"]


def test_command_confirm_records_a_manual_target_for_the_engine_to_ramp(
    client, fake_store, fake_trace_store
) -> None:
    """Confirm no longer sends a one-shot batch (always vetoed by G-04's step bound): it records a K10
    MANUAL_TARGET for the engine to ramp toward (engine/manual.py) and answers 202 RAMPING."""
    proposal_id = _propose_command(client)
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "RAMPING" and body["trace_id"] and body["expires_at"]
    assert body["hub_ids"] == [SAMPLE_HUB_ID]  # the bank selection resolved to its hubs
    (target,) = _manual_targets(fake_trace_store)
    assert target["hub_ids"] == [SAMPLE_HUB_ID] and target["p_kw_target"] == 3.5
    issued, expires = (datetime.fromisoformat(target[k]) for k in ("issued_at", "expires_at"))
    assert expires - issued == timedelta(minutes=15)  # the default hold
    assert target["proposer"] == "operator" and target["reason"] == "demo"
    assert fake_store.command_batches == []  # no one-shot batch any more
    assert fake_store.operator_actions[-1]["action_kind"] == "MANUAL_COMMAND"


def test_command_duration_override_is_bounded(client, fake_trace_store) -> None:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"hub_id": SAMPLE_HUB_ID, "p_kw_setpoint": -5.0, "reason": "peak", "duration_minutes": 90},
    )
    proposal_id = resp.json()["proposal_id"]
    assert (
        client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS).status_code
        == 202
    )
    (target,) = _manual_targets(fake_trace_store)
    issued, expires = (datetime.fromisoformat(target[k]) for k in ("issued_at", "expires_at"))
    assert expires - issued == timedelta(minutes=90) and target["p_kw_target"] == -5.0
    too_long = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"hub_id": SAMPLE_HUB_ID, "p_kw_setpoint": -5.0, "reason": "x", "duration_minutes": 241},
    )
    assert too_long.status_code == 422


def test_an_unknown_hub_is_404_at_confirm(client) -> None:
    resp = client.post(
        "/og/api/fleet/command",
        headers=OPERATOR_HEADERS,
        json={"hub_id": "hub-nope", "p_kw_setpoint": 1, "reason": "x"},
    )
    confirm = client.post(
        f"/og/api/fleet/command/{resp.json()['proposal_id']}/confirm", headers=OPERATOR_HEADERS
    )
    assert confirm.status_code == 404


def test_a_manual_target_can_be_cancelled(client, fake_store, fake_trace_store) -> None:
    proposal_id = _propose_command(client)
    trace_id = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS).json()[
        "trace_id"
    ]
    fake_store.manual_target_hubs[trace_id] = {SAMPLE_HUB_ID: 3.5}
    listed = client.get("/og/api/fleet/manual-targets", headers=VIEWER_HEADERS).json()["items"]
    assert [(i["hub_id"], i["trace_id"]) for i in listed] == [(SAMPLE_HUB_ID, trace_id)]

    resp = client.post(f"/og/api/fleet/manual-targets/{trace_id}/cancel", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED" and resp.json()["hub_ids"] == [SAMPLE_HUB_ID]
    cancel = _manual_targets(fake_trace_store)[-1]
    assert cancel["hub_ids"] == [SAMPLE_HUB_ID] and cancel["cancels"] == trace_id
    assert cancel["expires_at"] == cancel["issued_at"]  # expires now: newest target wins, and it is over

    fake_store.manual_target_hubs.clear()  # no longer controls any hub
    assert (
        client.post(f"/og/api/fleet/manual-targets/{trace_id}/cancel", headers=OPERATOR_HEADERS).status_code
        == 404
    )


def test_viewer_cannot_confirm_or_cancel(client) -> None:
    proposal_id = _propose_command(client)
    assert (
        client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=VIEWER_HEADERS).status_code == 403
    )
    assert (
        client.post(f"/og/api/fleet/manual-targets/{uuid4()}/cancel", headers=VIEWER_HEADERS).status_code
        == 403
    )


def test_command_confirm_unknown_proposal_404(client) -> None:
    resp = client.post(f"/og/api/fleet/command/{uuid4()}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


def test_command_confirm_is_single_use(client) -> None:
    proposal_id = _propose_command(client)
    first = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert first.status_code == 202
    second = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert second.status_code == 404


def test_command_confirm_expired_proposal_is_410(client, fake_proposals) -> None:
    proposal_id = _propose_command(client)
    # Force the 60 s TTL to have elapsed without waiting real time in the test.
    stored = fake_proposals._proposals[UUID(proposal_id)]
    stored.created_at -= 120.0
    resp = client.post(f"/og/api/fleet/command/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 410


# -- safestop -----------------------------------------------------------------------------------------


def test_safestop_propose_confirm(client, fake_store) -> None:
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "bank", "scope_id": "bank-01", "reason": "overload drill"},
    )
    assert propose.status_code == 202
    proposal_id = propose.json()["proposal_id"]
    assert fake_store.notifications[0]["action"] == "PROPOSE"

    confirm = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["engaged"] is True
    assert fake_store.operator_actions[-1]["action_kind"] == "SAFE_STOP_ENGAGE"


def test_safestop_confirm_no_response_is_503(client, fake_store, monkeypatch) -> None:
    monkeypatch.setattr("opengrid.api.routers.safestop._STOP_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr("opengrid.api.routers.safestop._STOP_POLL_INTERVAL_S", 0.01)
    fake_store.safestop_responds = False
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "fleet", "scope_id": None, "reason": "drill"},
    )
    proposal_id = propose.json()["proposal_id"]
    resp = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 503


def test_safestop_release_request_is_step_one_of_two(client, fake_store) -> None:
    """K8: a release is the guardian's two-person path; the request alone releases nothing and writes
    no SAFE_STOP_RELEASE action (test_safestop_release.py covers the approval)."""
    resp = client.post(
        "/og/api/safestop/bank/bank-01/release", headers=OPERATOR_HEADERS, json={"reason": "clear"}
    )
    assert resp.status_code == 202
    assert fake_store.operator_actions == []


def test_safestop_proposal_window_is_og_safestops_confirm_window(client, fake_proposals, fake_store) -> None:
    """Workstation finding (2026-09-26): the API advertised and kept a safe-stop proposal for 60 s, but
    og-safestop's ConfirmationBroker expires it at `[safestop].confirm_window_s` = 30 s -- confirming at
    40 s got a 503 and the stop was NOT engaged. The API now uses the broker's window and refuses a
    confirm past it with 409 "propose again" (nothing is notified to og-safestop)."""
    propose = client.post(
        "/og/api/safestop",
        headers=OPERATOR_HEADERS,
        json={"scope": "fleet", "scope_id": None, "reason": "drill"},
    )
    assert propose.json()["expires_in_s"] == 30.0
    proposal_id = propose.json()["proposal_id"]
    stored = fake_proposals._proposals[UUID(proposal_id)]
    stored.created_at -= 40.0
    notified_before = len(fake_store.notifications)
    resp = client.post(f"/og/api/safestop/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "proposal expired, propose again"
    assert len(fake_store.notifications) == notified_before  # no CONFIRM sent for an expired proposal


def test_the_release_request_keeps_the_60_s_window(client, fake_proposals) -> None:
    resp = client.post(
        "/og/api/safestop/bank/bank-01/release", headers=OPERATOR_HEADERS, json={"reason": "clear"}
    )
    assert resp.json()["expires_in_s"] == 60.0
    assert fake_proposals._proposals[UUID(resp.json()["proposal_id"])].ttl_s == 60.0
