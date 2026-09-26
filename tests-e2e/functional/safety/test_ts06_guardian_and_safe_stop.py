"""Q2: guardian negative tests, safe stop and anomaly responses, against the running dev stack.

Test plan `04-mvp-s-test-plan.md` S3.6-S3.7 (TS-06-06, -07a, -09, -15, -16, -17, -23, TS-07-04, A11). Guardian
checks are driven through the operator's manual-command path (02b S7.3): og-api writes the proposal and the
independent og-guardian process judges it, so every verdict asserted here is the real guardian's. Anomalies go
through `ogsim.control`, the same path as its web UI.

`STOP_BANK` is the one bank these tests safe-stop. It is left engaged afterwards whenever the two-person
release does not complete (see `test_a_second_authorised_operator_releases_the_stop`), so every other
scenario keeps away from it.
"""

from __future__ import annotations

import os

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

STOP_BANK = "bank-007"
OPERATOR_A = "e2e-alice"
OPERATOR_B = "e2e-bob"
#: og-guardian's `[guardian].inverter_cap_kw` (G-02 bounds a hub by the smaller of this and its rated kW).
INVERTER_CAP_KW = float(os.environ.get("OG_E2E_INVERTER_CAP_KW", "11.0"))


def _verdict(resp) -> dict:
    body = resp.json()
    return body.get("detail", body) if isinstance(body.get("detail"), dict) else body


# --- guardian negatives via the manual-command path -------------------------------------------------


def test_a_command_that_holds_the_current_setpoint_is_signed(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,), idle=True)

    resp = stack.manual_command(hub["hub_id"], float(hub["p_kw"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "PASS"


def test_ts_06_07a_g02_a_setpoint_above_the_hub_power_limit_is_vetoed(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,))

    resp = stack.manual_command(hub["hub_id"], float(hub["p_limit_kw"]) * 3)

    assert resp.status_code == 409, resp.text
    verdict = _verdict(resp)
    assert verdict["outcome"] in {"VETOED", "PARTLY_VETOED"}
    assert "G-02" in verdict["vetoed_rule_ids"]


def test_ts_06_09_g04_a_step_beyond_the_hub_ramp_limit_is_vetoed(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,), idle=True)

    within_power_limit = 0.9 * min(float(hub["p_limit_kw"]), INVERTER_CAP_KW)

    resp = stack.manual_command(hub["hub_id"], within_power_limit)

    assert resp.status_code == 409, resp.text
    verdict = _verdict(resp)
    assert "G-04" in verdict["vetoed_rule_ids"]
    assert "G-02" not in verdict["vetoed_rule_ids"], "the setpoint is within the hub limit"


def test_ts_06_06_g01_a_hub_reporting_soc_below_reserve_is_refused(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,))
    anomaly = stack.inject("soc_sensor_drift", hub["hub_id"], duration_s=120, drift_kwh_per_min=-60.0)
    try:
        drained = wait_until(
            lambda: (
                row
                if (
                    row := stack.rows(
                        "SELECT soc_kwh, p_kw FROM og.hub_state WHERE hub_id = %(h)s", {"h": hub["hub_id"]}
                    )[0]
                )["soc_kwh"]
                < hub["r_kwh"]
                else None
            ),
            timeout_s=60,
            what=f"{hub['hub_id']} to report SoC below its reserve",
        )

        resp = stack.manual_command(hub["hub_id"], float(drained["p_kw"]))
    finally:
        stack.clear_anomaly(anomaly)

    assert resp.status_code == 409, resp.text
    assert "G-01" in _verdict(resp)["vetoed_rule_ids"]


def test_every_veto_is_traced_with_its_rule(stack: Stack) -> None:
    started = now_utc()
    hub = stack.online_hub(exclude_banks=(STOP_BANK,))

    resp = stack.manual_command(hub["hub_id"], float(hub["p_limit_kw"]) * 3)

    trace_id = _verdict(resp)["trace_id"]
    traced = stack.rows(
        "SELECT decision_type, payload FROM og.trace WHERE trace_id = %(t)s AND created_at >= %(s)s",
        {"t": trace_id, "s": started},
    )
    assert traced, f"verdict trace row {trace_id} missing (K10)"


