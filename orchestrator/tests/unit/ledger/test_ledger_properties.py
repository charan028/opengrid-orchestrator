"""Hypothesis property tests for `opengrid.ledger` (BUILD.md S5a, TS-05-02/07/11).

Replays random sequences of reserve/release/substitute/new-better-offer operations and asserts the
K2/K13 invariants hold after every step:
  - sum of active reservations per (bank, interval) never exceeds capability (K2);
  - a committed reservation is never reduced/released without an allowed reason code (K13);
  - substitution preserves an obligation's committed total;
  - the ledger version strictly increases on every successful write.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.ledger import (
    ALLOWED_RELEASE_REASONS,
    CommitmentLockViolation,
    ReservationError,
    ReservationLedger,
    encode_interval_key,
)

from .conftest import FixedCapability, InMemoryLedgerBackend

T0 = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 26, 18, 15, tzinfo=UTC)
BANKS = ["bank-a", "bank-b"]
CAPABILITY_KW = Decimal(100)

# "new-better-offer" reasons are never in the allowed set -- exercising them must never succeed in
# reducing a committed reservation (review S3: "switching to a different obligation forbidden").
DISALLOWED_REASONS = ["R-BETTER-PRICE", "R-NEW-OFFER", ""]


@st.composite
def _operation(draw: st.DrawFn) -> tuple[str, ...]:
    kind = draw(st.sampled_from(["reserve", "release_valid", "release_invalid", "substitute"]))
    bank = draw(st.sampled_from(BANKS))
    other_bank = draw(st.sampled_from([b for b in BANKS if b != bank] or BANKS))
    # A zero-kW reservation has nothing to protect (releasing it to zero is a no-op against the K13
    # floor regardless of reason), so keep amounts strictly positive to exercise real reductions.
    amount = draw(st.decimals(min_value="0.1", max_value=50, places=1, allow_nan=False, allow_infinity=False))
    reason = draw(st.sampled_from(sorted(ALLOWED_RELEASE_REASONS - {"R-AS-RELEASE"})))
    disallowed_reason = draw(st.sampled_from(DISALLOWED_REASONS))
    return kind, bank, other_bank, str(amount), reason, disallowed_reason


def _run_scenario(operations: list[tuple[str, ...]]) -> None:
    async def scenario() -> None:
        backend = InMemoryLedgerBackend()
        capability = FixedCapability(default_kw=CAPABILITY_KW)
        ledger = ReservationLedger(backend, capability)

        committed_obligations: list[UUID] = []
        last_version = 0

        for kind, bank, other_bank, amount_s, reason, disallowed_reason in operations:
            amount = Decimal(amount_s)
            if kind == "reserve":
                obligation_id = uuid4()
                key = encode_interval_key(bank, T0, T1)
                try:
                    await ledger.reserve(obligation_id, {key: amount}, uuid4())
                except ReservationError:
                    pass
                else:
                    committed_obligations.append(obligation_id)
                    new_version = await ledger.ledger_version()
                    assert new_version > last_version  # ledger version strictly increases
                    last_version = new_version

            elif kind in ("release_valid", "release_invalid") and committed_obligations:
                obligation_id = committed_obligations[0]
                active = [
                    r for r in backend.rows.values() if r.obligation_id == obligation_id and r.is_active
                ]
                if not active:
                    continue
                record = active[0]
                use_reason = reason if kind == "release_valid" else disallowed_reason
                if use_reason in ALLOWED_RELEASE_REASONS - {"R-AS-RELEASE"}:
                    await ledger.release(record.reservation_id, use_reason)
                    new_version = await ledger.ledger_version()
                    assert new_version > last_version
                    last_version = new_version
                else:
                    prior_amount = record.amount_kw
                    try:
                        await ledger.release(record.reservation_id, use_reason)
                    except CommitmentLockViolation:
                        pass
                    else:  # pragma: no cover - would be a K13 violation if ever reached
                        raise AssertionError("release with a disallowed reason must raise")
                    # K13: the reservation must be untouched after a rejected release
                    assert backend.rows[record.reservation_id].amount_kw == prior_amount
                    assert backend.rows[record.reservation_id].is_active

            elif kind == "substitute" and committed_obligations:
                obligation_id = committed_obligations[0]
                active = [
                    r for r in backend.rows.values() if r.obligation_id == obligation_id and r.is_active
                ]
                if not active:
                    continue
                record = active[0]
                prior_total = sum(
                    r.amount_kw
                    for r in backend.rows.values()
                    if r.obligation_id == obligation_id and r.is_active
                )
                with contextlib.suppress(ReservationError):
                    await ledger.substitute(obligation_id, record.reservation_id, other_bank)
                new_total = sum(
                    r.amount_kw
                    for r in backend.rows.values()
                    if r.obligation_id == obligation_id and r.is_active
                )
                assert new_total == prior_total  # substitution preserves the obligation's committed total

            # K2, checked after every operation: sum of active reservations per (bank, interval) never
            # exceeds capability.
            for b in BANKS:
                active_kw = sum(
                    r.amount_kw
                    for r in backend.rows.values()
                    if r.bank_id == b and r.interval_start == T0 and r.is_active
                )
                assert active_kw <= CAPABILITY_KW

    asyncio.run(scenario())


@settings(max_examples=200)
@given(operations=st.lists(_operation(), min_size=1, max_size=25))
def test_ledger_invariants_hold_over_random_operation_sequences(operations: list[tuple[str, ...]]) -> None:
    _run_scenario(operations)
