"""R3: MQTT broker recovery on the dev stack (dev broker only, never the shared one).

- A ~20 s broker outage: og-engine, og-guardian and og-safestop reconnect (their reconnect counters increase) and
  telemetry and guardian verdicts resume.
- A safe stop engaged while the broker is down is delivered to the hubs once it is back.
- A broker outage over 60 s: og-guardian and og-safestop exit and are restarted (systemd in production, the
  container restart policy here). `slow`.

Metrics endpoints are loopback-only inside each container; ports are overridable for a stack that differs.
"""

from __future__ import annotations

import os
import time

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

BANK = "bank-007"
OPERATOR_A = os.environ.get("OG_E2E_OPERATOR_A", "og-op-a")
OPERATOR_B = os.environ.get("OG_E2E_OPERATOR_B", "og-op-b")
RECONNECT_METRICS = {
    "og-engine": (
        int(os.environ.get("OG_E2E_ENGINE_METRICS_PORT", "9101")),
        "og_engine_mqtt_reconnects_total",
    ),
    "og-guardian": (int(os.environ.get("OG_E2E_GUARDIAN_METRICS_PORT", "9102")), "og_mqtt_reconnects_total"),
    "og-safestop": (int(os.environ.get("OG_E2E_SAFESTOP_METRICS_PORT", "9103")), "og_mqtt_reconnects_total"),
}


def _broker_outage(stack: Stack, seconds: float) -> None:
    stack.compose("stop", "mosquitto")
    try:
        time.sleep(seconds)  # the outage itself is the scenario, not a wait for a condition
    finally:
        stack.compose("start", "mosquitto")


def _telemetry_since(stack: Stack, since) -> bool:
    return bool(stack.rows("SELECT 1 FROM og.hub_state WHERE last_seen_at > %(t)s LIMIT 1", {"t": since}))


def _verdicts_since(stack: Stack, since) -> bool:
    return bool(
        stack.rows(
            "SELECT 1 FROM og.verdict v JOIN og.command_batch b USING (command_batch_id) WHERE b.created_at > %(t)s "
            "LIMIT 1",
            {"t": since},
        )
    )


def test_services_reconnect_and_resume_after_a_20_s_broker_outage(stack: Stack) -> None:
    before = {svc: stack.metric(svc, port, name) for svc, (port, name) in RECONNECT_METRICS.items()}
    missing = [svc for svc, value in before.items() if value is None]
    assert not missing, f"no reconnect counter exposed by {missing} (check [metrics] ports on this stack)"

    _broker_outage(stack, 20)
    back = now_utc()

    wait_until(
        lambda: _telemetry_since(stack, back), timeout_s=120, what="hub telemetry after the broker returned"
    )
    wait_until(
        lambda: _verdicts_since(stack, back),
        timeout_s=120,
        what="guardian verdicts after the broker returned",
    )
    for svc, (port, name) in RECONNECT_METRICS.items():
        after = wait_until(
            lambda svc=svc, port=port, name=name: (
                v if (v := stack.metric(svc, port, name)) and v > before[svc] else None
            ),
            timeout_s=90,
            what=f"{svc}'s {name} to increase",
        )
        assert after > before[svc]


def _release(stack: Stack) -> None:
    for _attempt in range(3):  # the guardian refuses while its own clock check (G-20) fails
        requested = stack.post(f"/safestop/bank/{BANK}/release", {"reason": "e2e broker"}, user=OPERATOR_A)
        assert requested.status_code == 202, requested.text
        since = now_utc()
        stack.post(f"/safestop/release/{requested.json()['proposal_id']}/approve", user=OPERATOR_B)
        try:
            wait_until(
                lambda since=since: stack.rows(
                    "SELECT 1 FROM og.stop_event WHERE action = 'RELEASE' AND scope_ref = %(b)s AND created_at >= %(t)s",
                    {"b": BANK, "t": since},
                ),
                timeout_s=30,
                what="the RELEASE",
            )
            return
        except AssertionError:
            continue


def test_a_safe_stop_engaged_during_the_outage_is_delivered_after_reconnect(stack: Stack) -> None:
    stack.compose("stop", "mosquitto")
    try:
        proposed = stack.post(
            "/safestop", {"scope": "bank", "scope_id": BANK, "reason": "e2e broker"}, user=OPERATOR_A
        )
        assert proposed.status_code == 202, proposed.text
        stack.post(f"/safestop/{proposed.json()['proposal_id']}/confirm", user=OPERATOR_A)
        engaged_at = now_utc()
    finally:
        stack.compose("start", "mosquitto")
    try:
        wait_until(
            lambda: stack.rows(
                "SELECT 1 FROM og.stop_event WHERE action = 'ENGAGE' AND scope_ref = %(b)s AND created_at >= %(t)s",
                {"b": BANK, "t": engaged_at.replace(microsecond=0)},
            ),
            timeout_s=90,
            what="the ENGAGE recorded by og-safestop",
        )
        wait_until(
            lambda: (
                all(
                    abs(float(r["p_kw"])) <= 0.05
                    for r in stack.rows(
                        "SELECT s.p_kw FROM og.hub h JOIN og.hub_state s USING (hub_id) "
                        "WHERE h.bank_id = %(b)s AND s.health = 'online'",
                        {"b": BANK},
                    )
                )
                or None
            ),
            timeout_s=120,
            what=f"{BANK}'s hubs to stop once the broker delivered the ENGAGE",
        )
    finally:
        _release(stack)


@pytest.mark.slow
def test_a_broker_outage_over_60_s_restarts_the_guardian_and_safestop(stack: Stack) -> None:
    before = {svc: stack.restart_count(svc) for svc in ("og-guardian", "og-safestop")}

    _broker_outage(stack, 75)
    back = now_utc()

    for svc, count in before.items():
        assert stack.restart_count(svc) > count, f"{svc} did not exit and restart during a 75 s broker outage"
    wait_until(
        lambda: _verdicts_since(stack, back), timeout_s=180, what="guardian verdicts after its restart"
    )
    stack.wait_process_up("safestop", timeout_s=180)
