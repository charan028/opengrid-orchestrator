"""R2: two-person safe-stop release (D-12), test plan TS-06-24.

og-op-a engages a bank stop (two-step) and requests its release; og-op-a approving its own release is refused;
og-op-b approves, the guardian signs the RELEASE, and the command path into that bank resumes (a command there
is no longer refused SAFE_STOP). The operator identities come from the stack's own config; no credential is
stored here (the harness sends the identity plus the proxy secret read from the environment).
"""

from __future__ import annotations

import os

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

BANK = "bank-007"
OPERATOR_A = os.environ.get("OG_E2E_OPERATOR_A", "og-op-a")
OPERATOR_B = os.environ.get("OG_E2E_OPERATOR_B", "og-op-b")


def _verdict(resp) -> dict:
    body = resp.json()
    return body["detail"] if isinstance(body.get("detail"), dict) else body


def _command_into_bank(stack: Stack):
    hub = stack.rows(
        "SELECT h.hub_id, s.p_kw FROM og.hub h JOIN og.hub_state s USING (hub_id) "
        "WHERE h.bank_id = %(b)s AND s.health = 'online' LIMIT 1",
        {"b": BANK},
    )[0]
    return stack.manual_command(hub["hub_id"], float(hub["p_kw"]), user=OPERATOR_A)


def test_ts_06_24_a_second_operator_releases_the_stop_and_the_command_path_resumes(stack: Stack) -> None:
    proposed = stack.post(
        "/safestop", {"scope": "bank", "scope_id": BANK, "reason": "e2e D-12"}, user=OPERATOR_A
    )
    if proposed.status_code == 403:
        pytest.skip(f"{OPERATOR_A} is not an operator on this stack ([api.roles.operator])")
    assert proposed.status_code == 202, proposed.text
    engaged = stack.post(f"/safestop/{proposed.json()['proposal_id']}/confirm", user=OPERATOR_A)
    assert engaged.status_code == 200, engaged.text
    blocked = _command_into_bank(stack)
    assert "SAFE_STOP" in (_verdict(blocked).get("vetoed_rule_ids") or []), blocked.text

    released = False
    for _attempt in range(3):  # the guardian correctly refuses while its own clock check (G-20) fails
        requested = stack.post(f"/safestop/bank/{BANK}/release", {"reason": "e2e D-12"}, user=OPERATOR_A)
        assert requested.status_code == 202, requested.text
        proposal = requested.json()["proposal_id"]
        self_approved = stack.post(f"/safestop/release/{proposal}/approve", user=OPERATOR_A)
        assert self_approved.status_code == 403, self_approved.text

        since = now_utc()
        approved = stack.post(f"/safestop/release/{proposal}/approve", user=OPERATOR_B)
        assert approved.status_code in {200, 202}, approved.text
        try:
            wait_until(
                lambda since=since: stack.rows(
                    "SELECT 1 FROM og.stop_event WHERE action = 'RELEASE' AND scope_ref = %(b)s "
                    "AND created_at >= %(t)s",
                    {"b": BANK, "t": since},
                ),
                timeout_s=30,
                what="the guardian-signed RELEASE",
            )
            released = True
            break
        except AssertionError:
            continue
    assert released, "og-op-b's approval never produced a guardian-signed RELEASE"

    resumed = _command_into_bank(stack)
    assert resumed.status_code in {200, 409}, resumed.text
    assert "SAFE_STOP" not in (_verdict(resumed).get("vetoed_rule_ids") or []), "the bank is still stopped"
