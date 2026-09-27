"""Q4: today's safety and market decisions on the dev stack.

- D-12 two-person safe-stop release: og-op-a engages (two-step) and requests the release; og-op-a approving its
  own release is 403, og-op-b approving releases it. K8 replay: re-publishing that old RELEASE never lifts a newer
  stop.
- K7 escalation: more than 5% vetoes per tick puts the scope CONSERVATIVE; three ticks raise
  ALR-SAFE-STOP-REQUESTED plus an unconfirmed proposal, never an automatic stop; recovery clears both.

The ERCOT_AS capacity hold needs a live delivery window and lives in `test_as_capacity_hold.py` (slow).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from e2e_stack import REPO_ROOT, Stack, _dev_secret, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

#: The one bank this module safe-stops (the same one the Q2 suite uses); every scenario releases it again.
STOP_BANK = "bank-007"
OPERATOR_A = os.environ.get("OG_E2E_OPERATOR_A", "og-op-a")
OPERATOR_B = os.environ.get("OG_E2E_OPERATOR_B", "og-op-b")


def _engage(stack: Stack, user: str) -> None:
    proposed = stack.post(
        "/safestop", {"scope": "bank", "scope_id": STOP_BANK, "reason": "e2e D-12"}, user=user
    )
    if proposed.status_code == 403:
        pytest.skip(f"{user} is not an operator on this stack ([api.roles.operator])")
    assert proposed.status_code == 202, proposed.text
    confirmed = stack.post(f"/safestop/{proposed.json()['proposal_id']}/confirm", user=user)
    assert confirmed.status_code == 200, confirmed.text


def _release_rows_since(stack: Stack, since) -> list[dict]:
    return stack.rows(
        "SELECT stop_event_id FROM og.stop_event WHERE action = 'RELEASE' AND scope_ref = %(b)s "
        "AND created_at >= %(t)s",
        {"b": STOP_BANK, "t": since},
    )


def _two_person_release(stack: Stack, *, attempts: int = 3) -> None:
    """og-op-a requests, og-op-b approves; retried because the guardian refuses while its clock check (G-20)
    fails."""
    for attempt in range(1, attempts + 1):
        requested = stack.post(f"/safestop/bank/{STOP_BANK}/release", {"reason": "e2e D-12"}, user=OPERATOR_A)
        assert requested.status_code == 202, requested.text
        started = now_utc()
        approved = stack.post(f"/safestop/release/{requested.json()['proposal_id']}/approve", user=OPERATOR_B)
        assert approved.status_code in {200, 202}, approved.text
        try:
            wait_until(
                lambda since=started: _release_rows_since(stack, since),
                timeout_s=30,
                what="the guardian-signed RELEASE",
            )
            return
        except AssertionError:
            if attempt == attempts:
                raise


def test_d12_two_person_release_only_by_a_second_operator(stack: Stack) -> None:
    _engage(stack, OPERATOR_A)

    requested = stack.post(f"/safestop/bank/{STOP_BANK}/release", {"reason": "e2e D-12"}, user=OPERATOR_A)
    assert requested.status_code == 202, requested.text
    self_approved = stack.post(
        f"/safestop/release/{requested.json()['proposal_id']}/approve", user=OPERATOR_A
    )
    assert self_approved.status_code == 403, self_approved.text

    _two_person_release(stack)


# --- K8 replay ------------------------------------------------------------------------------------


def _mosquitto(*args: str, user: str, credential_var: str) -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not available to reach the dev broker")
    compose_file = str(REPO_ROOT / "dev" / "docker-compose.yml")
    cmd = [docker, "compose", "-f", compose_file, "exec", "-T", "mosquitto", *args]
    cmd += ["-h", "localhost", "-u", user, "-P", _dev_secret(credential_var)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603 -- fixed argv
    return done.stdout


def _retained_releases() -> list[tuple[str, str]]:
    """(topic, payload) of every retained RELEASE on the dev broker's stop tree, read as the sim user."""
    out = _mosquitto(
        "mosquitto_sub",
        "-t",
        "og/v1/stop/#",
        "-v",
        "-W",
        "3",
        user="og_sim",
        credential_var="OG_MQTT_SIM_PASSWORD",
    )
    found = []
    for line in out.splitlines():
        topic, _, payload = line.partition(" ")
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("action") == "RELEASE":
            found.append((topic, payload))
    return found


