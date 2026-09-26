"""Power-quality and asset-health API (07-delivery/06 S6.6/S6.7, WP-J; ES15/TS-15a).

The router is mounted onto `create_app()` here (the lead adds the one-line mount in `api/app.py`), and
every dependency is overridden with an in-memory fake (`pq_fakes.py`), so no DB or broker is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from opengrid.api.app import create_app
from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.pq_store import ObligationEnvelope
from opengrid.api.routers import pq
from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import PqEnvelopeLimits
from opengrid.platform.mqtt import validate_payload

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS, _echo_csrf_cookie_as_header
from .pq_fakes import FakeAssetPorts, FakePqStore, RecordingPublisher

HUB = "hub-0001"
HUB_2 = "hub-0002"
BANK = "bank-01"
OBLIGATION_ID = UUID("7b1e2c4a-0000-4000-8000-000000000001")
LOOSE_LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=50.0,
    voltage_band_pct=5.0,
    freq_tolerance_hz=0.5,
    pf_min=0.9,
    thd_voltage_limit_pct=5.0,
    thd_current_limit_pct=5.0,
)


def _summary(hub_id: str, ts: datetime, *, v: str = "240", freq: str = "60.0", thd_i: str = "2.0"):
    return PqWaveformSummaryRow(
        hub_id=hub_id,
        ts=ts,
        v_rms_a=Decimal(v),
        v_rms_b=Decimal(v),
        i_rms_a=Decimal("10"),
        i_rms_b=Decimal("10"),
        freq_hz=Decimal(freq),
        pf_a=Decimal("0.99"),
        pf_b=Decimal("0.99"),
        thd_v_pct_a=Decimal("1.0"),
        thd_i_pct_a=Decimal(thd_i),
        harmonics_v={"3": {"mag_pct": Decimal("1.2"), "angle_deg": Decimal("40")}},
    )


@pytest.fixture
def pq_store() -> FakePqStore:
    store = FakePqStore()
    store.add_hub(HUB)
    store.add_hub(HUB_2)
    return store


@pytest.fixture
def asset_ports() -> FakeAssetPorts:
    return FakeAssetPorts()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def pq_client(
    fake_store, fake_trace_store, fake_proposals, fake_config, pq_store, asset_ports, publisher
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_store] = lambda: fake_store
    app.dependency_overrides[get_trace_store] = lambda: fake_trace_store
    app.dependency_overrides[get_proposals] = lambda: fake_proposals
    app.dependency_overrides[get_config] = lambda: fake_config
    app.dependency_overrides[pq.get_pq_store] = lambda: pq_store
    app.dependency_overrides[pq.get_asset_health_service] = asset_ports.service
    app.dependency_overrides[pq.get_capture_publisher] = lambda: publisher
    client = TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)
    client.event_hooks = {"request": [_echo_csrf_cookie_as_header], "response": []}
    return client


# -- auth ------------------------------------------------------------------------------------------------


def test_reads_require_an_identity(pq_client) -> None:
    assert pq_client.get(f"/og/api/hubs/{HUB}/waveform").status_code == 401


def test_reads_refuse_an_identity_not_asserted_by_the_proxy(pq_client) -> None:
    resp = pq_client.get(
        f"/og/api/hubs/{HUB}/waveform", headers={**VIEWER_HEADERS, "X-OG-Proxy-Auth": "wrong"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("path", [f"/og/api/hubs/{HUB}/waveform-capture", f"/og/api/hubs/{HUB}/calibrate"])
def test_writes_require_operator(pq_client, path) -> None:
    resp = pq_client.post(path, headers=VIEWER_HEADERS, json={"reason": "look"})
    assert resp.status_code == 403


# -- waveform / spectrum ---------------------------------------------------------------------------------


def test_ts_15a_waveform_returns_latest_summary_at_or_before_at_and_raw_capture_metadata(
    pq_client, pq_store
) -> None:
    at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    pq_store.summary_rows = [
        _summary(HUB, at - timedelta(seconds=20), v="239"),
        _summary(HUB, at - timedelta(seconds=10), v="241"),
        _summary(HUB, at + timedelta(seconds=10), v="250"),  # after `at`: excluded
        _summary(HUB_2, at, v="200"),  # other hub: excluded
    ]
    capture_id = uuid4()
    pq_store.raw_index = [
        {
            "capture_id": capture_id,
            "hub_id": HUB,
            "ts": at - timedelta(seconds=5),
            "trigger_reason": "API_REQUEST",
            "blob_ref": "blob://x",
            "channels": 6,
        },
        {
            "capture_id": uuid4(),
            "hub_id": HUB,
            "ts": at + timedelta(seconds=5),
            "trigger_reason": "PQ_DEVIATION",
            "blob_ref": "blob://y",
            "channels": 6,
        },
    ]
    resp = pq_client.get(
        f"/og/api/hubs/{HUB}/waveform", params={"at": at.isoformat()}, headers=VIEWER_HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert Decimal(body["summary"]["v_rms_a"]) == Decimal("241")
    assert [c["capture_id"] for c in body["raw_captures"]] == [str(capture_id)]


def test_waveform_with_no_summary_in_window_is_null_not_synthesized(pq_client) -> None:
    resp = pq_client.get(f"/og/api/hubs/{HUB}/waveform", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["summary"] is None
    assert resp.json()["raw_captures"] == []


def test_waveform_at_without_offset_is_read_as_utc(pq_client, pq_store) -> None:
    at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    pq_store.summary_rows = [_summary(HUB, at - timedelta(seconds=1))]
    params = {"at": "2026-09-26T12:00:00"}
    resp = pq_client.get(f"/og/api/hubs/{HUB}/waveform", params=params, headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["summary"] is not None


def test_waveform_unknown_hub_is_404(pq_client) -> None:
    assert pq_client.get("/og/api/hubs/nope/waveform", headers=VIEWER_HEADERS).status_code == 404


def test_spectrum_returns_harmonics_oldest_first_within_window(pq_client, pq_store) -> None:
    now = datetime.now(UTC)
    pq_store.summary_rows = [_summary(HUB, now - timedelta(seconds=s)) for s in (5, 30, 3_600)]
    resp = pq_client.get(f"/og/api/hubs/{HUB}/spectrum", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    points = resp.json()["points"]
    assert len(points) == 2
    assert points[0]["ts"] < points[1]["ts"]
    assert points[0]["harmonics_v"]["3"] == {"mag_pct": "1.2", "angle_deg": "40"}
    assert points[0]["harmonics_i"] is None


def test_spectrum_from_after_to_is_422(pq_client) -> None:
    now = datetime.now(UTC)
    params = {"from": now.isoformat(), "to": (now - timedelta(minutes=1)).isoformat()}
    assert (
        pq_client.get(f"/og/api/hubs/{HUB}/spectrum", params=params, headers=VIEWER_HEADERS).status_code
        == 422
    )


# -- bank PQ / obligation compliance ---------------------------------------------------------------------


def test_bank_pq_is_the_bank_measurement_aggregate(pq_client, pq_store) -> None:
    now = datetime.now(UTC)
    pq_store.summary_rows = [
        _summary(HUB, now, v="252", freq="60.1"),
        _summary(HUB_2, now, v="252", freq="60.1"),
    ]
    resp = pq_client.get(f"/og/api/banks/{BANK}/pq", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["hub_count"] == 2
    assert body["reporting_hub_count"] == 2
    assert body["measurement"]["voltage_deviation_pct"] == pytest.approx(5.0)
    assert body["measurement"]["freq_deviation_hz"] == pytest.approx(0.1)
    assert "_measurement" not in body


def test_bank_pq_with_only_stale_summaries_is_null_never_compliant(pq_client, pq_store) -> None:
    pq_store.summary_rows = [_summary(HUB, datetime.now(UTC) - timedelta(minutes=5))]
    resp = pq_client.get(f"/og/api/banks/{BANK}/pq", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["measurement"] is None


def test_bank_pq_unknown_bank_is_404(pq_client) -> None:
    assert pq_client.get("/og/api/banks/bank-99/pq", headers=VIEWER_HEADERS).status_code == 404


def _obligation(limits: PqEnvelopeLimits | None, bank_ids: list[str]) -> ObligationEnvelope:
    return ObligationEnvelope(
        obligation_id=OBLIGATION_ID,
        contract_id=uuid4(),
        service_type="DATA_CENTER",
        state="DELIVERING",
        limits=limits,
        bank_ids=bank_ids,
    )


def test_pq_compliance_nominal_when_measurement_is_well_inside_the_envelope(pq_client, pq_store) -> None:
    pq_store.obligations[OBLIGATION_ID] = _obligation(LOOSE_LIMITS, [BANK])
    pq_store.summary_rows = [_summary(HUB, datetime.now(UTC))]
    resp = pq_client.get(f"/og/api/obligations/{OBLIGATION_ID}/pq-compliance", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"] == "NOMINAL"
    assert body["banks"][0]["dimensions"]["thd_current_pct"] == "NOMINAL"
    assert body["envelope"]["thd_current_limit_pct"] == 5.0


def test_pq_compliance_breach_when_thd_exceeds_the_limit(pq_client, pq_store) -> None:
    pq_store.obligations[OBLIGATION_ID] = _obligation(LOOSE_LIMITS, [BANK, "bank-02"])
    pq_store.summary_rows = [_summary(HUB, datetime.now(UTC), thd_i="9.0")]
    body = pq_client.get(f"/og/api/obligations/{OBLIGATION_ID}/pq-compliance", headers=VIEWER_HEADERS).json()
    assert body["overall"] == "BREACH"
    assert [b["verdict"] for b in body["banks"]] == ["BREACH", "NO_DATA"]


def test_pq_compliance_missing_data_is_no_data_never_nominal(pq_client, pq_store) -> None:
    pq_store.obligations[OBLIGATION_ID] = _obligation(LOOSE_LIMITS, [BANK, "bank-02"])
    pq_store.summary_rows = [_summary(HUB, datetime.now(UTC))]
    body = pq_client.get(f"/og/api/obligations/{OBLIGATION_ID}/pq-compliance", headers=VIEWER_HEADERS).json()
    assert body["overall"] == "NO_DATA"


def test_pq_compliance_without_envelope_or_banks(pq_client, pq_store) -> None:
    pq_store.obligations[OBLIGATION_ID] = _obligation(None, [BANK])
    body = pq_client.get(f"/og/api/obligations/{OBLIGATION_ID}/pq-compliance", headers=VIEWER_HEADERS).json()
    assert body["envelope"] is None
    assert body["overall"] is None
    pq_store.obligations[OBLIGATION_ID] = _obligation(LOOSE_LIMITS, [])
    body = pq_client.get(f"/og/api/obligations/{OBLIGATION_ID}/pq-compliance", headers=VIEWER_HEADERS).json()
    assert body["overall"] is None
    assert body["banks"] == []


def test_pq_compliance_unknown_obligation_is_404(pq_client) -> None:
    assert (
        pq_client.get(f"/og/api/obligations/{uuid4()}/pq-compliance", headers=VIEWER_HEADERS).status_code
        == 404
    )


# -- asset health / calibration history / work orders ----------------------------------------------------


def test_asset_health_returns_inverter_pq_history_and_open_work_order(pq_client, pq_store) -> None:
    pq_store.inverter_pq[HUB] = {"hub_id": HUB, "asset_state": "DEGRADED", "quality_score": Decimal("0.7")}
    pq_store.events[HUB] = [{"event_type": "STATE_TRANSITION", "from_state": "WATCH", "to_state": "DEGRADED"}]
    pq_store.open_orders[HUB] = {"work_order_id": str(uuid4()), "status": "OPEN", "severity": "HIGH"}
    resp = pq_client.get(f"/og/api/hubs/{HUB}/asset-health", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_state"] == "DEGRADED"
    assert Decimal(body["inverter_pq"]["quality_score"]) == Decimal("0.7")
    assert body["history"][0]["to_state"] == "DEGRADED"
    assert body["open_work_order"]["severity"] == "HIGH"


def test_asset_health_without_inverter_record_is_404(pq_client) -> None:
    assert pq_client.get(f"/og/api/hubs/{HUB}/asset-health", headers=VIEWER_HEADERS).status_code == 404


def test_calibration_history_lists_attempts_with_guardian_decision(pq_client, pq_store) -> None:
    pq_store.calibrations[HUB] = [
        {"calibration_id": str(uuid4()), "outcome": "PENDING", "command_status": "SIGNED"}
    ]
    resp = pq_client.get(f"/og/api/hubs/{HUB}/calibration-history", headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["attempts"][0]["command_status"] == "SIGNED"


def test_work_orders_filter_by_status(pq_client, pq_store) -> None:
    pq_store.orders = [{"hub_id": HUB, "status": "OPEN"}, {"hub_id": HUB_2, "status": "CLOSED"}]
    resp = pq_client.get("/og/api/work-orders", params={"status": "OPEN"}, headers=VIEWER_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == [{"hub_id": HUB, "status": "OPEN"}]
    assert len(pq_client.get("/og/api/work-orders", headers=VIEWER_HEADERS).json()) == 2


def test_work_orders_unknown_status_is_422(pq_client) -> None:
    resp = pq_client.get("/og/api/work-orders", params={"status": "BOGUS"}, headers=VIEWER_HEADERS)
    assert resp.status_code == 422


# -- waveform capture (two-step) -------------------------------------------------------------------------


def _propose(client: TestClient, action: str, hub_id: str = HUB) -> str:
    resp = client.post(f"/og/api/hubs/{hub_id}/{action}", headers=OPERATOR_HEADERS, json={"reason": "check"})
    assert resp.status_code == 202
    return resp.json()["proposal_id"]


def test_ts_15a_capture_propose_publishes_nothing_until_confirmed(pq_client, publisher, fake_store) -> None:
    proposal_id = _propose(pq_client, "waveform-capture")
    assert publisher.published == []
    resp = pq_client.post(
        f"/og/api/hubs/{HUB}/waveform-capture/{proposal_id}/confirm", headers=OPERATOR_HEADERS
    )
    assert resp.status_code == 200
    assert len(publisher.published) == 1
    topic_suffix, payload = publisher.published[0]
    assert topic_suffix == f"scada/wave/north/{BANK}/{HUB}/request"
    validate_payload("waveform_capture_request", payload)
    assert payload["trigger_reason"] == "API_REQUEST"
    assert payload["request_id"] == resp.json()["request_id"]
    assert fake_store.operator_actions[-1]["target_ref"] == f"hub:{HUB}"


def test_capture_confirm_is_single_use(pq_client) -> None:
    proposal_id = _propose(pq_client, "waveform-capture")
    url = f"/og/api/hubs/{HUB}/waveform-capture/{proposal_id}/confirm"
    assert pq_client.post(url, headers=OPERATOR_HEADERS).status_code == 200
    assert pq_client.post(url, headers=OPERATOR_HEADERS).status_code == 404


def test_capture_confirm_for_another_hub_is_404_and_keeps_the_proposal(pq_client) -> None:
    proposal_id = _propose(pq_client, "waveform-capture")
    wrong = pq_client.post(
        f"/og/api/hubs/{HUB_2}/waveform-capture/{proposal_id}/confirm", headers=OPERATOR_HEADERS
    )
    assert wrong.status_code == 404
    right = pq_client.post(
        f"/og/api/hubs/{HUB}/waveform-capture/{proposal_id}/confirm", headers=OPERATOR_HEADERS
    )
    assert right.status_code == 200


def test_capture_confirm_expired_proposal_is_410(pq_client, fake_proposals, publisher) -> None:
    proposal_id = _propose(pq_client, "waveform-capture")
    fake_proposals._proposals[UUID(proposal_id)].created_at -= 120.0
    resp = pq_client.post(
        f"/og/api/hubs/{HUB}/waveform-capture/{proposal_id}/confirm", headers=OPERATOR_HEADERS
    )
    assert resp.status_code == 410
    assert publisher.published == []


def test_capture_propose_unknown_hub_is_404(pq_client) -> None:
    resp = pq_client.post(
        "/og/api/hubs/nope/waveform-capture", headers=OPERATOR_HEADERS, json={"reason": "x"}
    )
    assert resp.status_code == 404


def test_capture_proposal_cannot_confirm_a_calibration(pq_client, asset_ports) -> None:
    proposal_id = _propose(pq_client, "waveform-capture")
    resp = pq_client.post(f"/og/api/hubs/{HUB}/calibrate/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404
    assert asset_ports.recorded == []


# -- calibration (two-step; the API never signs) ---------------------------------------------------------


def test_g25_calibrate_confirm_records_a_pending_attempt_for_the_guardian(
    pq_client, asset_ports, fake_store
) -> None:
    proposal_id = _propose(pq_client, "calibrate")
    assert asset_ports.recorded == []
    resp = pq_client.post(f"/og/api/hubs/{HUB}/calibrate/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["outcome"] == "PENDING"
    assert "signature" not in body
    assert len(asset_ports.recorded) == 1
    attempt = asset_ports.recorded[0]
    assert attempt["hub_id"] == HUB
    assert attempt["command_batch_id"] is None
    assert str(attempt["calibration_id"]) == body["calibration_id"]
    assert body["correction"]["freq_hz"] == pytest.approx(-0.05)
    assert asset_ports.traced[0][0] == "CALIBRATION_ATTEMPT"
    assert fake_store.operator_actions[-1]["action_kind"] == "MANUAL_COMMAND"


def test_calibrate_refused_by_ladder_primary_check_is_409(pq_client, asset_ports) -> None:
    asset_ports.sensitive_grant = True
    proposal_id = _propose(pq_client, "calibrate")
    resp = pq_client.post(f"/og/api/hubs/{HUB}/calibrate/{proposal_id}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    assert asset_ports.recorded == []


def test_calibrate_confirm_unknown_proposal_is_404(pq_client) -> None:
    resp = pq_client.post(f"/og/api/hubs/{HUB}/calibrate/{uuid4()}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


def test_calibrate_requires_a_reason(pq_client) -> None:
    resp = pq_client.post(f"/og/api/hubs/{HUB}/calibrate", headers=OPERATOR_HEADERS, json={"reason": ""})
    assert resp.status_code == 422


def test_create_app_mounts_the_pq_router() -> None:
    """The one-line mount in `api/app.py` (merge of #17): every WP-J path is served by `create_app()`."""
    paths = set(create_app().openapi()["paths"])
    for path in (
        "/og/api/hubs/{hub_id}/waveform",
        "/og/api/hubs/{hub_id}/spectrum",
        "/og/api/banks/{bank_id}/pq",
        "/og/api/obligations/{obligation_id}/pq-compliance",
        "/og/api/hubs/{hub_id}/asset-health",
        "/og/api/hubs/{hub_id}/calibration-history",
        "/og/api/work-orders",
        "/og/api/hubs/{hub_id}/waveform-capture",
        "/og/api/hubs/{hub_id}/calibrate",
    ):
        assert path in paths, path
