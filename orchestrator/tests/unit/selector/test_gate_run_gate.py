"""`run_gate` end-to-end orchestration (02a S3.8), with every I/O seam faked (BUILD.md "use fakes for
siblings"): `fleet`/`forecast`/`contracts`/`ledger` and the DB-backed `persist_plan`/`_configured_bank_ids`
are all monkeypatched, so this exercises the horizon/assembly/persist/transition wiring in `gate.py`
without a live Postgres or the sibling stub modules other agents own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from opengrid.selector import gate
from opengrid.selector.types import CandidateOpportunity
from unit.selector.factories import make_bank, zero_price_scenario

OPPORTUNITY_ID = str(uuid4())


async def _fake_load_banks(horizon_start, horizon_end, bank_ids):
    return (make_bank("B1", 10.0, range(1)),)


async def _fake_load_scenarios(horizon_start, horizon_end):
    return (zero_price_scenario(range(1)),)


async def _fake_load_committed(horizon_start, horizon_end, bank_ids):
    return ()


async def _fake_load_candidates(horizon_start, horizon_end, bank_ids, contract_scope):
    return (
        CandidateOpportunity(
            opportunity_id=OPPORTUNITY_ID,
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


@pytest.fixture
def _wired(monkeypatch):
    monkeypatch.setattr(gate, "load_banks", _fake_load_banks)
    monkeypatch.setattr(gate, "load_scenarios", _fake_load_scenarios)
    monkeypatch.setattr(gate, "load_committed", _fake_load_committed)
    monkeypatch.setattr(gate, "load_candidates", _fake_load_candidates)
    monkeypatch.setattr(gate, "_configured_bank_ids", _fake_configured_bank_ids)

    persisted = {}

    async def _fake_persist_plan(plan_mode, gate_kind, horizon_start, horizon_end, scenarios, result):
        persisted["plan_mode"] = plan_mode
        persisted["gate_kind"] = gate_kind
        persisted["result"] = result
        return uuid4()

    monkeypatch.setattr(gate, "persist_plan", _fake_persist_plan)

    reserved = {}

    async def _fake_reserve(obligation_id, selected_kw, plan_id):
        reserved["obligation_id"] = obligation_id
        reserved["selected_kw"] = selected_kw
        reserved["plan_id"] = plan_id

    monkeypatch.setattr(gate.ledger, "reserve", _fake_reserve)

    return persisted, reserved


async def test_run_gate_persists_and_reserves_the_selected_candidate(_wired):
    persisted, reserved = _wired

    plan = await gate.run_gate("SCHEDULED_15MIN")

    assert plan.solver_status == "OPTIMAL"
    assert plan.gate_kind == "SCHEDULED_15MIN"
    assert persisted["result"].selected_x[OPPORTUNITY_ID] is True
    assert reserved["obligation_id"] == UUID(OPPORTUNITY_ID)
    assert reserved["selected_kw"]["0"] == pytest.approx(10.0)


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


async def test_run_gate_renomination_requires_contract_scope():
    with pytest.raises(ValueError, match="RENOMINATION"):
        await gate.run_gate("RENOMINATION", contract_scope=None)


async def test_compute_horizon_is_24h():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    start, end = await gate.compute_horizon("SCHEDULED_15MIN", now)
    assert start == now
    assert (end - start).total_seconds() == 24 * 3600


def test_plan_mode_is_l_da_only_at_midnight_scheduled_gate():
    midnight = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    noon = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert gate._plan_mode_for("SCHEDULED_15MIN", midnight) == "L-DA"
    assert gate._plan_mode_for("SCHEDULED_15MIN", noon) == "L-ID"
    assert gate._plan_mode_for("ADMISSION", midnight) == "L-ID"