def _bank_output_kw(stack: Stack) -> list[float]:
    return [
        abs(float(row["p_kw"]))
        for row in stack.rows(
            "SELECT s.p_kw FROM og.hub h JOIN og.hub_state s USING (hub_id) "
            "WHERE h.bank_id = %(b)s AND s.health = 'online'",
            {"b": STOP_BANK},
        )
    ]


def test_k8_a_replayed_old_release_never_lifts_a_newer_stop(stack: Stack) -> None:
    _engage(stack, OPERATOR_A)
    _two_person_release(stack)
    old_releases = _retained_releases()
    if not old_releases:
        pytest.skip("no retained RELEASE visible on the dev broker to replay")

    _engage(stack, OPERATOR_A)
    newest = stack.rows(
        "SELECT stop_event_id FROM og.stop_event WHERE action = 'ENGAGE' AND scope_ref = %(b)s "
        "ORDER BY created_at DESC LIMIT 1",
        {"b": STOP_BANK},
    )[0]["stop_event_id"]
    for topic, payload in old_releases:
        for target in (topic, f"og/v1/stop/bank/{STOP_BANK}/{newest}"):
            _mosquitto(
                "mosquitto_pub", "-t", target, "-m", payload,
                user="og_safestop", credential_var="OG_MQTT_SAFESTOP_PASSWORD",
            )  # fmt: skip
    try:
        # A stopped bank ramps to 0 (ramp_bank_s) and stays there; an honoured replay would put its hubs
        # back on their held setpoint instead.
        wait_until(
            lambda: all(kw <= 0.05 for kw in _bank_output_kw(stack)) or None,
            timeout_s=90,
            what=f"{STOP_BANK} to reach 0 kW under the newer stop",
        )
        settled = now_utc()
        wait_until(
            lambda: (now_utc() - settled).total_seconds() >= 10, timeout_s=15, what="10 s of observation"
        )
        assert all(kw <= 0.05 for kw in _bank_output_kw(stack)), "the replayed RELEASE lifted the newer stop"
    finally:
        _two_person_release(stack)


# --- K7 escalation --------------------------------------------------------------------------------


@pytest.mark.skip(
    reason="R3: manual commands no longer produce per-command vetoes; the veto storm needs an engine-path source "
    "(e.g. the unit-cap fixture in r3/test_k4_resolve.py)"
)
def test_k7_sustained_vetoes_request_a_stop_but_never_engage_one(stack: Stack) -> None:
    started = now_utc()
    hub = stack.online_hub(exclude_banks=(STOP_BANK,))
    over_limit = float(hub["p_limit_kw"]) * 3

    def veto_storm() -> None:
        for _ in range(4):  # several vetoed batches per 2 s tick, well over 5% of the tick's batches
            stack.manual_command(hub["hub_id"], over_limit)

    alert = wait_until(
        lambda: (
            veto_storm()
            or stack.rows(
                "SELECT id FROM og.alert WHERE rule = 'ALR-SAFE-STOP-REQUESTED' AND opened_at >= %(t)s",
                {"t": started},
            )
        ),
        timeout_s=60,
        interval_s=0.5,
        what="ALR-SAFE-STOP-REQUESTED after sustained vetoes",
    )
    assert alert
    assert not stack.rows(
        "SELECT 1 FROM og.stop_event WHERE action = 'ENGAGE' AND created_at >= %(t)s "
        "AND initiator_kind <> 'SAFESTOP_AUTHORITY'",
        {"t": started},
    ), "escalation must never engage a stop by itself"
    wait_until(
        lambda: stack.rows(
            "SELECT 1 FROM og.alert WHERE rule = 'ALR-SAFE-STOP-REQUESTED' AND opened_at >= %(t)s "
            "AND cleared_at IS NOT NULL",
            {"t": started},
        ),
        timeout_s=120,
        what="the escalation to clear once vetoes stop",
    )