# --- safe stop: scope, two-step, two-person release -------------------------------------------------


def _engage(stack: Stack, scope: str, scope_id: str) -> dict:
    proposed = stack.post("/safestop", {"scope": scope, "scope_id": scope_id, "reason": "e2e"})
    assert proposed.status_code == 202, proposed.text
    confirmed = stack.post(f"/safestop/{proposed.json()['proposal_id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_ts_06_16_a_bank_safe_stop_only_stops_that_bank(stack: Stack) -> None:
    started = now_utc()

    engaged = _engage(stack, "bank", STOP_BANK)

    assert engaged["engaged"] is True
    event = stack.rows(
        "SELECT action, scope_kind, scope_ref FROM og.stop_event WHERE created_at >= %(t)s AND action = 'ENGAGE'",
        {"t": started},
    )
    assert [(e["scope_kind"], e["scope_ref"]) for e in event] == [("BANK", STOP_BANK)]

    inside = stack.rows(
        "SELECT h.hub_id, s.p_kw FROM og.hub h JOIN og.hub_state s USING (hub_id) "
        "WHERE h.bank_id = %(b)s AND s.health = 'online' LIMIT 1",
        {"b": STOP_BANK},
    )[0]
    into_stop = stack.manual_command(inside["hub_id"], float(inside["p_kw"]))
    assert into_stop.status_code == 409, into_stop.text
    assert "SAFE_STOP" in _verdict(into_stop)["vetoed_rule_ids"]

    outside = stack.online_hub(exclude_banks=(STOP_BANK,), idle=True)
    elsewhere = stack.manual_command(outside["hub_id"], float(outside["p_kw"]))
    assert elsewhere.status_code == 200, "a bank-scoped stop must not stop other banks"


def test_ts_06_15_a_safe_stop_never_engages_on_one_message(stack: Stack) -> None:
    started = now_utc()

    proposed = stack.post("/safestop", {"scope": "fleet", "scope_id": None, "reason": "e2e, never confirmed"})

    assert proposed.status_code == 202, proposed.text
    assert not stack.rows(
        "SELECT 1 FROM og.stop_event WHERE created_at >= %(t)s AND scope_kind = 'FLEET'", {"t": started}
    ), "a proposal alone engaged a stop"


def test_a_release_cannot_be_approved_by_its_requester(stack: Stack) -> None:
    _engage(stack, "bank", STOP_BANK)
    requested = stack.post(f"/safestop/bank/{STOP_BANK}/release", {"reason": "e2e"}, user=OPERATOR_A)
    if requested.status_code == 403:
        pytest.skip(f"{OPERATOR_A} is not an operator on this stack ([api.roles.operator])")
    assert requested.status_code == 202, requested.text

    self_approved = stack.post(
        f"/safestop/release/{requested.json()['proposal_id']}/approve", user=OPERATOR_A
    )

    assert self_approved.status_code == 403, self_approved.text


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: og-api writes the SAFE_STOP_RELEASE row only at approval, with confirmed_at taken just before the "
        "insert, and og-guardian reads the row's created_at as requested_at -- approved_at is always a few ms "
        "earlier than requested_at, so every release is refused APPROVAL_STALE (api/routers/safestop.py "
        "approve_release, guardian/repo.py release mapping)."
    ),
)
def test_a_second_authorised_operator_releases_the_stop(stack: Stack) -> None:
    _engage(stack, "bank", STOP_BANK)
    requested = stack.post(f"/safestop/bank/{STOP_BANK}/release", {"reason": "e2e"}, user=OPERATOR_A)
    if requested.status_code == 403:
        pytest.skip(f"{OPERATOR_A} is not an operator on this stack ([api.roles.operator])")
    started = now_utc()

    approved = stack.post(f"/safestop/release/{requested.json()['proposal_id']}/approve", user=OPERATOR_B)

    assert approved.status_code in {200, 202}, approved.text
    wait_until(
        lambda: stack.rows(
            "SELECT 1 FROM og.stop_event WHERE action = 'RELEASE' AND scope_ref = %(b)s AND created_at >= %(t)s",
            {"b": STOP_BANK, "t": started},
        ),
        timeout_s=30,
        what="the guardian-signed RELEASE",
    )


