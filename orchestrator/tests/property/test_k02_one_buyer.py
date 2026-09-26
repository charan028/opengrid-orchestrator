"""K2 one buyer (00-invariants.md K2; TS-05-02): per bank and interval, reservations never exceed capability."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.limits import check_one_buyer
from opengrid.ledger import ReservationError, ReservationLedger, encode_interval_key

from .support import NOW, FlatCapability, InMemoryLedgerBackend, run

BANKS = ["bank-000", "bank-001", "bank-002"]
INTERVAL_END = NOW + timedelta(minutes=15)
_kw = st.floats(min_value=0.0, max_value=500.0, allow_nan=False)
_demands = st.lists(
    st.tuples(st.sampled_from(BANKS), st.decimals(min_value="0.1", max_value=60, places=1)), max_size=40
)


@given(st.lists(_kw, max_size=30), _kw)
def test_k02_check_one_buyer_accepts_exactly_the_sums_within_capability(reservations_kw, capability_kw):
    result = check_one_buyer(reservations_kw, capability_kw)

    assert result.ok == (sum(reservations_kw) <= capability_kw + 1e-9)


@given(_demands, st.decimals(min_value=10, max_value=200, places=1))
def test_k02_ledger_admits_obligations_first_fit_and_never_oversells(demands, capability_kw):
    async def scenario() -> None:
        backend = InMemoryLedgerBackend()
        ledger = ReservationLedger(backend, FlatCapability(capability_kw))
        sold = dict.fromkeys(BANKS, Decimal(0))
        for bank_id, amount in demands:
            key = encode_interval_key(bank_id, NOW, INTERVAL_END)
            fits = sold[bank_id] + amount <= capability_kw
            try:
                await ledger.reserve(uuid4(), {key: amount}, uuid4())
            except ReservationError:
                assert not fits
            else:
                assert fits
                sold[bank_id] += amount
        for bank_id in BANKS:
            active = await backend.active_reservations(bank_id, NOW)
            assert sum(r.amount_kw for r in active) == sold[bank_id] <= capability_kw

    run(scenario())


@given(_demands, st.decimals(min_value=10, max_value=200, places=1))
def test_k02_each_reservation_backs_exactly_one_obligation(demands, capability_kw):
    async def scenario() -> None:
        backend = InMemoryLedgerBackend()
        ledger = ReservationLedger(backend, FlatCapability(capability_kw))
        for bank_id, amount in demands:
            key = encode_interval_key(bank_id, NOW, INTERVAL_END)
            try:
                await ledger.reserve(uuid4(), {key: amount}, uuid4())
            except ReservationError:
                continue
        obligations = [r.obligation_id for r in backend.rows.values() if r.is_active]
        assert len(obligations) == len(set(obligations))
        assert len({r.reservation_id for r in backend.rows.values()}) == len(backend.rows)

    run(scenario())
