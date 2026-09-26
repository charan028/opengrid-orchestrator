"""Q1 part 2: an obligation actually DELIVERING -- the RT allocator serves it first, a new call does not
interrupt it, and losing a serving hub is met by substitution or a traced shortfall, never silently.

Test plan `04-mvp-s-test-plan.md` S3.4-S3.5 (TS-04-07, -11/-13, -12, TS-05-07) and the
guardian's commitment-lock check against a live delivery (TS-06-14). These wait for a real
15-minute delivery window to open, so they are marked `slow` (`pytest -m "not slow"` skips them).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from e2e_stack import INTERVAL, Offer, Stack, now_utc, wait_until

pytestmark = pytest.mark.slow

COMMITTED_KW = 100.0
#: A cycle may round per bank; this is the tolerance on "the committed kW is served in full".
SERVED_TOLERANCE_KW = Decimal("0.5")
SHORTFALL_OR_LOCK_CODES = ("R-SHORTFALL", "R-COMMIT-LOCK-INFEASIBLE", "R-COMMIT-LOCK-OVERRIDE")


@pytest.fixture(scope="module")
def delivering(stack: Stack) -> Iterator[tuple[Offer, dict]]:
    """One ERCOT_ENERGY obligation committed for a single interval that opens within ~15 minutes, returned
    once it is DELIVERING (the window is taken from the earliest lightly-loaded interval at least a minute
    away, so the gate commits it before the window opens)."""
    earliest = now_utc() + timedelta(seconds=90)
    start, end = stack.light_window(1, max_committed_kw=500)
    while start < earliest:
        start, end = stack.light_window(1, max_committed_kw=500, first_offset=2)
    contract_id = stack.create_contract("ERCOT_ENERGY", "T2")
    offer = stack.offer(
        contract_id, window_start=start, window_end=end, requested_kw=COMMITTED_KW, value_per_mwh=180
    )
    committed = stack.wait_decided(offer)
    assert committed["state"] == "COMMITTED", committed
    wait_until(
        lambda: now_utc() >= start + timedelta(seconds=10),
        timeout_s=max((start - now_utc()).total_seconds(), 0) + 180,
        interval_s=2,
        what=f"the delivery window at {start:%H:%M}",
    )
    obligation = stack.wait_state(offer, {"DELIVERING"}, timeout_s=90)
    yield offer, obligation
    assert now_utc() < end, "a delivery scenario outlived the window; the results above may be unreliable"


def _served_per_cycle(stack: Stack, obligation_id, since) -> list[Decimal]:
    """Total non-headroom kW granted to the obligation in each complete engine cycle since `since`."""
    rows = stack.rows(
        """SELECT cycle_id, sum(granted_kw) AS kw FROM og.grant
           WHERE obligation_id = %(o)s AND NOT is_headroom AND created_at >= %(t)s
           GROUP BY cycle_id ORDER BY min(created_at)""",
        {"o": obligation_id, "t": since},
    )
    return [row["kw"] for row in rows][1:-1]  # drop the possibly-partial first and last cycles


def _wait_cycles(stack: Stack, obligation_id, since, count: int) -> list[Decimal]:
    return wait_until(
        lambda: served if len(served := _served_per_cycle(stack, obligation_id, since)) >= count else None,
        timeout_s=count * 2 + 30,
        what=f"{count} engine cycles of grants",
    )


def test_ts_05_07_the_rt_allocator_serves_the_committed_kw_every_cycle(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    _, obligation = delivering

    served = _wait_cycles(stack, obligation["obligation_id"], now_utc(), 5)

    assert all(kw >= Decimal(str(COMMITTED_KW)) - SERVED_TOLERANCE_KW for kw in served), served


def test_ts_04_07_a_new_call_during_delivery_does_not_interrupt_it(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    offer, obligation = delivering
    locked = stack.active_commitments(obligation["obligation_id"])
    rival_window_end = offer.window_start + INTERVAL * 3

    rival_contract = stack.create_contract("ERCOT_ENERGY", "T2")
    admitted = now_utc()
    rival = stack.offer(
        rival_contract,
        window_start=offer.window_start,
        window_end=rival_window_end,
        requested_kw=5000,
        value_per_mwh=900,
    )
    stack.wait_decided(rival)

    assert stack.active_commitments(obligation["obligation_id"]) == locked
    served = _wait_cycles(stack, obligation["obligation_id"], admitted, 5)
    assert all(kw >= Decimal(str(COMMITTED_KW)) - SERVED_TOLERANCE_KW for kw in served), served
    assert stack.obligation(offer)["state"] == "DELIVERING"


def test_ts_06_14_g19_a_manual_command_cutting_a_committed_hub_is_vetoed(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    _, obligation = delivering
    serving = stack.rows(
        """SELECT h.hub_id FROM og.hub h JOIN og.hub_state s USING (hub_id)
           WHERE h.bank_id = (SELECT bank_id FROM og.grant WHERE obligation_id = %(o)s AND NOT is_headroom
                              ORDER BY created_at DESC LIMIT 1)
             AND s.health = 'online' AND s.p_kw <> 0
           ORDER BY abs(s.p_kw) DESC LIMIT 1""",
        {"o": obligation["obligation_id"]},
    )
    if not serving:
        pytest.skip("no hub in the serving bank is currently producing")

    resp = stack.manual_command(serving[0]["hub_id"], 0.0)

    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert "G-19" in body["vetoed_rule_ids"], body


def _serving_hub(stack: Stack, obligation_id) -> dict:
    bank = stack.rows(
        """SELECT bank_id FROM og.grant WHERE obligation_id = %(o)s AND NOT is_headroom
           ORDER BY created_at DESC LIMIT 1""",
        {"o": obligation_id},
    )[0]["bank_id"]
    return stack.rows(
        """SELECT h.hub_id, h.bank_id, b.zone FROM og.hub h JOIN og.hub_state s USING (hub_id)
           JOIN og.bank b ON b.bank_id = h.bank_id
           WHERE h.bank_id = %(b)s AND s.health = 'online' ORDER BY abs(s.p_kw) DESC LIMIT 1""",
        {"b": bank},
    )[0]


def test_ts_04_11_13_losing_a_serving_hub_is_met_by_substitution_within_the_same_obligation(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    offer, obligation = delivering
    hub = _serving_hub(stack, obligation["obligation_id"])
    locked = stack.active_commitments(obligation["obligation_id"])
    tripped = now_utc()

    anomaly = stack.inject("inverter_trip", hub["hub_id"], duration_s=60)
    try:
        served = _wait_cycles(stack, obligation["obligation_id"], tripped + timedelta(seconds=6), 5)
    finally:
        stack.clear_anomaly(anomaly)

    codes = stack.trace_reason_codes(tripped)
    maintained = all(kw >= Decimal(str(COMMITTED_KW)) - SERVED_TOLERANCE_KW for kw in served)
    if maintained:
        assert stack.obligation(offer)["obligation_id"] == obligation["obligation_id"]
    else:
        assert any(code.startswith(SHORTFALL_OR_LOCK_CODES) for code in codes), (
            f"served dropped to {served} with no traced shortfall/lock reason: {sorted(set(codes))}"
        )
    assert stack.active_commitments(obligation["obligation_id"]) == locked, (
        "a hub fault changed the commitment"
    )


def test_ts_04_12_losing_a_whole_zone_is_never_silently_absorbed(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    offer, obligation = delivering
    hub = _serving_hub(stack, obligation["obligation_id"])
    lost = now_utc()

    anomaly = stack.inject("zone_mass_disconnect", hub["zone"], duration_s=90)
    try:
        wait_until(
            lambda: stack.get(f"/fleet/hubs/{hub['hub_id']}").json().get("health") in {"stale", "offline"},
            timeout_s=60,
            what=f"zone {hub['zone']} to drop",
        )
        observed_from = now_utc()
        wait_until(
            lambda: now_utc() >= observed_from + timedelta(seconds=12), timeout_s=20, what="12 s of cycles"
        )
        served = _served_per_cycle(stack, obligation["obligation_id"], observed_from)
    finally:
        stack.clear_anomaly(anomaly)

    if not served or not all(kw >= Decimal(str(COMMITTED_KW)) - SERVED_TOLERANCE_KW for kw in served):
        codes = stack.trace_reason_codes(lost)
        state = stack.obligation(offer)
        assert state["at_risk"] or any(code.startswith(SHORTFALL_OR_LOCK_CODES) for code in codes), (
            f"served {served} with no AT_RISK flag or traced shortfall: {sorted(set(codes))}"
        )
    assert not stack.rows(
        "SELECT 1 FROM og.invariant_violation WHERE detected_at >= %(t)s AND check_name LIKE 'K13%%'",
        {"t": lost},
    )
