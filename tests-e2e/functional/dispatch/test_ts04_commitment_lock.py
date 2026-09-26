"""Q1 part 1: commitment lock (K13) and multi-customer selection, against the running dev stack.

Test plan `04-mvp-s-test-plan.md` S3.4 (TS-04-05, -06, -15, -16). Each scenario uses fresh contracts so its
admission gate runs immediately (the engine gates each contract at most once per 15-minute slot), and
windows with no existing commitment (`Stack.free_window`), at least three intervals out, so an earlier run's
still-locked obligations never take its capacity and nothing is delivering while the test observes the gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from e2e_stack import Stack, now_utc

pytestmark = pytest.mark.usefixtures("stack")


def _new_invariant_violations(stack: Stack, since, *checks: str) -> list[dict]:
    return stack.rows(
        "SELECT check_name, scope, detail FROM og.invariant_violation "
        "WHERE detected_at >= %(t)s AND check_name = ANY(%(c)s)",
        {"t": since, "c": list(checks)},
    )


def _commit(stack: Stack, service_type: str, tier: str, *, start, end, kw: float, value: float):
    contract_id = stack.create_contract(service_type, tier)
    offer = stack.offer(
        contract_id,
        window_start=start,
        window_end=end,
        requested_kw=kw,
        value_per_mwh=value,
    )
    obligation = stack.wait_decided(offer)
    return offer, obligation


def test_ts_04_05_same_tier_rising_value_never_displaces_a_committed_obligation(
    stack: Stack,
) -> None:
    started = now_utc()
    start, end = stack.free_window(2)
    _, first = _commit(stack, "ERCOT_ENERGY", "T2", start=start, end=end, kw=80, value=100)
    stack.require_committed(first)
    locked = stack.active_commitments(first["obligation_id"])
    assert locked and all(kw == Decimal("80.000") for kw in locked.values()), locked

    for multiple in (1.5, 2.0, 3.0):
        rival_offer, rival = _commit(
            stack,
            "ERCOT_ENERGY",
            "T2",
            start=start,
            end=end,
            kw=5000,
            value=100 * multiple,
        )

        assert stack.active_commitments(first["obligation_id"]) == locked, f"displaced at {multiple}x"
        assert rival["committed_qty_kw"] <= rival_offer.requested_kw
        rival_kw = stack.active_commitments(rival["obligation_id"])
        assert all(kw <= rival_offer.requested_kw for kw in rival_kw.values())

    history = stack.commitment_history(first["obligation_id"])
    assert all(row["supersedes"] is None for row in history), "the first obligation was re-committed"
    assert not any(row["reason_code"].startswith("R-COMMIT-LOCK-OVERRIDE") for row in history)
    assert not _new_invariant_violations(stack, started, "K13_LOCK_VIOLATION", "K2_DOUBLE_SOLD")


def test_ts_04_06_higher_value_lower_tier_call_gets_only_headroom_over_a_firm_commitment(
    stack: Stack,
) -> None:
    started = now_utc()
    start, end = stack.free_window(2)
    _, firm = _commit(stack, "DIST_DEFERRAL", "T1", start=start, end=end, kw=120, value=120)
    stack.require_committed(firm)
    locked = stack.active_commitments(firm["obligation_id"])

    market_offer, market = _commit(stack, "ERCOT_ENERGY", "T3", start=start, end=end, kw=5000, value=5000)

    assert stack.active_commitments(firm["obligation_id"]) == locked
    assert all(row["supersedes"] is None for row in stack.commitment_history(firm["obligation_id"]))
    assert market["committed_qty_kw"] <= market_offer.requested_kw
    assert not _new_invariant_violations(stack, started, "K13_LOCK_VIOLATION", "K2_DOUBLE_SOLD")


def test_ts_04_15_partial_take_respects_min_qty_and_increment(stack: Stack) -> None:
    contract_id = stack.create_contract("ERCOT_AS", "T2", variant="NONSPIN")
    stack.add_product_rule(
        contract_id,
        product_code="NONSPIN",
        min_qty_kw=100,
        increment_kw=100,
        block=False,
        duration_minutes=60,
        variable_kind="SEMI_CONTINUOUS",
    )
    start, end = stack.free_window(4)
    offer = stack.offer(
        contract_id,
        window_start=start,
        window_end=end,
        requested_kw=250,
        value_per_mwh=200,
    )

    obligation = stack.wait_decided(offer)

    stack.require_committed(obligation)
    taken = stack.active_commitments(obligation["obligation_id"])
    for kw in taken.values():
        assert kw >= Decimal(100), taken
        assert kw % Decimal(100) == 0, f"{kw} kW is not a whole number of 100 kW increments"
        assert kw <= Decimal(250)


def test_ts_04_16_all_or_nothing_block_is_never_partially_taken(stack: Stack) -> None:
    def block_offer(kw: float):
        contract_id = stack.create_contract("PARTNER_CAPACITY", "T3", variant="EVENT")
        stack.add_product_rule(
            contract_id,
            product_code="EVENT_BLOCK",
            min_qty_kw=kw,
            increment_kw=0,
            block=True,
            duration_minutes=30,
            variable_kind="BINARY",
        )
        start, end = stack.free_window(2)
        offer = stack.offer(
            contract_id,
            window_start=start,
            window_end=end,
            requested_kw=kw,
            value_per_mwh=300,
        )
        return offer, stack.wait_decided(offer)

    _, too_big = block_offer(50_000)
    assert too_big["state"] != "COMMITTED", too_big
    assert not stack.active_commitments(too_big["obligation_id"])

    fits_offer, fits = block_offer(50)
    stack.require_committed(fits)
    taken = stack.active_commitments(fits["obligation_id"])
    assert taken and all(kw == fits_offer.requested_kw for kw in taken.values()), taken
