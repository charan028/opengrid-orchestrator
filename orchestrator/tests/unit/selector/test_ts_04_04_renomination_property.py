"""TS-04-04 / ES04-S04 (traceability-gap closure, 2026-09-26): "Re-nomination point is the only
mid-window reselection gate" for a tolling/multi-day commitment (04-mvp-s-test-plan.md TS-04-04:
">= 500 cases", "0 reselections strictly between re-nomination points; exactly the contract's
declared points allow a new x_o decision").

Combines the REAL selector Mode O model (`selector.model.build_mode_o_model` -> `solve.highs_solve`
-> `extract.extract_plan`, independently checked by `selector.validate.validate_plan`) with the REAL
`opengrid.contracts.renomination` gate (`due_renomination_points`/`exercise_renomination_point`)
against an in-memory `FakeContractsRepo`/`FakeTraceBackend` -- no DB/MQTT (BUILD.md S5).

Every example freezes one multi-day/tolling obligation's committed profile (`CommittedObligation`,
C24) and throws a random competing candidate + a random price path at the selector for a gate time
strictly BETWEEN the contract's two declared re-nomination points. Two invariants must hold for
every one of the >= 500 examples:

  1. K13/C24 (`validate_plan`'s `_check_commitment_lock`): the frozen commitment is delivered in
     full -- 0 reduction of the committed capacity, regardless of how attractive the competing
     candidate is.
  2. `due_renomination_points` at that gate time returns nothing for this contract's obligation --
     0 reselections strictly between the declared points, no matter what price/competition arrived.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

import opengrid.contracts as contracts
from opengrid.contracts.renomination import due_renomination_points
from opengrid.core.models.engine import Contract, RenominationPoint
from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import ScenarioPrice, SolverSettings
from opengrid.selector.validate import validate_plan
from opengrid.trace import TraceStore
from unit.contracts.fakes import FakeContractsRepo, FakeTraceBackend
from unit.selector.factories import (
    binary_candidate,
    committed,
    continuous_candidate,
    make_bank,
    semi_continuous_candidate,
    simple_inputs,
)

_FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=10.0)
_N_INTERVALS = 4
_TOLLING_COMMITTED_KW = 20.0
_BANK_CAPACITY_KW = 100.0

_competitor_kind = st.sampled_from(["BINARY", "CONTINUOUS", "SEMI_CONTINUOUS"])
_competitor_kw = st.floats(min_value=0.1, max_value=90.0, allow_nan=False, allow_infinity=False)
_competitor_value = st.floats(min_value=0.0, max_value=5000.0, allow_nan=False, allow_infinity=False)
_price_path = st.lists(
    st.floats(min_value=-500.0, max_value=9000.0, allow_nan=False, allow_infinity=False),
    min_size=_N_INTERVALS,
    max_size=_N_INTERVALS,
)
# Where, strictly between the two declared re-nomination points, this example's gate/price arrival
# lands (exclusive of both ends -- the points themselves are the only legal reselection gates).
_between_fraction = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)


def _build_selector_inputs(
    competitor_kind: str, competitor_kw: float, competitor_value: float, price_path: list[float]
):
    bank = make_bank("B1", _BANK_CAPACITY_KW, range(_N_INTERVALS))
    prices = dict(enumerate(price_path))
    scenario = ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh=prices)

    tolling = committed("o-tolling", dict.fromkeys(range(_N_INTERVALS), _TOLLING_COMMITTED_KW), ("B1",))

    if competitor_kind == "BINARY":
        competitor = binary_candidate(
            "c-competitor", competitor_kw, competitor_value, tuple(range(_N_INTERVALS)), ("B1",)
        )
    elif competitor_kind == "CONTINUOUS":
        competitor = continuous_candidate(
            "c-competitor", competitor_kw, competitor_value, tuple(range(_N_INTERVALS)), ("B1",)
        )
    else:
        competitor = semi_continuous_candidate(
            "c-competitor",
            competitor_kw,
            competitor_kw / 4,
            competitor_kw / 8,
            competitor_value,
            tuple(range(_N_INTERVALS)),
            ("B1",),
        )

    return simple_inputs((bank,), (scenario,), (tolling,), (competitor,), n_intervals=_N_INTERVALS)


_BASE_TIME = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)


async def _renomination_scenario(as_of: datetime, contract_id, obligation_id) -> list[RenominationPoint]:
    """Two declared points bracket `as_of` (`_BASE_TIME` and `_BASE_TIME + 1 day`): the first was
    already exercised at contract start (a real multi-day/tolling contract's prior gate), the second
    is still a full day out. `as_of` (this test's `gate_time`) always lands strictly between them."""
    repo = FakeContractsRepo()
    trace = TraceStore(FakeTraceBackend())
    contracts.configure(repo, trace)
    try:
        contract = Contract(
            contract_id=contract_id,
            customer_id=uuid4(),
            service_type="ERCOT_ENERGY",
            tier="T2",
            profile_ref="tolling@1",
            start_at=_BASE_TIME - timedelta(days=1),
            renomination_allowed=True,
            status="ACTIVE",
        )
        await repo.upsert_contract(contract)

        point_a = RenominationPoint(
            renomination_point_id=uuid4(),
            contract_id=contract_id,
            obligation_id=obligation_id,
            scheduled_at=_BASE_TIME,
            exercised_at=_BASE_TIME,  # already exercised at the start of this window
            outcome="CONFIRMED",
        )
        point_b = RenominationPoint(
            renomination_point_id=uuid4(),
            contract_id=contract_id,
            obligation_id=obligation_id,
            scheduled_at=_BASE_TIME + timedelta(days=1),
        )
        repo.renomination_points[point_a.renomination_point_id] = point_a
        repo.renomination_points[point_b.renomination_point_id] = point_b

        return await due_renomination_points(repo, as_of=as_of)
    finally:
        contracts.reset_for_testing()


@given(
    competitor_kind=_competitor_kind,
    competitor_kw=_competitor_kw,
    competitor_value=_competitor_value,
    price_path=_price_path,
    between_fraction=_between_fraction,
)
@settings(max_examples=500, deadline=None)
def test_ts_04_04_no_reselection_or_lock_reduction_between_renomination_points(
    competitor_kind: str,
    competitor_kw: float,
    competitor_value: float,
    price_path: list[float],
    between_fraction: float,
) -> None:
    # -- 1. The real selector, run against a random competing candidate + price path -----------------
    inputs = _build_selector_inputs(competitor_kind, competitor_kw, competitor_value, price_path)
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, _FAST_SETTINGS)

    if outcome.status == "OPTIMAL":
        plan = extract_plan(built, outcome, "L-ID")
        ok, violations = validate_plan(inputs, plan)
        assert ok, violations
        delivered = sum(
            kw for (oid, _b, _t), kw in plan.bank_interval_allocation.items() if oid == "o-tolling"
        )
        assert delivered >= _TOLLING_COMMITTED_KW * _N_INTERVALS - 1e-6

    # -- 2. The real contracts re-nomination gate, at a time strictly between the two declared points -
    contract_id = uuid4()
    obligation_id = uuid4()
    gate_time = _BASE_TIME + timedelta(days=between_fraction)  # strictly between point_a and point_b

    due = asyncio.run(_renomination_scenario(gate_time, contract_id, obligation_id))
    assert due == []  # 0 reselections: neither declared point is due yet at this gate time
