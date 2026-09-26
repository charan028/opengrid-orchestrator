"""Q4: an ERCOT_AS award is a capacity hold (test plan TS-05-16).

A committed Non-Spin award reserves its kW and holds energy for a full deployment above the reserve floor, but
discharges 0 kW until ERCOT deploys it; an operator-recorded deployment (`POST /og/api/dispatch/as-deployments`)
starts delivery and ending it returns the hold to 0 kW. Needs a live window, so `slow`.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.slow

#: The Non-Spin minimum: the smallest hold the energy-hold feasibility check has to fit.
AWARD_KW = 100.0
NONSPIN_MINUTES = 240


def _discharged_per_cycle(stack: Stack, obligation_id, since) -> list[Decimal]:
    rows = stack.rows(
        """SELECT cycle_id, sum(granted_kw) AS kw FROM og.grant
           WHERE obligation_id = %(o)s AND NOT is_headroom AND created_at >= %(t)s
           GROUP BY cycle_id ORDER BY min(created_at)""",
        {"o": obligation_id, "t": since},
    )
    return [row["kw"] for row in rows]


def _observe(stack: Stack, obligation_id, seconds: float) -> list[Decimal]:
    since = now_utc()
    wait_until(
        lambda: (now_utc() - since).total_seconds() >= seconds, timeout_s=seconds + 10, what="observation"
    )
    return _discharged_per_cycle(stack, obligation_id, since)


def test_an_as_award_holds_capacity_at_0_kw_until_ercot_deploys_it(stack: Stack) -> None:
    contract_id = stack.create_contract("ERCOT_AS", "T2", variant="NONSPIN")
    stack.add_product_rule(
        contract_id,
        product_code="NSPIN",
        min_qty_kw=100,
        increment_kw=100,
        block=False,
        duration_minutes=NONSPIN_MINUTES,
        variable_kind="SEMI_CONTINUOUS",
    )
    earliest = now_utc() + timedelta(seconds=90)
    start, end = stack.light_window(1, max_committed_kw=1000)
    if start < earliest:
        start, end = stack.light_window(1, max_committed_kw=1000, first_offset=2)
    offer = stack.offer(
        contract_id, window_start=start, window_end=end, requested_kw=AWARD_KW, value_per_mwh=200
    )
    award = stack.wait_decided(offer)
    # The intake prices hourly Non-Spin offers for every active ERCOT_AS contract; end this one now so only
    # the award under test holds fleet capacity and energy.
    stack.end_contract(contract_id)
    assert award["state"] == "COMMITTED", award
    reserved = stack.rows(
        "SELECT sum(amount) AS kw FROM og.reservation WHERE obligation_id = %(o)s AND kind = 'POWER_KW' "
        "AND released_at IS NULL AND interval_start = %(s)s",
        {"o": award["obligation_id"], "s": start},
    )[0]["kw"]
    assert reserved is not None and reserved >= Decimal(str(AWARD_KW)), (
        f"the award's kW is not reserved: {reserved}"
    )

    wait_until(
        lambda: now_utc() >= start + timedelta(seconds=10),
        timeout_s=max((start - now_utc()).total_seconds(), 0) + 180,
        interval_s=2,
        what=f"the award's window at {start:%H:%M}",
    )
    held = _observe(stack, award["obligation_id"], 12)
    assert all(kw <= Decimal("0.5") for kw in held), (
        f"a held AS award must not discharge before deployment: {held}"
    )

    deployed = stack.post(
        "/dispatch/as-deployments",
        {"obligation_id": str(award["obligation_id"]), "duration_minutes": 5, "reason": "e2e deployment"},
    )
    assert deployed.status_code == 201, deployed.text
    deployment_id = deployed.json()["deployment_id"]
    try:
        wait_until(
            lambda: any(
                kw > Decimal("0.5")
                for kw in _discharged_per_cycle(
                    stack, award["obligation_id"], now_utc() - timedelta(seconds=6)
                )
            ),
            timeout_s=45,
            what="discharge once ERCOT deploys the award",
        )
    finally:
        stack.http.delete(
            f"{stack.api_base}/dispatch/as-deployments/{deployment_id}", headers=stack.headers()
        )

    wait_until(
        lambda: (
            (lambda cycles: cycles and all(kw <= Decimal("0.5") for kw in cycles[-3:]))(
                _discharged_per_cycle(stack, award["obligation_id"], now_utc() - timedelta(seconds=10))
            )
            or not _discharged_per_cycle(stack, award["obligation_id"], now_utc() - timedelta(seconds=10))
        ),
        timeout_s=45,
        what="the award to return to a 0 kW hold after the deployment ends",
    )
