"""Unit tests for `opengrid.ledger` (02a S4). Maps to TS-05-02 (one buyer), TS-05-07/11 (commitment
lock), TS-05-12 (AS release default off), ES05-S04 (substitution)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.ledger import (
    CommitmentLockViolation,
    ReservationError,
    ReservationLedger,
    decode_interval_key,
    encode_interval_key,
)

T0 = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 26, 18, 15, tzinfo=UTC)


def _key(bank_id: str = "bank-1") -> str:
    return encode_interval_key(bank_id, T0, T1)


def test_encode_decode_interval_key_roundtrip():
    key = encode_interval_key("bank-1", T0, T1)
    bank_id, start, end = decode_interval_key(key)
    assert (bank_id, start, end) == ("bank-1", T0, T1)


def test_decode_interval_key_rejects_malformed_input():
    with pytest.raises(ValueError, match="malformed"):
        decode_interval_key("not-a-valid-key")


async def test_ts_05_02_reserve_succeeds_within_capability(ledger: ReservationLedger):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(40)}, uuid4())
    assert await ledger.ledger_version() == 1
    assert await ledger.free_headroom("bank-1", T0) == Decimal(60)


async def test_ts_05_02_reserve_refuses_to_exceed_capability(ledger: ReservationLedger, capability):
    capability.default_kw = Decimal(50)
    await ledger.reserve(uuid4(), {_key(): Decimal(40)}, uuid4())

    with pytest.raises(ReservationError) as exc_info:
        await ledger.reserve(uuid4(), {_key(): Decimal(20)}, uuid4())
    assert exc_info.value.reason_code == "R-COMMIT-LOCK-INFEASIBLE"
    assert exc_info.value.detail == {
        "bank_id": "bank-1",
        "interval_start": T0.isoformat(),
        "requested_kw": 20.0,
        "reserved_by_others_kw": 40.0,
        "capability_kw": 50.0,
    }
    # the failed attempt must not have written anything (all-or-nothing)
    assert await ledger.free_headroom("bank-1", T0) == Decimal(10)


async def test_reserve_is_all_or_nothing_across_intervals(ledger: ReservationLedger, capability):
    capability.per_bank_kw = {"bank-1": Decimal(10), "bank-2": Decimal(1000)}
    obligation_id = uuid4()
    profile = {
        encode_interval_key("bank-1", T0, T1): Decimal(20),  # exceeds capability
        encode_interval_key("bank-2", T0, T1): Decimal(5),
    }
    with pytest.raises(ReservationError):
        await ledger.reserve(obligation_id, profile, uuid4())
    # bank-2's headroom must be untouched since the whole reserve() call failed
    assert await ledger.free_headroom("bank-2", T0) == Decimal(1000)


async def test_ledger_version_strictly_increases_across_writes(ledger: ReservationLedger):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    v1 = await ledger.ledger_version()
    await ledger.reserve(uuid4(), {_key("bank-2"): Decimal(10)}, uuid4())
    v2 = await ledger.ledger_version()
    assert v2 > v1


async def test_ts_05_07_release_without_reason_raises_commitment_lock_violation(
    ledger: ReservationLedger, backend
):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    with pytest.raises(CommitmentLockViolation):
        await ledger.release(reservation_id, "R-BECAUSE-BETTER-PRICE")


async def test_ts_05_07_release_with_allowed_reason_succeeds(ledger: ReservationLedger, backend):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    await ledger.release(reservation_id, "R-COMMIT-LOCK-OVERRIDE-L1")
    assert not backend.rows[reservation_id].is_active
    assert await ledger.free_headroom("bank-1", T0) == Decimal(100)


async def test_ts_05_12_as_release_disabled_by_default(ledger: ReservationLedger, backend):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    with pytest.raises(CommitmentLockViolation):
        await ledger.release(reservation_id, "R-AS-RELEASE")


async def test_as_release_allowed_when_explicitly_enabled(backend, capability):
    ledger = ReservationLedger(backend, capability, as_release_enabled=True)
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    await ledger.release(reservation_id, "R-AS-RELEASE")
    assert not backend.rows[reservation_id].is_active


async def test_release_unknown_reservation_raises(ledger: ReservationLedger):
    with pytest.raises(ReservationError):
        await ledger.release(uuid4(), "R-COMMIT-LOCK-OVERRIDE-L0")


async def test_reduce_below_floor_without_reason_is_blocked(ledger: ReservationLedger, backend):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    with pytest.raises(CommitmentLockViolation):
        await ledger.reduce(reservation_id, Decimal(5), reason_code="R-JUST-BECAUSE")


async def test_es05_s04_substitution_preserves_obligation_total(
    ledger: ReservationLedger, backend, capability
):
    capability.per_bank_kw = {"bank-1": Decimal(100), "bank-2": Decimal(100)}
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key("bank-1"): Decimal(30)}, uuid4())
    (old_reservation_id,) = backend.rows.keys()

    new_id = await ledger.substitute(obligation_id, old_reservation_id, "bank-2")

    assert not backend.rows[old_reservation_id].is_active
    assert backend.rows[old_reservation_id].release_reason == "R-SUBSTITUTION"
    new_record = backend.rows[new_id]
    assert new_record.is_active
    assert new_record.bank_id == "bank-2"
    assert new_record.amount_kw == Decimal(30)
    # obligation's committed total across active reservations is unchanged
    active_total = sum(
        r.amount_kw for r in backend.rows.values() if r.obligation_id == obligation_id and r.is_active
    )
    assert active_total == Decimal(30)


async def test_reservations_for_obligation_returns_all_records(ledger: ReservationLedger):
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    records = await ledger.reservations_for_obligation(obligation_id)
    assert len(records) == 1
    assert records[0].obligation_id == obligation_id


async def test_reservations_for_obligation_falls_back_to_backend_on_cache_miss(ledger: ReservationLedger):
    assert await ledger.reservations_for_obligation(uuid4()) == []


async def test_substitute_unknown_reservation_raises(ledger: ReservationLedger):
    with pytest.raises(ReservationError, match="NOT-FOUND"):
        await ledger.substitute(uuid4(), uuid4(), "bank-2")


async def test_substitution_across_obligations_is_rejected(ledger: ReservationLedger, backend):
    obligation_id = uuid4()
    other_obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    with pytest.raises(ReservationError, match="OBLIGATION-MISMATCH"):
        await ledger.substitute(other_obligation_id, reservation_id, "bank-2")


async def test_substitution_refuses_to_exceed_target_bank_capability(
    ledger: ReservationLedger, backend, capability
):
    capability.per_bank_kw = {"bank-1": Decimal(100), "bank-2": Decimal(5)}
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key("bank-1"): Decimal(30)}, uuid4())
    (reservation_id,) = backend.rows.keys()

    with pytest.raises(ReservationError):
        await ledger.substitute(obligation_id, reservation_id, "bank-2")
    # original reservation must remain active/untouched since substitution failed
    assert backend.rows[reservation_id].is_active


T2 = datetime(2026, 9, 26, 18, 30, tzinfo=UTC)


async def test_ts_05_03_reserve_writes_one_commitment_row_per_interval(ledger: ReservationLedger, backend):
    """Regression (live 2026-09-26): `reserve()` wrote reservations but never the equality-freeze
    `og.commitment` rows (02a S2.1 `SELECTED -> COMMITTED`), so no obligation was ever frozen for the
    next gate (C24) and the same OFFERED candidate was re-reserved every gate until K2 tripped."""
    obligation_id, plan_id = uuid4(), uuid4()
    profile = {
        encode_interval_key("bank-1", T0, T1): Decimal(30),
        encode_interval_key("bank-2", T0, T1): Decimal(20),
        encode_interval_key("bank-1", T1, T2): Decimal(50),
    }

    await ledger.reserve(obligation_id, profile, plan_id, variable_kind="BINARY")

    by_start = {c.interval_start: c for c in backend.commitments.values()}
    assert set(by_start) == {T0, T1}
    assert by_start[T0].committed_kw == Decimal(50)  # summed across banks for the interval
    assert by_start[T0].interval_end == T1
    assert by_start[T1].committed_kw == Decimal(50)
    assert all(c.plan_id == plan_id and c.obligation_id == obligation_id for c in by_start.values())
    assert all(c.variable_kind == "BINARY" for c in by_start.values())


async def test_ts_05_03_failed_reserve_writes_no_commitment(ledger: ReservationLedger, backend, capability):
    capability.default_kw = Decimal(10)
    with pytest.raises(ReservationError):
        await ledger.reserve(uuid4(), {_key(): Decimal(20)}, uuid4())
    assert backend.commitments == {}
    assert backend.rows == {}


async def test_ts_05_07_second_reserve_for_same_obligation_is_refused(ledger: ReservationLedger, backend):
    """K13: an obligation already committed for an interval cannot be re-reserved on top (the DB's
    `ux_commitment_active` index); the ledger surfaces it as a reservation error, not a crash."""
    obligation_id = uuid4()
    await ledger.reserve(obligation_id, {_key(): Decimal(10)}, uuid4())
    with pytest.raises(ReservationError, match="ALREADY-COMMITTED"):
        await ledger.reserve(obligation_id, {_key("bank-2"): Decimal(10)}, uuid4())
    assert len(backend.rows) == 1


async def test_release_uncommitted_frees_orphan_reservations(ledger: ReservationLedger, backend):
    """Legacy rows from before commitments were written (live 2026-09-26: 9,991 active reservations on
    OFFERED obligations) must not keep consuming K2 headroom."""
    orphan_id = uuid4()
    await ledger.reserve(uuid4(), {_key(): Decimal(10)}, uuid4())  # committed, must survive
    await ledger.reserve(orphan_id, {_key("bank-2"): Decimal(10)}, uuid4())
    for commitment_id, commitment in list(backend.commitments.items()):
        if commitment.obligation_id == orphan_id:
            del backend.commitments[commitment_id]

    released = await ledger.release_uncommitted()

    assert released == 1
    active = [r for r in backend.rows.values() if r.is_active]
    assert len(active) == 1
    assert active[0].obligation_id != orphan_id
    orphan = next(r for r in backend.rows.values() if r.obligation_id == orphan_id)
    assert orphan.release_reason == "R-COMMIT-LOCK-INFEASIBLE"


async def test_ts_06_09_restarted_ledger_reports_the_durable_version(backend, capability):
    """Regression (live 2026-09-26): a restarted og-engine reported ledger_version 0 until its first
    write, while the guardian reads the durable version -- G-09 would veto every batch. The version is
    loaded from the backend, and an empty `release_uncommitted()` does not leave a phantom bump."""
    first = ReservationLedger(backend, capability)
    await first.reserve(uuid4(), {_key(): Decimal(10)}, uuid4())
    durable = await first.ledger_version()

    restarted = ReservationLedger(backend, capability)
    assert await restarted.ledger_version() == durable
    assert await restarted.release_uncommitted() == 0
    assert await restarted.ledger_version() == durable == await backend.current_version()


async def test_module_level_functions_require_configure():
    import opengrid.ledger as ledger_module

    ledger_module._instance = None
    with pytest.raises(RuntimeError, match="configure"):
        await ledger_module.ledger_version()


async def test_module_level_functions_delegate_to_configured_instance(ledger: ReservationLedger):
    import opengrid.ledger as ledger_module

    ledger_module.configure(ledger)
    try:
        obligation_id = uuid4()
        await ledger_module.reserve(obligation_id, {_key(): Decimal(15)}, uuid4())
        assert await ledger_module.ledger_version() == 1
        assert await ledger_module.free_headroom("bank-1", T0) == Decimal(85)
        records = await ledger.reservations_for_obligation(obligation_id)
        await ledger_module.release(records[0].reservation_id, "R-COMMIT-LOCK-OVERRIDE-L0")
        assert await ledger_module.free_headroom("bank-1", T0) == Decimal(100)
    finally:
        ledger_module._instance = None
