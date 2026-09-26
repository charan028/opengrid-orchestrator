"""Q4: today's delivery-time decisions against a live delivery on the dev stack.

- D-17 best-effort SHORTFALL: an L2 BLOCK on one bank serving a committed obligation drives it to SHORTFALL; while
  blocked it keeps receiving the feasible remainder (never 0) and is flagged AT_RISK; lifting the block restores the
  full commitment within 2 cycles and clears AT_RISK.
- D-18 need-basis commitment: a reduced grant carrying R-GRANT-CLOSED-LOOP is corroborated by G-19 only for a
  MEASURED_FEEDBACK service profile and vetoed for a fixed one.

Both wait for a real 15-minute delivery window, so they are `slow`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from e2e_stack import Offer, Stack, now_utc, wait_until

pytestmark = pytest.mark.slow

#: Big enough that no single bank can carry it, so blocking one bank leaves a real remainder elsewhere.
COMMITTED_KW = 400.0
CYCLE_S = 2.0
TOLERANCE_KW = Decimal("0.5")


@pytest.fixture(scope="module")
def delivering(stack: Stack) -> Iterator[tuple[Offer, dict]]:
    earliest = now_utc() + timedelta(seconds=90)
    start, end = stack.light_window(1, max_committed_kw=1000)
    if start < earliest:
        start, end = stack.light_window(1, max_committed_kw=1000, first_offset=2)
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


def _cycles(stack: Stack, obligation_id, since) -> list[dict]:
    """Per complete engine cycle since `since`: total served kW and kW per bank."""
    rows = stack.rows(
        """SELECT cycle_id, bank_id, sum(granted_kw) AS kw, min(created_at) AS at FROM og.grant
           WHERE obligation_id = %(o)s AND NOT is_headroom AND created_at >= %(t)s
           GROUP BY cycle_id, bank_id ORDER BY min(created_at)""",
        {"o": obligation_id, "t": since},
    )
    cycles: dict[str, dict] = {}
    for row in rows:
        cycle = cycles.setdefault(row["cycle_id"], {"at": row["at"], "total": Decimal(0), "banks": {}})
        cycle["total"] += row["kw"]
        cycle["banks"][row["bank_id"]] = row["kw"]
    return list(cycles.values())[1:-1]  # drop possibly-partial first and last cycles


def test_d17_an_l2_block_gives_best_effort_shortfall_and_the_lift_restores_the_commitment(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    offer, obligation = delivering
    ob_id = obligation["obligation_id"]
    committed = Decimal(str(COMMITTED_KW))
    before = wait_until(
        lambda: c if len(c := _cycles(stack, ob_id, now_utc() - timedelta(seconds=12))) >= 3 else None,
        timeout_s=30,
        what="grants before the block",
    )
    shares = before[-1]["banks"]
    assert len(shares) >= 2, f"the obligation must be served by at least two banks, got {shares}"
    blocked_bank = max(shares, key=lambda bank: shares[bank])

    anomaly = stack.inject("utility_instruction", blocked_bank, duration_s=600, mode="block")
    blocked_at = now_utc()
    try:
        state = wait_until(
            lambda: ob if (ob := stack.obligation(offer))["state"] == "SHORTFALL" else None,
            timeout_s=150,
            what="SHORTFALL after the sustained L2 BLOCK",
        )
        assert state["at_risk"], "a best-effort SHORTFALL must be flagged AT_RISK"

        during = wait_until(
            lambda: c if len(c := _cycles(stack, ob_id, now_utc() - timedelta(seconds=12))) >= 3 else None,
            timeout_s=30,
            what="grants while blocked",
        )
        for cycle in during:
            assert cycle["total"] > TOLERANCE_KW, f"best effort means never 0 while blocked: {cycle}"
            assert cycle["total"] < committed - TOLERANCE_KW, (
                f"the blocked bank cannot be delivering: {cycle}"
            )
            assert cycle["banks"].get(blocked_bank, Decimal(0)) <= TOLERANCE_KW, cycle
        assert (now_utc() - blocked_at).total_seconds() > 0
    finally:
        stack.clear_anomaly(anomaly)
    lifted_at = now_utc()

    after = wait_until(
        lambda: c if len(c := _cycles(stack, ob_id, lifted_at)) >= 4 else None,
        timeout_s=40,
        what="grants after the lift",
    )
    assert any(cycle["total"] >= committed - TOLERANCE_KW for cycle in after[:2]), (
        f"the full commitment did not return within 2 cycles of the lift: {[c['total'] for c in after]}"
    )
    wait_until(
        lambda: not stack.obligation(offer)["at_risk"],
        timeout_s=60,
        what="AT_RISK to clear after the lift",
    )


# --- D-18: G-19 need basis, judged by the real og-guardian on a batch proposed as the engine would ------


def _insert_profile(stack: Stack, contract_id: UUID, setpoint_source: str) -> None:
    envelope = stack.rows(
        "INSERT INTO og.pq_envelope (customer_id, phase_config) VALUES (%(c)s, 'SPLIT_PHASE') RETURNING pq_envelope_id",
        {"c": uuid4()},
    )
    version = stack.rows(
        "SELECT coalesce(max(version), 0) + 1 AS v FROM og.service_profile WHERE contract_id = %(c)s",
        {"c": contract_id},
    )[0]["v"]
    stack.execute(
        """INSERT INTO og.service_profile (contract_id, version, control_primitive, target_quantity, target_scope,
               setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, accuracy_tolerance, deadband,
               priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour)
           VALUES (%(c)s, %(v)s, 'CLOSED_LOOP_REGULATION', 'KW', 'BANK', %(src)s, %(fb)s, 2, 100, 0.05, 0.01,
                   'T2', 'METERED', 'KWH', %(env)s, 'HOLD')""",
        {
            "c": contract_id,
            "v": version,
            "src": setpoint_source,
            "fb": "e2e-site-meter" if setpoint_source == "MEASURED_FEEDBACK" else None,
            "env": envelope[0]["pq_envelope_id"],
        },
    )


def _propose_closed_loop(stack: Stack, obligation_id: UUID, bank_id: str, reduced_kw: float) -> UUID:
    """Hand og-guardian a batch as the engine does (K10 pre-image in og.trace via the trace library, then the
    og.command_batch header), carrying one reduced R-GRANT-CLOSED-LOOP item for the obligation. Its `seq` is
    deliberately stale, so G-13 always refuses it: nothing is ever signed or accepted (the engine's own lease
    sequence for the bank is untouched), while the guardian still evaluates and reports every other check."""
    from opengrid.core.crypto import sha256_hex_of_json
    from opengrid.trace import TraceStore
    from opengrid.trace.pg_backend import PgTraceBackend
    from psycopg_pool import AsyncConnectionPool

    hub = stack.rows(
        "SELECT h.hub_id FROM og.hub h JOIN og.hub_state s USING (hub_id) "
        "WHERE h.bank_id = %(b)s AND s.health = 'online' LIMIT 1",
        {"b": bank_id},
    )[0]["hub_id"]
    epoch = stack.rows(
        "SELECT coalesce(max(epoch), 0) AS e FROM og.lease_state WHERE bank_id = %(b)s", {"b": bank_id}
    )[0]["e"]
    batch_id = uuid4()
    now = datetime.now(UTC)
    item = {
        "hub_id": hub,
        "p_kw_setpoint": reduced_kw,
        "reason_code": "R-GRANT-CLOSED-LOOP",
        "obligation_id": str(obligation_id),
        "obligation_granted_kw": str(reduced_kw),
    }
    payload = {
        "command_batch_id": str(batch_id),
        "bank_id": bank_id,
        "cycle_id": f"e2e-need-basis-{batch_id.hex[:8]}",
        "epoch": int(epoch),
        "seq": 0,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "ledger_version": int(
            stack.rows("SELECT coalesce(max(ledger_version), 0) AS v FROM og.reservation")[0]["v"]
        ),
        "items": [item],
        "is_firm_event": False,
    }

    async def write() -> UUID:
        async with AsyncConnectionPool(stack.dsn, min_size=1, max_size=1, open=False) as pool:
            ref = await TraceStore(PgTraceBackend(pool)).append(
                stream_id="e2e-need-basis",
                decision_type="RT_ALLOCATION",
                event_class="RT_ALLOCATION",
                payload=payload,
                reason_codes=["R-GRANT-CLOSED-LOOP"],
            )
            return ref.trace_id

    trace_id = asyncio.run(write(), loop_factory=asyncio.SelectorEventLoop)
    stack.execute(
        """INSERT INTO og.command_batch (command_batch_id, cycle_id, ledger_version, submission_id, command_count,
               merkle_root, trace_pre_image_id)
           VALUES (%(id)s, %(cycle)s, %(lv)s, %(sub)s, 1, %(root)s, %(pre)s)""",
        {
            "id": batch_id,
            "cycle": payload["cycle_id"],
            "lv": payload["ledger_version"],
            "sub": f"e2e:{batch_id}",
            "root": sha256_hex_of_json(item),
            "pre": trace_id,
        },
    )
    return batch_id


def _verdict_rules(stack: Stack, batch_id: UUID) -> list[str]:
    row = wait_until(
        lambda: (
            found[0]
            if (
                found := stack.rows(
                    "SELECT outcome, vetoed_rule_ids FROM og.verdict WHERE command_batch_id = %(b)s",
                    {"b": batch_id},
                )
            )
            else None
        ),
        timeout_s=60,
        what=f"a guardian verdict on {batch_id}",
    )
    assert row["outcome"] != "PASS", "a stale-seq batch must never be signed"
    return list(row["vetoed_rule_ids"] or [])


def test_d18_a_closed_loop_reduction_is_corroborated_only_for_a_measured_feedback_profile(
    stack: Stack, delivering: tuple[Offer, dict]
) -> None:
    offer, obligation = delivering
    ob_id = obligation["obligation_id"]
    bank = stack.rows(
        "SELECT bank_id FROM og.grant WHERE obligation_id = %(o)s AND NOT is_headroom "
        "ORDER BY created_at DESC LIMIT 1",
        {"o": ob_id},
    )[0]["bank_id"]

    _insert_profile(stack, offer.contract_id, "PLAN")
    fixed_rules = _verdict_rules(stack, _propose_closed_loop(stack, ob_id, bank, reduced_kw=1.0))
    assert "G-19" in fixed_rules, f"a fixed profile's closed-loop reduction must be vetoed: {fixed_rules}"

    _insert_profile(stack, offer.contract_id, "MEASURED_FEEDBACK")
    measured_rules = _verdict_rules(stack, _propose_closed_loop(stack, ob_id, bank, reduced_kw=1.0))
    assert "G-19" not in measured_rules, (
        f"a MEASURED_FEEDBACK closed-loop reduction must pass G-19: {measured_rules}"
    )
