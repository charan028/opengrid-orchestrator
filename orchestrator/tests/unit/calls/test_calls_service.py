"""`opengrid.calls` (D-29, D-33): the one call path every origin shares. Every refusal path, the sign
convention (+charge/-discharge, discharge only), idempotency, utility isolation (404 + AUTHZ_DENY),
cancel/shorten, the status states and the alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid.calls import (
    CallLimits,
    CallOrigin,
    CallOutcome,
    CallRefused,
    CallRequest,
    CallState,
    call_status,
    cancel_call,
    cancel_deployment,
    find_call_by_key,
    issue_call,
    list_calls,
)
from opengrid.calls import rules as r
from opengrid.calls.models import Granted

from .fakes import FakeDeployment, make_award

NOW = datetime(2026, 9, 26, 21, 40, tzinfo=UTC)  # 16:40 CT, inside a 16:30-18:00 toll window
WINDOW = (datetime(2026, 9, 26, 21, 30, tzinfo=UTC), datetime(2026, 9, 26, 23, 0, tzinfo=UTC))
LIMITS = CallLimits()
AEN = "AUSTIN_ENERGY"


def _toll(store, **kw):
    kw.setdefault("window_start", WINDOW[0])
    kw.setdefault("window_end", WINDOW[1])
    return store.add(make_award(**kw))


def _utility(obligation_id=None, **kw) -> CallRequest:
    base = {
        "origin": CallOrigin.UTILITY,
        "principal": "og-util-aen",
        "reason": "AE peak",
        "duration_minutes": 60,
        "obligation_id": obligation_id,
        "utility_id": AEN,
        "requested_kw": -20_000.0,
        "idempotency_key": f"k-{uuid4()}",
    }
    return CallRequest(**{**base, **kw})


async def _refused(store, trace, request, *, now=NOW, limits=LIMITS) -> CallRefused:
    with pytest.raises(CallRefused) as info:
        await issue_call(store, trace, request, limits=limits, now=now)
    return info.value


# --- accept --------------------------------------------------------------------------------------------


async def test_ts_d33_01_a_utility_call_is_accepted_traced_audited_and_alerted(store, trace) -> None:
    oid = _toll(store)
    record = await issue_call(store, trace, _utility(oid), limits=LIMITS, now=NOW)

    assert record.outcome is CallOutcome.ACCEPTED
    assert record.origin is CallOrigin.UTILITY and record.principal == "og-util-aen"
    assert record.requested_kw == -20_000.0 and record.target_kw == -20_000.0
    assert record.reason.startswith("utility call: ")
    (deployment,) = store.deployments.values()
    assert deployment.source == "UTILITY" and deployment.requested_kw == -20_000.0
    assert deployment.end_at - deployment.start_at == timedelta(minutes=60)
    (event,) = trace.events
    assert event["event_class"] == "DISPATCH_CALL"
    assert event["payload"]["origin"] == "UTILITY" and event["payload"]["principal"] == "og-util-aen"
    assert record.trace_id == event["trace_id"]  # traced before it takes effect (K10)
    (action,) = store.operator_actions
    assert action["target_ref"] == f"UTILITY_CALL:{oid}" and action["operator_ref"] == "og-util-aen"
    (alert,) = store.alerts
    assert alert["rule"] == "ALR-UTILITY-CALL"


async def test_ts_d33_02_the_utility_obligation_is_resolved_from_the_window(store, trace) -> None:
    oid = _toll(store)
    _toll(store, utility_id="CPS_ENERGY")
    record = await issue_call(store, trace, _utility(None), limits=LIMITS, now=NOW)
    assert record.obligation_id == oid


async def test_ts_d33_03_an_operator_call_raises_no_alert_and_takes_full_committed_kw(store, trace) -> None:
    oid = _toll(store)
    request = CallRequest(
        origin=CallOrigin.OPERATOR, principal="operator", reason="x", duration_minutes=30, obligation_id=oid
    )
    record = await issue_call(store, trace, request, limits=LIMITS, now=NOW)
    assert record.requested_kw is None and record.target_kw == -26_000.0
    assert store.alerts == []


# --- every refusal path --------------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [0.0, 5.0, 20_000.0])
async def test_ts_d33_04_a_charge_or_zero_kw_is_refused_sign_convention(store, trace, kw) -> None:
    exc = await _refused(store, trace, _utility(_toll(store), requested_kw=kw))
    assert (exc.reason_code, exc.http_status) == (r.R_CHARGE_REFUSED, 422)
    assert store.deployments == {}
    assert exc.call is not None and exc.call.outcome is CallOutcome.REFUSED  # recorded
    assert store.alerts[-1]["rule"] == "ALR-UTILITY-CALL-REFUSED"
    assert "DISPATCH_CALL_REFUSED" in trace.classes()


async def test_ts_d33_05_over_the_90_minute_product_cap_is_refused(store, trace) -> None:
    oid = _toll(store, window_end=WINDOW[0] + timedelta(hours=3))
    ok = await issue_call(store, trace, _utility(oid, duration_minutes=90), limits=LIMITS, now=NOW)
    assert ok.outcome is CallOutcome.ACCEPTED
    other = _toll(store, window_end=WINDOW[0] + timedelta(hours=3))
    exc = await _refused(store, trace, _utility(other, duration_minutes=91))
    assert exc.reason_code == r.R_DURATION_CAP and exc.http_status == 409


async def test_ts_d33_06_an_overlapping_call_is_refused_409(store, trace) -> None:
    oid = _toll(store)
    await issue_call(store, trace, _utility(oid, duration_minutes=30), limits=LIMITS, now=NOW)
    later = NOW + timedelta(minutes=20)
    exc = await _refused(store, trace, _utility(oid, start_at=later, duration_minutes=10))
    assert exc.reason_code == r.R_OVERLAP and exc.http_status == 409
    assert len(store.deployments) == 1
    # Back-to-back (after the first ends) is not an overlap.
    after = NOW + timedelta(minutes=30)
    ok = await issue_call(
        store, trace, _utility(oid, start_at=after, duration_minutes=10), limits=LIMITS, now=NOW
    )
    assert ok.outcome is CallOutcome.ACCEPTED


async def test_ts_d33_07_more_than_committed_kw_is_refused(store, trace) -> None:
    exc = await _refused(store, trace, _utility(_toll(store), requested_kw=-26_000.5))
    assert exc.reason_code == r.R_OVER_COMMITTED


@pytest.mark.parametrize(
    ("start", "minutes"),
    [(WINDOW[0] - timedelta(minutes=5), 10), (WINDOW[1] - timedelta(minutes=10), 20), (WINDOW[1], 5)],
)
async def test_ts_d33_08_outside_the_reservation_window_is_refused(store, trace, start, minutes) -> None:
    oid = _toll(store)
    exc = await _refused(
        store,
        trace,
        _utility(oid, start_at=start, duration_minutes=minutes),
        now=WINDOW[0] - timedelta(hours=1),
    )
    assert exc.reason_code == r.R_OUTSIDE_WINDOW


async def test_ts_d33_09_a_window_already_ended_is_refused(store, trace) -> None:
    oid = _toll(store)
    request = _utility(oid, start_at=NOW - timedelta(minutes=30), duration_minutes=None, end_at=NOW)
    exc = await _refused(store, trace, request)
    assert exc.reason_code == r.R_WINDOW_PASSED


@pytest.mark.parametrize("state", ["OFFERED", "SELECTED", "FULFILLED", "SETTLED", "REJECTED"])
async def test_ts_d33_10_a_non_deployable_state_is_refused(store, trace, state) -> None:
    exc = await _refused(store, trace, _utility(_toll(store, state=state)))
    assert exc.reason_code == r.R_STATE


async def test_ts_d33_11_a_product_without_duration_is_refused(store, trace) -> None:
    exc = await _refused(store, trace, _utility(_toll(store, duration_minutes=None)))
    assert exc.reason_code == r.R_NO_PRODUCT_DURATION


@pytest.mark.parametrize(
    ("origin", "service_type", "variant"),
    [
        (CallOrigin.UTILITY, "ERCOT_AS", None),
        (CallOrigin.GRID_LINK, "ERCOT_AS", None),
        (CallOrigin.ERCOT_POLL, "REGULATED_CAPACITY", "TOLLING"),
        (CallOrigin.MARKET_SIM, "REGULATED_CAPACITY", "TOLLING"),
        (CallOrigin.OPERATOR, "REGULATED_CAPACITY", "FIRM"),
        (CallOrigin.OPERATOR, "PARTNER_CAPACITY", None),
    ],
)
async def test_ts_d33_12_each_origin_deploys_only_its_own_kinds(
    store, trace, origin, service_type, variant
) -> None:
    oid = _toll(store, service_type=service_type, variant=variant, utility_id=None)
    request = CallRequest(
        origin=origin, principal="p", reason="x", duration_minutes=10, obligation_id=oid, requested_kw=-1.0
    )
    exc = await _refused(store, trace, request)
    assert exc.reason_code == r.R_NOT_DEPLOYABLE


async def test_ts_d33_13_unknown_obligation_is_404_and_needs_an_id_without_utility(store, trace) -> None:
    exc = await _refused(store, trace, _utility(uuid4()))
    assert (exc.reason_code, exc.http_status) == (r.R_NOT_FOUND, 404)
    assert exc.call is not None and exc.call.obligation_id is None  # nothing about the id is stored
    no_id = CallRequest(origin=CallOrigin.OPERATOR, principal="operator", reason="x", duration_minutes=5)
    assert (await _refused(store, trace, no_id)).reason_code == r.R_NO_OBLIGATION


async def test_ts_d33_14_fleet_wide_is_refused(store, trace) -> None:
    request = CallRequest(
        origin=CallOrigin.OPERATOR, principal="operator", reason="x", duration_minutes=5, fleet_wide=True
    )
    assert (await _refused(store, trace, request)).reason_code == r.R_FLEET_WIDE


async def test_ts_d33_15_rate_limit_is_429_and_not_recorded(store, trace) -> None:
    limits = CallLimits(max_calls_per_hour=2, max_calls_per_day=2)
    oid = _toll(store)
    for _ in range(2):
        await _refused(store, trace, _utility(oid, requested_kw=1.0), limits=limits)
    before = len(store.calls)
    exc = await _refused(store, trace, _utility(oid), limits=limits)
    assert (exc.reason_code, exc.http_status) == (r.R_RATE_LIMIT, 429)
    assert len(store.calls) == before


# --- utility isolation ---------------------------------------------------------------------------------


async def test_ts_d33_16_another_utilitys_obligation_is_404_and_traced_authz_deny(store, trace) -> None:
    cps = _toll(store, utility_id="CPS_ENERGY")
    exc = await _refused(store, trace, _utility(cps))
    assert (exc.reason_code, exc.http_status) == (r.R_NOT_FOUND, 404)
    assert exc.detail == "no such obligation"  # identical to a missing one
    deny = [e for e in trace.events if e["event_class"] == "TRACE_AUTHZ_DENY"]
    assert deny and deny[0]["decision_type"] == "AUTHZ_DENY"
    assert store.deployments == {}


async def test_ts_d33_17_another_utilitys_call_is_404_on_status_and_cancel(store, trace) -> None:
    record = await issue_call(store, trace, _utility(_toll(store)), limits=LIMITS, now=NOW)
    for op in (
        call_status(store, trace, record.call_id, principal="og-util-cps", utility_id="CPS_ENERGY", now=NOW),
        cancel_call(
            store,
            trace,
            record.call_id,
            origin=CallOrigin.UTILITY,
            principal="og-util-cps",
            utility_id="CPS_ENERGY",
        ),
    ):
        with pytest.raises(CallRefused) as info:
            await op
        assert info.value.http_status == 404
    assert trace.classes().count("TRACE_AUTHZ_DENY") == 2
    assert [c.call_id for c in await list_calls(store, utility_id="CPS_ENERGY")] == []


# --- idempotency ---------------------------------------------------------------------------------------


async def test_ts_d33_18_a_replayed_key_returns_the_original_call(store, trace) -> None:
    request = _utility(_toll(store), idempotency_key="aen-1")
    first = await issue_call(store, trace, request, limits=LIMITS, now=NOW)
    again = await issue_call(store, trace, request, limits=LIMITS, now=NOW + timedelta(seconds=5))
    assert again.replayed and again.call_id == first.call_id
    assert len(store.deployments) == 1
    assert (await find_call_by_key(store, "og-util-aen", "aen-1")).call_id == first.call_id


async def test_ts_d33_19_a_reused_key_with_other_params_is_409(store, trace) -> None:
    oid = _toll(store)
    await issue_call(store, trace, _utility(oid, idempotency_key="aen-2"), limits=LIMITS, now=NOW)
    exc = await _refused(store, trace, _utility(oid, idempotency_key="aen-2", requested_kw=-10.0))
    assert exc.reason_code == r.R_IDEMPOTENCY_CONFLICT


async def test_ts_d33_20_a_replayed_refusal_is_refused_again_without_a_new_row(store, trace) -> None:
    request = _utility(_toll(store), idempotency_key="aen-3", requested_kw=10.0)
    first = await _refused(store, trace, request)
    rows = len(store.calls)
    again = await _refused(store, trace, request)
    assert again.reason_code == first.reason_code and again.http_status == 422
    assert again.call is not None and again.call.replayed
    assert len(store.calls) == rows


async def test_ts_d33_21_the_same_key_is_independent_per_principal(store, trace) -> None:
    oid = _toll(store, window_end=WINDOW[0] + timedelta(hours=4))
    await issue_call(
        store, trace, _utility(oid, idempotency_key="k", duration_minutes=10), limits=LIMITS, now=NOW
    )
    other = CallRequest(
        origin=CallOrigin.OPERATOR,
        principal="operator",
        reason="x",
        obligation_id=oid,
        duration_minutes=10,
        start_at=NOW + timedelta(minutes=30),
        idempotency_key="k",
    )
    assert not (await issue_call(store, trace, other, limits=LIMITS, now=NOW)).replayed


# --- cancel / shorten / status -------------------------------------------------------------------------


async def test_ts_d33_22_cancel_mid_call_ends_it_and_status_is_completed(store, trace) -> None:
    oid = _toll(store)
    record = await issue_call(store, trace, _utility(oid), limits=LIMITS, now=NOW)
    mid = NOW + timedelta(minutes=10)
    ended = await cancel_call(
        store,
        trace,
        record.call_id,
        origin=CallOrigin.UTILITY,
        principal="og-util-aen",
        utility_id=AEN,
        now=mid,
    )
    assert ended.cancelled_at == mid
    assert "DISPATCH_CALL_END" in trace.classes()
    status = await call_status(store, trace, record.call_id, principal="og-util-aen", utility_id=AEN, now=mid)
    assert status.state is CallState.COMPLETED
    with pytest.raises(CallRefused) as info:
        await cancel_call(
            store, trace, record.call_id, origin=CallOrigin.UTILITY, principal="og-util-aen", now=mid
        )
    assert info.value.reason_code == r.R_ALREADY_ENDED


async def test_ts_d33_23_shorten_but_never_extend(store, trace) -> None:
    record = await issue_call(store, trace, _utility(_toll(store)), limits=LIMITS, now=NOW)
    with pytest.raises(CallRefused) as info:
        await cancel_call(
            store,
            trace,
            record.call_id,
            origin=CallOrigin.UTILITY,
            principal="og-util-aen",
            end_at=record.end_at + timedelta(minutes=1),
            now=NOW,
        )
    assert info.value.reason_code == r.R_CANNOT_EXTEND
    shorter = record.end_at - timedelta(minutes=20)
    updated = await cancel_call(
        store,
        trace,
        record.call_id,
        origin=CallOrigin.UTILITY,
        principal="og-util-aen",
        end_at=shorter,
        now=NOW,
    )
    assert updated.end_at == shorter and updated.cancelled_at is None


async def test_ts_d33_24_status_states_follow_start_delivery_and_end(store, trace) -> None:
    oid = _toll(store)
    start = NOW + timedelta(minutes=5)
    record = await issue_call(
        store, trace, _utility(oid, start_at=start, duration_minutes=30), limits=LIMITS, now=NOW
    )

    async def state_at(when):
        return await call_status(
            store, trace, record.call_id, principal="og-util-aen", utility_id=AEN, now=when
        )

    assert (await state_at(NOW)).state is CallState.ACCEPTED
    store.delivery[oid] = Granted(last_kw=5_000.0, kwh=100.0)
    active = await state_at(start + timedelta(minutes=1))
    assert active.state is CallState.ACTIVE  # r3.4.1: unmeasured, never RAMPING/DELIVERING from grants
    assert active.granted_kw == -5_000.0 and active.granted_kwh == 100.0  # signed: discharge < 0
    assert active.public()["delivery_state"] == "UNMEASURED" and active.public()["delivery_measured"] is False
    store.delivery[oid] = Granted(last_kw=19_500.0, kwh=900.0)
    assert (await state_at(start + timedelta(minutes=5))).state is CallState.ACTIVE  # even at target
    assert (await state_at(start + timedelta(minutes=31))).state is CallState.COMPLETED


async def test_ts_d33_25_a_refused_call_reports_refused_with_its_reason(store, trace) -> None:
    exc = await _refused(store, trace, _utility(_toll(store), requested_kw=100.0))
    status = await call_status(
        store, trace, exc.call.call_id, principal="og-util-aen", utility_id=AEN, now=NOW
    )
    assert status.state is CallState.REFUSED
    assert status.public()["reason_code"] == r.R_CHARGE_REFUSED


async def test_ts_d33_26_cancel_deployment_works_for_ledger_and_legacy_rows(store, trace) -> None:
    record = await issue_call(store, trace, _utility(_toll(store)), limits=LIMITS, now=NOW)
    later = NOW + timedelta(minutes=5)
    stop = {"origin": CallOrigin.OPERATOR, "principal": "operator", "now": later}
    assert await cancel_deployment(store, trace, record.deployment_id, **stop)
    assert store.deployments[record.deployment_id].cancelled_at == later
    assert not await cancel_deployment(store, trace, record.deployment_id, **stop)
    legacy = FakeDeployment(
        uuid4(), uuid4(), NOW, NOW + timedelta(hours=1), "MARKET_SIM", "m", "r", None, None
    )
    store.deployments[legacy.deployment_id] = legacy
    assert await cancel_deployment(store, trace, legacy.deployment_id, **stop)
    assert legacy.cancelled_at == later


async def test_ts_d33_27_an_alert_failure_never_blocks_a_call(store, trace) -> None:
    store.fail_alerts = True
    record = await issue_call(store, trace, _utility(_toll(store)), limits=LIMITS, now=NOW)
    assert record.outcome is CallOutcome.ACCEPTED


def test_ts_d33_28_request_needs_exactly_one_bound_and_aware_times() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CallRequest(origin=CallOrigin.OPERATOR, principal="p", reason="x")
    with pytest.raises(ValueError, match="timezone"):
        CallRequest(
            origin=CallOrigin.OPERATOR,
            principal="p",
            reason="x",
            duration_minutes=5,
            start_at=datetime(2026, 1, 1),
        )


def test_ts_d33_29_limits_come_from_config_and_are_validated() -> None:
    class _Cfg:
        def get(self, key, default):
            return {"dispatch.calls.max_calls_per_hour": 3, "dispatch.calls.max_calls_per_day": 9}.get(
                key, default
            )

    assert CallLimits.from_config(_Cfg()) == CallLimits(max_calls_per_hour=3, max_calls_per_day=9)
    with pytest.raises(ValueError):
        CallLimits(max_calls_per_hour=10, max_calls_per_day=5)
    with pytest.raises(ValueError):
        CallLimits(ramping_fraction=0.0)


async def test_ts_d33_44_cancelling_an_ended_call_is_already_ended_not_cannot_extend(store, trace) -> None:
    record = await issue_call(
        store, trace, _utility(_toll(store), duration_minutes=10), limits=LIMITS, now=NOW
    )
    later = NOW + timedelta(minutes=30)
    for end_at in (None, later + timedelta(minutes=5)):
        with pytest.raises(CallRefused) as info:
            await cancel_call(
                store,
                trace,
                record.call_id,
                origin=CallOrigin.UTILITY,
                principal="og-util-aen",
                end_at=end_at,
                now=later,
            )
        assert info.value.reason_code == r.R_ALREADY_ENDED


async def test_ts_d33_45_only_the_issuer_or_an_operator_may_cancel(store, trace) -> None:
    oid = _toll(store, window_end=WINDOW[0] + timedelta(hours=3))
    record = await issue_call(store, trace, _utility(oid), limits=LIMITS, now=NOW)
    for origin, principal in (
        (CallOrigin.UTILITY, "og-util-other"),
        (CallOrigin.GRID_LINK, "grid_link:AUSTIN_ENERGY"),
    ):
        with pytest.raises(CallRefused) as info:
            await cancel_call(store, trace, record.call_id, origin=origin, principal=principal, now=NOW)
        assert (info.value.reason_code, info.value.http_status) == (r.R_NOT_ISSUER, 403)
    assert trace.classes().count("TRACE_AUTHZ_DENY") == 2
    refused = [a for a in store.alerts if a["detail"].get("reason_code") == r.R_NOT_ISSUER]
    assert [a["rule"] for a in refused] == ["ALR-UTILITY-CALL-REFUSED"] * 2
    assert {a["severity"] for a in refused} == {"warning"}
    assert {a["detail"]["scope_ref"] for a in refused} == {
        f"og-util-other:{record.call_id}",
        f"grid_link:AUSTIN_ENERGY:{record.call_id}",
    }  # keyed per (principal, call): the store coalesces repeats of the same key
    ended = await cancel_call(
        store, trace, record.call_id, origin=CallOrigin.OPERATOR, principal="operator", now=NOW
    )
    assert ended.cancelled_at == NOW


async def test_ts_d33_46_other_cancel_refusals_raise_no_alert(store, trace) -> None:
    record = await issue_call(
        store, trace, _utility(_toll(store), duration_minutes=10), limits=LIMITS, now=NOW
    )
    before = len(store.alerts)
    with pytest.raises(CallRefused):
        await cancel_call(
            store,
            trace,
            record.call_id,
            origin=CallOrigin.UTILITY,
            principal="og-util-aen",
            now=NOW + timedelta(minutes=30),
        )
    assert len(store.alerts) == before