# --- process loss --------------------------------------------------------------------------------


def test_ts_06_17_with_the_guardian_down_a_command_is_never_treated_as_passed(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,), idle=True)
    stack.compose("stop", "og-guardian")
    try:
        resp = stack.manual_command(hub["hub_id"], float(hub["p_kw"]))

        assert resp.status_code == 503, f"expected 'no verdict' (503), got {resp.status_code}: {resp.text}"
    finally:
        stack.compose("start", "og-guardian")
        stack.wait_process_up("guardian")

    recovered = stack.manual_command(hub["hub_id"], float(hub["p_kw"]))
    assert recovered.status_code == 200, "the guardian did not resume signing after restart"


def test_ts_06_23_safe_stop_engages_with_engine_and_guardian_both_down(stack: Stack) -> None:
    stack.compose("stop", "og-engine", "og-guardian")
    try:
        started = now_utc()

        engaged = _engage(stack, "bank", STOP_BANK)

        assert engaged["engaged"] is True
        assert stack.rows(
            "SELECT 1 FROM og.stop_event WHERE action = 'ENGAGE' AND scope_ref = %(b)s AND created_at >= %(t)s",
            {"b": STOP_BANK, "t": started},
        )
    finally:
        stack.compose("start", "og-engine", "og-guardian")
        stack.wait_process_up("engine")
        stack.wait_process_up("guardian")


# --- anomaly responses (TS-07) -------------------------------------------------------------------


def test_ts_07_04_an_offline_hub_is_classified_and_recovers(stack: Stack) -> None:
    hub = stack.online_hub(exclude_banks=(STOP_BANK,))
    anomaly = stack.inject("hub_offline", hub["hub_id"], duration_s=45)
    try:
        wait_until(
            lambda: stack.get(f"/fleet/hubs/{hub['hub_id']}").json().get("health") in {"stale", "offline"},
            timeout_s=40,
            what=f"{hub['hub_id']} to be classified stale/offline",
        )
    finally:
        stack.clear_anomaly(anomaly)

    wait_until(
        lambda: stack.get(f"/fleet/hubs/{hub['hub_id']}").json().get("health") == "online",
        timeout_s=60,
        what=f"{hub['hub_id']} to recover online",
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING (dev stack): an ogsim.control `bank_overload` injection never changes the SCADA sim's readings "
        "(bank-005 stayed <= 382 kVA of 600 with +60% injected), while fleet anomalies from the same control "
        "plane apply -- so ALR-SCADA-OVERLOAD cannot be exercised here yet."
    ),
)
def test_a11_a_scada_bank_overload_raises_an_alert_that_clears(stack: Stack) -> None:
    started = now_utc()
    bank = "bank-005"
    anomaly = stack.inject("bank_overload", bank, duration_s=120, kva_over_rating_pct=60.0)
    try:
        wait_until(
            lambda: stack.rows(
                "SELECT 1 FROM og.alert WHERE rule = 'ALR-SCADA-OVERLOAD' AND opened_at >= %(t)s",
                {"t": started},
            ),
            timeout_s=90,
            what="ALR-SCADA-OVERLOAD",
        )
    finally:
        stack.clear_anomaly(anomaly)

    wait_until(
        lambda: stack.rows(
            "SELECT 1 FROM og.alert WHERE rule = 'ALR-SCADA-OVERLOAD' AND opened_at >= %(t)s "
            "AND cleared_at IS NOT NULL",
            {"t": started},
        ),
        timeout_s=120,
        what="ALR-SCADA-OVERLOAD to clear",
    )
