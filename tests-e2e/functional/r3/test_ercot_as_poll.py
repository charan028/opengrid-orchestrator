"""D-35 (r3.4.1): ERCOT AS deployment instructions, end to end. The simulator control plane injects the
`ercot_as_*` scenarios; og-feeds polls ogsim.market's MMS endpoint, applies each instruction through the
operator route's core and answers ERCOT. Asserted on what the running processes wrote: the `ercot_as_poll`
trace stream, `og.as_deployment` (source ERCOT) and `og.alert`.

The stack must run og-feeds with `[feeds.ercot_as_poll]` enabled against the stack's market sim, mapping
`OG_ESR_1:ECRS` to an ERCOT_AS contract (default: the seeded demo ECRS contract). Set OG_E2E_ERCOT_AS_POLL=1
to run; otherwise the module skips.

The accepted ECRS deployment is also checked as DELIVERED power (D-38, `delivery_check`) before it is recalled:
measured from telemetry with `opengrid.core.delivery`, it must reach 95% of the deployed kW within the policy's
600 s ramp time and hold it (the sim's instruction carries the same 10-minute ECRS ramp, which the call does not
use). That check watches the running deployment for up to about 12 minutes; the deployment ends with the sim's
instruction, 30 minutes by default.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from delivery_check import assert_delivered, obligation_buckets, wait_delivered
from e2e_stack import Stack, wait_until

pytestmark = [
    pytest.mark.usefixtures("stack"),
    pytest.mark.skipif(
        os.environ.get("OG_E2E_ERCOT_AS_POLL") != "1",
        reason="needs og-feeds with [feeds.ercot_as_poll] enabled (set OG_E2E_ERCOT_AS_POLL=1)",
    ),
]

RESOURCE = os.environ.get("OG_E2E_ERCOT_AS_RESOURCE", "OG_ESR_1")
#: Poll cadence 5 s plus up to 4 s of simulated delivery latency, with margin.
DECISION_TIMEOUT_S = 45.0


def _decisions(stack: Stack, since_seq: int) -> list[dict[str, Any]]:
    return stack.rows(
        "SELECT seq, event_class, payload FROM og.trace WHERE stream_id = 'ercot_as_poll' AND seq > %(s)s "
        "ORDER BY seq",
        {"s": since_seq},
    )


def _head(stack: Stack) -> int:
    rows = stack.rows("SELECT coalesce(max(seq), -1) AS seq FROM og.trace WHERE stream_id = 'ercot_as_poll'")
    return int(rows[0]["seq"])


def _decided(stack: Stack, since: int, event_class: str) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        return next((r for r in _decisions(stack, since) if r["event_class"] == event_class), None)

    return wait_until(probe, timeout_s=DECISION_TIMEOUT_S, what=f"an {event_class} decision")


@pytest.mark.parametrize(
    ("anomaly", "target", "params", "status"),
    [
        ("ercot_as_malformed", RESOURCE, {}, 422),
        ("ercot_as_unknown_award", "OG_ESR_UNKNOWN", {"mw": 0.5}, 404),
        ("ercot_as_exceed_award", RESOURCE, {"factor": 1000.0}, 409),
    ],
)
def test_a_refused_instruction_is_traced_alerted_and_deploys_nothing(
    stack: Stack, anomaly: str, target: str, params: dict[str, Any], status: int
) -> None:
    since = _head(stack)
    deployments_before = stack.rows("SELECT count(*) AS n FROM og.as_deployment WHERE source = 'ERCOT'")[0][
        "n"
    ]

    stack.inject(anomaly, target, **params)
    refused = _decided(stack, since, "AS_INSTRUCTION_REFUSED")

    assert refused["payload"]["origin"] == "ERCOT_POLL"
    assert refused["payload"]["http_status"] == status
    key = f"ALR-ERCOT-AS-REFUSED:{refused['payload']['instruction_id']}"
    assert stack.rows(
        "SELECT 1 FROM og.alert WHERE rule = 'ALR-ERCOT-AS-REFUSED' AND detail ->> 'condition_key' = %(k)s",
        {"k": key},
    )
    after = stack.rows("SELECT count(*) AS n FROM og.as_deployment WHERE source = 'ERCOT'")[0]["n"]
    assert after == deployments_before


def test_an_ecrs_deployment_its_duplicate_and_its_recall(stack: Stack) -> None:
    live = stack.rows(
        """SELECT o.obligation_id FROM og.obligation o
           WHERE o.service_type = 'ERCOT_AS' AND o.state IN ('COMMITTED', 'DELIVERING')
             AND o.window_start <= now() AND o.window_end > now() + interval '30 minutes'
             AND NOT EXISTS (SELECT 1 FROM og.as_deployment d WHERE d.obligation_id = o.obligation_id
                               AND d.cancelled_at IS NULL AND d.end_at > now())"""
    )
    if not live:
        pytest.skip("no committed, undeployed ERCOT_AS award covering the next 30 minutes on this stack")
    since = _head(stack)

    stack.inject("ercot_as_duplicate", RESOURCE, service="ECRS", mw=0.1)
    accepted = _decided(stack, since, "AS_INSTRUCTION_ACCEPTED")
    instruction_id = accepted["payload"]["instruction_id"]
    duplicate = _decided(stack, since, "AS_INSTRUCTION_DUPLICATE")
    assert duplicate["payload"]["instruction_id"] == instruction_id
    rows = stack.rows(
        "SELECT deployment_id FROM og.as_deployment WHERE source = 'ERCOT' AND obligation_id = %(o)s "
        "AND cancelled_at IS NULL AND end_at > now()",
        {"o": accepted["payload"]["obligation_id"]},
    )
    assert len(rows) == 1  # the duplicate never deployed twice

    # Accepted is not delivered (D-38): the deployment's measured discharge must reach the deployed kW (its signed
    # `requested_kw`; none = the award's full committed kW) and hold it, judged by core.delivery's own policy.
    deployment = stack.rows(
        "SELECT d.start_at, d.end_at, d.requested_kw, o.committed_qty_kw FROM og.as_deployment d "
        "JOIN og.obligation o USING (obligation_id) WHERE d.deployment_id = %(d)s",
        {"d": rows[0]["deployment_id"]},
    )[0]
    called_kw = (
        -float(deployment["requested_kw"])
        if deployment["requested_kw"] is not None
        else float(deployment["committed_qty_kw"])
    )
    delivered = wait_delivered(
        lambda until: obligation_buckets(
            stack,
            UUID(accepted["payload"]["obligation_id"]),
            committed_discharge_kw=called_kw,
            call_start=deployment["start_at"],
            until=until,
        ),
        call_start=deployment["start_at"],
        call_end=deployment["end_at"],
        what="the polled ECRS deployment",
    )
    assert_delivered(delivered, what="the polled ECRS deployment")

    stack.inject("ercot_as_recall", RESOURCE, service="ECRS")
    recalled = _decided(stack, since, "AS_RECALL_APPLIED")
    assert recalled["payload"]["recalls"] == instruction_id
    assert not stack.rows(
        "SELECT 1 FROM og.as_deployment WHERE deployment_id = %(d)s AND cancelled_at IS NULL AND end_at > now()",
        {"d": rows[0]["deployment_id"]},
    )
