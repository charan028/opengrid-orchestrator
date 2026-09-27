"""R3: MQTT broker recovery on the dev stack (dev broker only, never the shared one).

- A ~20 s broker outage: og-engine, og-guardian and og-safestop reconnect (their reconnect counters increase) and
  telemetry and guardian verdicts resume.
- A safe stop engaged while the broker is down is delivered to the hubs once it is back.
- A broker outage over 60 s: og-guardian and og-safestop exit and are restarted (systemd in production, the
  container restart policy here). `slow`.

Metrics endpoints are loopback-only inside each container, so they are scraped with `docker compose exec`. Their
ports come from `[metrics]` of the config the stack's og-* services load (`OG_CONFIG` in dev/docker-compose.yml,
dev/config/docker.toml; set `OG_E2E_CONFIG` for a stack that loads another file), else from each service's own
default: og-guardian 9103, og-safestop 9106, and no endpoint for og-engine without `engine_port`.
`OG_E2E_ENGINE_METRICS_PORT`, `OG_E2E_GUARDIAN_METRICS_PORT` and `OG_E2E_SAFESTOP_METRICS_PORT` override a port.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from e2e_stack import REPO_ROOT, Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

BANK = "bank-007"
OPERATOR_A = os.environ.get("OG_E2E_OPERATOR_A", "og-op-a")
OPERATOR_B = os.environ.get("OG_E2E_OPERATOR_B", "og-op-b")
#: The config file the dev stack's og-* services load (`OG_CONFIG` in dev/docker-compose.yml; the repo is mounted).
STACK_CONFIG = Path(os.environ.get("OG_E2E_CONFIG") or REPO_ROOT / "dev" / "config" / "docker.toml")


def _reconnect_metrics() -> dict[str, tuple[int | None, str]]:
    """service -> (its /metrics port, its reconnect counter). The port is the OG_E2E_<SVC>_METRICS_PORT override,
    else `[metrics]` of STACK_CONFIG, else the service's own default (guardian/main.py 9103, safestop/main.py
    9106). None: og-engine serves no /metrics without `[metrics].engine_port` (engine/metrics.py)."""
    section: dict[str, Any] = {}
    if STACK_CONFIG.is_file():
        with STACK_CONFIG.open("rb") as fh:
            section = tomllib.load(fh).get("metrics", {})

    def port(env: str, key: str, default: int | None) -> int | None:
        value = os.environ.get(env) or section.get(key, default)
        return None if value is None else int(value)

    return {
        "og-engine": (
            port("OG_E2E_ENGINE_METRICS_PORT", "engine_port", None),
            "og_engine_mqtt_reconnects_total",
        ),
        "og-guardian": (
            port("OG_E2E_GUARDIAN_METRICS_PORT", "guardian_port", 9103),
            "og_mqtt_reconnects_total",
        ),
        "og-safestop": (
            port("OG_E2E_SAFESTOP_METRICS_PORT", "safestop_port", 9106),
            "og_mqtt_reconnects_total",
        ),
    }


RECONNECT_METRICS = _reconnect_metrics()


def _reconnects(stack: Stack, svc: str) -> float | None:
    port, name = RECONNECT_METRICS[svc]
    return None if port is None else stack.metric(svc, port, name)


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
    before = {svc: _reconnects(stack, svc) for svc in RECONNECT_METRICS}
    missing = [svc for svc, value in before.items() if value is None]
    assert not missing, (
        f"no reconnect counter exposed by {missing} on {RECONNECT_METRICS} (check [metrics] in {STACK_CONFIG})"
    )

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
    for svc, (_, name) in RECONNECT_METRICS.items():
        after = wait_until(
            lambda svc=svc: v if (v := _reconnects(stack, svc)) and v > before[svc] else None,
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
