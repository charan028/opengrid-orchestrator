"""`run_gate` end-to-end orchestration (02a S3.8), with every I/O seam faked (BUILD.md "use fakes for
siblings"): `fleet`/`forecast`/`contracts`/`ledger` and the DB-backed `persist_plan`/`_configured_bank_ids`
are all monkeypatched, so this exercises the horizon/assembly/persist/transition wiring in `gate.py`
without a live Postgres or the sibling stub modules other agents own.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

import opengrid.ledger as ledger_module
from opengrid.ledger import decode_interval_key
from opengrid.market import MarketModel
from opengrid.selector import commit, gate
from opengrid.selector.types import CandidateOpportunity
from unit.selector.factories import make_bank, zero_price_scenario

OPPORTUNITY_ID = str(uuid4())
OBLIGATION_ID = str(uuid4())  # deliberately distinct from OPPORTUNITY_ID -- see the FK-violation bug fix


async def _fake_load_banks(horizon_start, horizon_end, bank_ids):
    return (make_bank("B1", 10.0, range(1)),)


async def _fake_load_scenarios(horizon_start, horizon_end, bank_ids=()):
    return (zero_price_scenario(range(1)),)


async def _fake_load_committed(horizon_start, horizon_end, bank_ids):
    return ()


async def _fake_load_candidates(horizon_start, horizon_end, bank_ids, contract_scope):
    return (
        CandidateOpportunity(
            opportunity_id=OPPORTUNITY_ID,
            obligation_id=OBLIGATION_ID,
            contract_id=str(uuid4()),
            eligible_bank_ids=("B1",),
            window_intervals=(0,),
            requested_kw=10.0,
            value_per_mwh=100.0,
            variable_kind="BINARY",
            min_qty_kw=0.0,
            increment_kw=0.0,
        ),
    )


async def _fake_configured_bank_ids():
    return ("B1",)


async def _fake_load_market(bank_ids):
    zones = dict.fromkeys(bank_ids, "LZ_NORTH")
    return MarketModel(zone_territory={}, utilities={}, banks=zones.items()), zones


@pytest.fixture
def _wired(monkeypatch):
    monkeypatch.setattr(gate, "load_banks", _fake_load_banks)
    monkeypatch.setattr(gate, "load_scenarios", _fake_load_scenarios)
    monkeypatch.setattr(gate, "load_committed", _fake_load_committed)
    monkeypatch.setattr(gate, "load_candidates", _fake_load_candidates)
    monkeypatch.setattr(gate, "load_market", _fake_load_market)
    monkeypatch.setattr(gate, "_configured_bank_ids", _fake_configured_bank_ids)

    persisted = {}

    async def _fake_insert_plan_analytics(value_row, shadow_rows, energy_rows):
        persisted["analytics"] = (value_row, shadow_rows, energy_rows)

    monkeypatch.setattr(gate.db, "insert_plan_analytics", _fake_insert_plan_analytics)

    async def _no_degraded_modes():
        return frozenset()

    async def _all_series_fit(horizon_start, horizon_end):
        return frozenset()

    monkeypatch.setattr(gate.db, "load_degraded_modes", _no_degraded_modes)
    monkeypatch.setattr(gate.db, "load_unfit_price_series", _all_series_fit)

    async def _no_solar(zone_by_bank, horizon_start, n_intervals, now):
        return {}

    async def _prune(keep_days=7):
        persisted["pruned"] = keep_days
        return 0

    monkeypatch.setattr(gate, "load_solar_shares", _no_solar)
    monkeypatch.setattr(gate.db, "prune_plan_energy_value", _prune)

    async def _fake_persist_plan(plan_mode, gate_kind, horizon_start, horizon_end, scenarios, result):
        persisted["plan_mode"] = plan_mode
        persisted["gate_kind"] = gate_kind
        persisted["result"] = result
        return uuid4()

    monkeypatch.setattr(gate, "persist_plan", _fake_persist_plan)

    reserved = {}

    async def _fake_reserve(obligation_id, selected_kw, plan_id, *, variable_kind="CONTINUOUS"):
        reserved["obligation_id"] = obligation_id
        reserved["selected_kw"] = selected_kw
        reserved["plan_id"] = plan_id
        reserved["variable_kind"] = variable_kind

    monkeypatch.setattr(commit.ledger, "reserve", _fake_reserve)
    monkeypatch.setattr(commit.contracts, "transition_obligation", _RecordingTransitions())
    monkeypatch.setattr(commit.contracts, "record_opportunity_decision", _RecordingDecisions())

    return persisted, reserved


class _RecordingDecisions:
    """Fake `contracts.record_opportunity_decision`: records `(opportunity_id, state, reason, gate)`."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, str, UUID]] = []

    async def __call__(self, opportunity_id, state, *, reason_code, gate_id, decided_at=None):
        self.calls.append((opportunity_id, state, reason_code, gate_id))


