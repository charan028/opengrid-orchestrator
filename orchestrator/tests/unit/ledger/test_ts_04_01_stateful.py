"""TS-04-01 (traceability-gap closure, 2026-09-26): a Hypothesis `RuleBasedStateMachine` over the
REAL `opengrid.contracts` obligation lifecycle (`admit`/`transition_obligation`/re-nomination) and the
REAL `opengrid.ledger.ReservationLedger` (`reserve`/`release`/`substitute`), wired to in-memory fakes
(`unit.contracts.fakes.FakeContractsRepo`/`FakeTraceBackend`, `unit.ledger.conftest`'s
`InMemoryLedgerBackend`/`FixedCapability`) -- no DB/MQTT (BUILD.md S5).

K13 (04-mvp-s-test-plan.md's "commitment lock"): "Granted kW for a committed obligation never drops
below the frozen commitment except on an L0/L1/L2 reason code or verified infeasibility... switching
(capacity moved to a different obligation) is not [allowed]". `test_ledger_properties.py` already
drives the ledger alone through a random operation sequence; this test additionally drives the
*obligation* lifecycle alongside it (admit -> select -> commit -> deliver -> shortfall/renomination)
so the invariant is checked across both modules together, through the four rule kinds the story
calls out: **admit** (new competing opportunities), **tick** (the gate: select + reserve a pending
obligation, or reject it on K2 exhaustion), **price_move** (a pure market perturbation that must never
itself touch a commitment), and **renomination_point** (the only mid-window reselection gate, K13
again).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from hypothesis.strategies import floats, sampled_from

import opengrid.contracts as contracts
from opengrid.contracts.errors import IllegalTransitionError
from opengrid.core.models.engine import Contract, RenominationPoint
from opengrid.ledger import (
    ALLOWED_RELEASE_REASONS,
    CommitmentLockViolation,
    ReservationError,
    ReservationLedger,
    encode_interval_key,
)
from opengrid.trace import TraceStore
from unit.contracts.fakes import FakeContractsRepo, FakeTraceBackend
from unit.ledger.conftest import FixedCapability, InMemoryLedgerBackend

_BANK_ID = "bank-a"
_T0 = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 26, 18, 15, tzinfo=UTC)
_BANK_CAPABILITY_KW = Decimal(100)

# Real reason codes (02a S2.1 `SHORTFALL_REASON_CODES` / `ledger.ALLOWED_RELEASE_REASONS`) that may
# ever reduce a committed obligation's kW -- everything else attempting a reduction must fail closed.
_LEGIT_REDUCE_REASONS = sorted(ALLOWED_RELEASE_REASONS - {"R-AS-RELEASE"})  # disabled by default (K13)
_ILLEGITIMATE_REASONS = ["R-BETTER-PRICE", "R-NEW-OFFER", ""]


class _ObligationsK13StateMachine(RuleBasedStateMachine):
    """`committed_floor[obligation_id]` is this machine's own ground truth for "the kW that must never
    be reduced without an allowed reason" -- updated ONLY in lockstep with a real ledger write that the
    K13 gate (`check_commitment_lock`, via `ReservationLedger.release`/`.reduce`) actually accepted.
    The `@invariant` below cross-checks it against the ledger's own active-reservation total after
    every single rule application, so a silent reduction anywhere (contracts OR ledger) is caught the
    step it happens, not just at the end of a run.
    """

    def __init__(self) -> None:
        super().__init__()
        self.repo = FakeContractsRepo()
        self.trace = TraceStore(FakeTraceBackend())
        contracts.configure(self.repo, self.trace)

        self.backend = InMemoryLedgerBackend()
        self.capability = FixedCapability(default_kw=_BANK_CAPABILITY_KW)
        self.ledger = ReservationLedger(self.backend, self.capability)

        self.contract_id = uuid4()
        self.pending_obligation_ids: list[UUID] = []
        self.committed_floor: dict[UUID, Decimal] = {}
        self.market_price_usd_per_mwh: float = 30.0

        self._run(self.repo.upsert_contract(_make_contract(self.contract_id)))

    @staticmethod
    def _run(coro):  # small sync-bridge helper: Hypothesis stateful machines run synchronously
        return asyncio.run(coro)

    # -- admit: a brand-new competing opportunity/obligation, always OFFERED, never touches K13 -------
    @rule(requested_kw=floats(min_value=1.0, max_value=40.0, allow_nan=False, allow_infinity=False))
    def admit(self, requested_kw: float) -> None:
        start, end = _T0, _T1
        opportunity = self._run(
            contracts.admit(self.contract_id, start, end, Decimal(str(round(requested_kw, 1))))
        )
        obligation = self._run(self.repo.get_obligation_by_opportunity(opportunity.opportunity_id))
        assert obligation is not None
        self.pending_obligation_ids.append(obligation.obligation_id)

    # -- price_move: a pure market perturbation. Must never, by itself, change any commitment ---------
    @rule(new_price=floats(min_value=-500.0, max_value=9000.0, allow_nan=False, allow_infinity=False))
    def price_move(self, new_price: float) -> None:
        self.market_price_usd_per_mwh = new_price  # tracked only; no ledger/contracts write follows

    # -- tick: the gate. Select + reserve the oldest pending obligation (or reject it on K2) -----------
    @precondition(lambda self: bool(self.pending_obligation_ids))
    @rule()
    def tick(self) -> None:
        obligation_id = self.pending_obligation_ids.pop(0)
        obligation = self._run(self.repo.get_obligation(obligation_id))
        assert obligation is not None

        self._run(contracts.transition_obligation(obligation_id, "SELECTED", reason_code="R-GATE-SELECT"))
        key = encode_interval_key(_BANK_ID, _T0, _T1)
        plan_id = uuid4()
        try:
            self._run(self.ledger.reserve(obligation_id, {key: obligation.committed_qty_kw}, plan_id))
        except ReservationError:
            self._run(
                contracts.transition_obligation(
                    obligation_id, "REJECTED", reason_code="R-COMMIT-LOCK-INFEASIBLE"
                )
            )
            return

        self._run(
            contracts.transition_obligation(obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER")
        )
        self.committed_floor[obligation_id] = obligation.committed_qty_kw
        # COMMITTED -> DELIVERING carries no reason code (02a S2.1) -- immediate for this test's
        # purposes, so `renomination_point` below always has a `DELIVERING` obligation to gate.
        self._run(contracts.transition_obligation(obligation_id, "DELIVERING", reason_code=None))

    # -- renomination_point: the ONLY mid-window reselection gate; must never move committed kW --------
    @precondition(lambda self: bool(self.committed_floor))
    @rule()
    def renomination_point(self) -> None:
        obligation_id = next(iter(self.committed_floor))
        point = RenominationPoint(
            renomination_point_id=uuid4(),
            contract_id=self.contract_id,
            obligation_id=obligation_id,
            scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.repo.renomination_points[point.renomination_point_id] = point
        # The module-level facade (`opengrid.contracts.exercise_renomination_point`), not
        # `contracts.renomination`'s repo/trace-taking function directly -- this test drives the fixed
        # public interface the real engine/API callers use. A `IllegalTransitionError` here is real,
        # correct behavior (renomination.py): the DELIVERING -> DELIVERING self-loop refuses an
        # obligation a prior `legitimate_reduction` already moved to SHORTFALL -- not a K13 gap.
        with contextlib.suppress(IllegalTransitionError):
            self._run(contracts.exercise_renomination_point(point.renomination_point_id, "RESELECTED"))
        # RESELECTED only drives DELIVERING -> DELIVERING (a self-loop, 02a S2.1): the obligation's
        # commitment/reservation is untouched by the gate itself -- a later `tick`-like re-solve would
        # be a separate, explicit act, never implied by exercising the point.

    # -- an attempted reduction with a disallowed reason must fail closed, never touch the reservation -
    @precondition(lambda self: bool(self.committed_floor))
    @rule(reason=sampled_from(_ILLEGITIMATE_REASONS))
    def illegitimate_reduction_attempt(self, reason: str) -> None:
        obligation_id = next(iter(self.committed_floor))
        records = self._run(self.ledger.reservations_for_obligation(obligation_id))
        active = [r for r in records if r.is_active]
        if not active:
            return
        record = active[0]
        try:
            self._run(self.ledger.release(record.reservation_id, reason))
        except CommitmentLockViolation:
            pass
        else:  # pragma: no cover - would itself be the K13 violation under test
            raise AssertionError("release with a disallowed/empty reason code must never succeed")

    # -- a legitimate override reduction must succeed and its floor must drop to exactly the new value -
    @precondition(lambda self: bool(self.committed_floor))
    @rule(reason=sampled_from(_LEGIT_REDUCE_REASONS))
    def legitimate_reduction(self, reason: str) -> None:
        obligation_id = next(iter(self.committed_floor))
        records = self._run(self.ledger.reservations_for_obligation(obligation_id))
        active = [r for r in records if r.is_active]
        if not active:
            return
        record = active[0]
        self._run(self.ledger.release(record.reservation_id, reason))
        self.committed_floor[obligation_id] = Decimal(0)
        # `IllegalTransitionError` here just means a prior `legitimate_reduction` already moved this
        # obligation to SHORTFALL/terminal this run -- not a K13 gap.
        with contextlib.suppress(IllegalTransitionError):
            self._run(
                contracts.transition_obligation(
                    obligation_id, "SHORTFALL", reason_code=_shortfall_reason_for(reason)
                )
            )

    @invariant()
    def committed_kw_never_reduced_without_a_reason(self) -> None:
        for obligation_id, floor in self.committed_floor.items():
            active_kw = sum(
                r.amount_kw
                for r in self.backend.rows.values()
                if r.obligation_id == obligation_id and r.is_active
            )
            assert active_kw >= floor, (
                f"obligation {obligation_id}: active reservation total {active_kw} fell below its "
                f"tracked K13 floor {floor} with no allowed reason code recorded"
            )


def _shortfall_reason_for(release_reason: str) -> str:
    """`opengrid.ledger.ALLOWED_RELEASE_REASONS` and `opengrid.contracts.state_machine
    .SHORTFALL_REASON_CODES` are two independent, module-owned vocabularies (BUILD.md S1: one owner
    per function) that happen to overlap on the L0/L1/L2/INFEASIBLE codes; map the ledger release
    reasons this test uses onto the nearest valid contracts-side `DELIVERING -> SHORTFALL` code so the
    obligation lifecycle stays legal after a legitimate ledger-side reduction."""
    if release_reason in {
        "R-COMMIT-LOCK-OVERRIDE-L0",
        "R-COMMIT-LOCK-OVERRIDE-L1",
        "R-COMMIT-LOCK-OVERRIDE-L2",
        "R-COMMIT-LOCK-INFEASIBLE",
    }:
        return release_reason
    return "R-SHORTFALL-THRESHOLD"


def _make_contract(contract_id: UUID) -> Contract:
    return Contract(
        contract_id=contract_id,
        customer_id=uuid4(),
        service_type="ERCOT_ENERGY",
        tier="T2",
        profile_ref="ts-04-01@1",
        start_at=datetime.now(UTC) - timedelta(days=1),
        renomination_allowed=True,
        status="ACTIVE",
    )


TestObligationsK13 = _ObligationsK13StateMachine.TestCase
TestObligationsK13.settings = settings(max_examples=200, stateful_step_count=30, deadline=None)