class _RecordingTransitions:
    """Fake `contracts.transition_obligation`: records `(obligation_id, to_state, reason_code)`."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, str | None]] = []

    async def __call__(self, obligation_id, to_state, *, reason_code, payload=None, at_risk=None):
        self.calls.append((obligation_id, to_state, reason_code))


async def test_ts_05_03_run_gate_commits_the_selected_obligation(_wired):
    """Regression (live 2026-09-26): the gate reserved but never moved the obligation through
    `OFFERED -> SELECTED -> COMMITTED`, so it stayed OFFERED and was re-reserved every gate until
    `ledger.reserve()` failed K2 with `R-COMMIT-LOCK-INFEASIBLE` and crashed the engine tick."""
    _persisted, reserved = _wired
    transitions = commit.contracts.transition_obligation

    await gate.run_gate("SCHEDULED_15MIN")

    obligation_id = UUID(OBLIGATION_ID)
    assert transitions.calls == [
        (obligation_id, "SELECTED", "R-GATE-SELECT"),
        (obligation_id, "COMMITTED", "R-COMMIT-LOCK-ENTER"),
    ]
    assert reserved["variable_kind"] == "BINARY"


async def test_ts_05_03_infeasible_reserve_rejects_the_obligation_without_crashing_the_gate(
    monkeypatch, _wired
):
    """02a S2.1 `SELECTED -> REJECTED`: a K2 refusal (live capability shrank since the snapshot) with no
    live-headroom substitute rejects that one obligation; the gate still returns its plan."""
    transitions = commit.contracts.transition_obligation

    async def _refuse(obligation_id, selected_kw, plan_id, *, variable_kind="CONTINUOUS"):
        raise ledger_module.ReservationError("R-COMMIT-LOCK-INFEASIBLE")

    async def _no_headroom(bank_id, interval_start):
        return Decimal(0)

    monkeypatch.setattr(commit.ledger, "reserve", _refuse)
    monkeypatch.setattr(commit.ledger, "free_headroom", _no_headroom)

    plan = await gate.run_gate("SCHEDULED_15MIN")

    obligation_id = UUID(OBLIGATION_ID)
    assert plan.solver_status == "OPTIMAL"
    assert transitions.calls == [
        (obligation_id, "SELECTED", "R-GATE-SELECT"),
        (obligation_id, "REJECTED", "R-COMMIT-LOCK-INFEASIBLE"),
    ]


async def test_ts_05_03_infeasible_reserve_retries_on_live_headroom(monkeypatch, _wired):
    """Substitute before rejecting: when the planned bank no longer has the capacity, the obligation's
    per-interval total is re-placed on banks with live free headroom and reserved once more."""
    transitions = commit.contracts.transition_obligation
    attempts: list[dict[str, Decimal]] = []

    async def _refuse_first(obligation_id, selected_kw, plan_id, *, variable_kind="CONTINUOUS"):
        attempts.append(selected_kw)
        if len(attempts) == 1:
            raise ledger_module.ReservationError("R-COMMIT-LOCK-INFEASIBLE")

    async def _headroom(bank_id, interval_start):
        return Decimal(4) if bank_id == "B1" else Decimal(100)

    async def _two_banks(horizon_start, horizon_end, bank_ids):
        return (make_bank("B1", 10.0, range(1)), make_bank("B2", 0.0, range(1)))

    async def _two_bank_candidate(horizon_start, horizon_end, bank_ids, contract_scope):
        (candidate,) = await _fake_load_candidates(horizon_start, horizon_end, bank_ids, contract_scope)
        return (dataclasses.replace(candidate, eligible_bank_ids=("B1", "B2")),)

    async def _two_configured_banks():
        return ("B1", "B2")  # a bank must exist in og.bank (and so have a territory) to be eligible, K15

    monkeypatch.setattr(commit.ledger, "reserve", _refuse_first)
    monkeypatch.setattr(commit.ledger, "free_headroom", _headroom)
    monkeypatch.setattr(gate, "_configured_bank_ids", _two_configured_banks)
    monkeypatch.setattr(gate, "load_banks", _two_banks)
    monkeypatch.setattr(gate, "load_candidates", _two_bank_candidate)

    await gate.run_gate("SCHEDULED_15MIN")

    assert len(attempts) == 2
    retry = {decode_interval_key(k)[0]: v for k, v in attempts[1].items()}
    assert retry == {"B1": Decimal(4), "B2": Decimal(6)}
    assert transitions.calls[-1][1] == "COMMITTED"


async def test_run_gate_persists_and_reserves_the_selected_candidate(_wired):
    persisted, reserved = _wired

    plan = await gate.run_gate("SCHEDULED_15MIN")

    assert plan.solver_status == "OPTIMAL"
    assert plan.gate_kind == "SCHEDULED_15MIN"
    assert persisted["result"].selected_x[OPPORTUNITY_ID] is True
    # Bug fix regression (combined-deploy pass): `ledger.reserve()` must be called with the real
    # `obligation_id` (og.reservation's FK target), never `opportunity_id` -- confirmed live as
    # `ForeignKeyViolation` the moment the selector actually selected something.
    assert reserved["obligation_id"] == UUID(OBLIGATION_ID)
    # Bug fix regression (combined-deploy pass): the key must be `encode_interval_key(bank_id, start,
    # end)`, not the bare interval index -- `opengrid.ledger.reserve()`'s real `decode_interval_key`
    # raised `ValueError` on the old `str(t)` shape the moment the selector actually selected something.
    selected_kw = reserved["selected_kw"]
    assert len(selected_kw) == 1
    (key, amount) = next(iter(selected_kw.items()))
    bank_id, interval_start, interval_end = decode_interval_key(key)
    assert bank_id == "B1"
    assert (interval_end - interval_start).total_seconds() == pytest.approx(15 * 60)
    assert amount == pytest.approx(10.0)


async def test_run_gate_never_reserves_against_a_fabricated_bank_id(monkeypatch, _wired):
    """Regression for qa/merge-notes.md section 11: `_configured_bank_ids` must never fabricate a
    `bank-NN`-style id (disjoint from the real `bank-000`-style topology) that then flows through
    `load_banks`/`load_candidates`/`load_committed` into a real `ledger.reserve()` call."""
    _persisted, reserved = _wired

    async def _fake_configured_bank_ids_real_format():
        return ("bank-000",)

    monkeypatch.setattr(gate, "_configured_bank_ids", _fake_configured_bank_ids_real_format)

    async def _fake_load_banks_real_bank(horizon_start, horizon_end, bank_ids):
        assert bank_ids == ("bank-000",)
        return (make_bank("bank-000", 10.0, range(1)),)

    monkeypatch.setattr(gate, "load_banks", _fake_load_banks_real_bank)

    async def _fake_load_candidates_real_bank(horizon_start, horizon_end, bank_ids, contract_scope):
        assert bank_ids == ("bank-000",)
        return (
            CandidateOpportunity(
                opportunity_id=OPPORTUNITY_ID,
                obligation_id=OBLIGATION_ID,
                contract_id=str(uuid4()),
                eligible_bank_ids=bank_ids,
                window_intervals=(0,),
                requested_kw=10.0,
                value_per_mwh=100.0,
                variable_kind="BINARY",
                min_qty_kw=0.0,
                increment_kw=0.0,
            ),
        )

    monkeypatch.setattr(gate, "load_candidates", _fake_load_candidates_real_bank)

    await gate.run_gate("SCHEDULED_15MIN")

    # `load_candidates` (asserted above) and `ledger.reserve` (here) only ever saw the real bank id
    # `og.bank` returned -- never a fabricated `bank-NN` placeholder from a count/format guess.
    assert reserved["selected_kw"], "expected a real reservation to be made"


async def test_run_gate_persists_the_shadow_value_for_its_plan(_wired):
    """ES05-S07: every gate writes the LP-vs-rule values (og.plan_value) under its own plan id."""
    persisted, reserved = _wired

    await gate.run_gate("SCHEDULED_15MIN")

    value_row, shadow_rows, _energy_rows = persisted["analytics"]
    assert persisted["pruned"] == 7  # og.plan_energy_value retention runs with every gate
    assert value_row["plan_id"] == reserved["plan_id"]
    assert value_row["value_added"] == value_row["lp_net_value"] - value_row["rule_net_value"]
    assert [r["obligation_id"] for r in shadow_rows] == [OBLIGATION_ID]


async def test_plan_analytics_failure_never_fails_the_gate(monkeypatch, _wired):
    async def _no_table(*_args):
        raise RuntimeError('relation "og.plan_value" does not exist')

    monkeypatch.setattr(gate.db, "insert_plan_analytics", _no_table)

    plan = await gate.run_gate("SCHEDULED_15MIN")

    assert plan.solver_status == "OPTIMAL"


async def test_stale_price_feed_no_new_commitments_mode_makes_zero_new_commitments(monkeypatch, _wired):
    """02b S6.5 row 1 (review blocker): while health's NO_NEW_COMMITMENTS is active (a price feed crossed
    STALE), the gate reserves and commits nothing new; the offer stays OFFERED (neither selected nor
    rejected); the gate still runs and plans what is already committed (K13)."""
    _persisted, reserved = _wired
    transitions = commit.contracts.transition_obligation
    decisions = commit.contracts.record_opportunity_decision

    async def _stale():
        return frozenset({"NO_NEW_COMMITMENTS"})

    monkeypatch.setattr(gate.db, "load_degraded_modes", _stale)
    monkeypatch.setattr(gate, "_rated_kw_by_bank", lambda bank_ids: {"B1": 0.0})  # would reject if reached

    plan = await gate.run_gate("SCHEDULED_15MIN")

    assert plan.solver_status == "OPTIMAL"
    assert reserved == {}
    assert transitions.calls == []
    assert decisions.calls == []


async def test_unreadable_degraded_mode_fails_closed(monkeypatch, _wired):
    _persisted, reserved = _wired

    async def _db_down():
        raise OSError("connection refused")

    monkeypatch.setattr(gate.db, "load_degraded_modes", _db_down)

    await gate.run_gate("SCHEDULED_15MIN")

    assert reserved == {}
    assert commit.contracts.transition_obligation.calls == []


async def test_mode_clear_selects_and_commits_normally(_wired):
    _persisted, reserved = _wired

    await gate.run_gate("SCHEDULED_15MIN")

    assert reserved["obligation_id"] == UUID(OBLIGATION_ID)
    assert commit.contracts.transition_obligation.calls[-1][1] == "COMMITTED"


async def test_offer_priced_only_off_a_not_for_firm_series_is_withheld_not_rejected(monkeypatch, _wired):
    """The only bank is priced off a series forecast flags NOT_FOR_FIRM: nothing is committed, and the
    offer is NOT rejected (its structural capacity is fine; only this gate's data is unfit)."""
    _persisted, reserved = _wired

    async def _unfit(horizon_start, horizon_end):
        return frozenset({"LZ_NORTH"})

    async def _zoned_bank(horizon_start, horizon_end, bank_ids):
        return (dataclasses.replace(make_bank("B1", 10.0, range(1)), zone="LZ_NORTH"),)

    monkeypatch.setattr(gate.db, "load_unfit_price_series", _unfit)
    monkeypatch.setattr(gate, "load_banks", _zoned_bank)
    monkeypatch.setattr(gate, "_rated_kw_by_bank", lambda bank_ids: {"B1": 600.0})

    await gate.run_gate("SCHEDULED_15MIN")

    assert reserved == {}
    assert commit.contracts.transition_obligation.calls == []


async def test_unreadable_series_fitness_fails_closed(monkeypatch, _wired):
    _persisted, reserved = _wired

    async def _db_down(horizon_start, horizon_end):
        raise OSError("connection refused")

    monkeypatch.setattr(gate.db, "load_unfit_price_series", _db_down)

    await gate.run_gate("SCHEDULED_15MIN")

    assert reserved == {}


async def test_run_gate_renomination_requires_contract_scope():
    with pytest.raises(ValueError, match="RENOMINATION"):
        await gate.run_gate("RENOMINATION", contract_scope=None)


async def test_compute_horizon_is_24h():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    start, end = await gate.compute_horizon("SCHEDULED_15MIN", now)
    assert start == now
    assert (end - start).total_seconds() == 24 * 3600


async def test_compute_horizon_is_aligned_to_the_15_minute_interval():
    """Regression (live 2026-09-26): the horizon started at `now` (e.g. 23:56:58.95), so each gate's
    interval keys were unique -- K2's per-interval check never saw another gate's reservations and
    `load_committed` never matched a commitment to the horizon."""
    now = datetime(2026, 9, 26, 4, 56, 58, 953921, tzinfo=UTC)
    start, end = await gate.compute_horizon("ADMISSION", now)
    assert start == datetime(2026, 9, 26, 4, 45, tzinfo=UTC)
    assert (end - start).total_seconds() == 24 * 3600


def test_plan_mode_is_l_da_only_at_midnight_scheduled_gate():
    midnight = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    noon = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert gate._plan_mode_for("SCHEDULED_15MIN", midnight) == "L-DA"
    assert gate._plan_mode_for("SCHEDULED_15MIN", noon) == "L-ID"
    assert gate._plan_mode_for("ADMISSION", midnight) == "L-ID"


async def test_opportunity_records_the_gate_decision(monkeypatch, _wired):
    """Lead review 2026-09-26: og.opportunity.state/gate_id never moved off OFFERED/NULL. The gate's
    decision (and the deciding plan) is now recorded on the opportunity too."""
    _persisted, reserved = _wired
    decisions = commit.contracts.record_opportunity_decision

    await gate.run_gate("SCHEDULED_15MIN")

    ((opportunity_id, state, reason, gate_id),) = decisions.calls
    assert (opportunity_id, state, reason) == (UUID(OPPORTUNITY_ID), "SELECTED", "R-GATE-SELECT")
    assert gate_id == reserved["plan_id"]


async def test_structurally_infeasible_offer_is_rejected_r_admit_reject(monkeypatch, _wired):
    """02a S2.1 `OFFERED -> REJECTED` / `R-ADMIT-REJECT` had no production caller: an offer larger than
    every eligible bank's RATED capacity was re-offered at every gate forever."""
    transitions = commit.contracts.transition_obligation
    decisions = commit.contracts.record_opportunity_decision

    async def _huge_candidate(horizon_start, horizon_end, bank_ids, contract_scope):
        (candidate,) = await _fake_load_candidates(horizon_start, horizon_end, bank_ids, contract_scope)
        return (dataclasses.replace(candidate, requested_kw=50_000.0),)

    monkeypatch.setattr(gate, "load_candidates", _huge_candidate)
    monkeypatch.setattr(gate, "_rated_kw_by_bank", lambda bank_ids: {"B1": 600.0})

    await gate.run_gate("SCHEDULED_15MIN")

    assert transitions.calls == [(UUID(OBLIGATION_ID), "REJECTED", "R-ADMIT-REJECT")]
    assert [(c[1], c[2]) for c in decisions.calls] == [("REJECTED", "R-ADMIT-REJECT")]


async def test_an_offer_that_merely_is_not_selected_stays_offered(monkeypatch, _wired):
    transitions = commit.contracts.transition_obligation

    async def _unpriced(horizon_start, horizon_end, bank_ids, contract_scope):
        (candidate,) = await _fake_load_candidates(horizon_start, horizon_end, bank_ids, contract_scope)
        return (dataclasses.replace(candidate, value_per_mwh=-1_000.0),)

    monkeypatch.setattr(gate, "load_candidates", _unpriced)
    monkeypatch.setattr(gate, "_rated_kw_by_bank", lambda bank_ids: {"B1": 600.0})

    await gate.run_gate("SCHEDULED_15MIN")

    assert transitions.calls == []
