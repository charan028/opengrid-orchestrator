"""Re-nomination tests (02a S1.7/S2.1, ES04-S04, TS-04-14): reselection is only possible at a
contract's declared, due, unexercised re-nomination point -- never in between, and never for any
other obligation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.contracts.errors import RenominationError
from opengrid.core.models.engine import Obligation, RenominationPoint

from .fakes import FakeContractsRepo, FakeTraceBackend


def _delivering_obligation(**overrides: object) -> Obligation:
    now = datetime.now(UTC)
    base: dict[str, object] = dict(
        obligation_id=uuid4(),
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        service_type="PARTNER_CAPACITY",
        tier="T3",
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=5),
        committed_qty_kw=Decimal("50"),
        state="DELIVERING",
        version=7,
    )
    base.update(overrides)
    return Obligation(**base)  # type: ignore[arg-type]


async def test_due_point_allows_reselection(repo: FakeContractsRepo, trace_backend: FakeTraceBackend) -> None:
    obligation = _delivering_obligation()
    repo.obligations[obligation.obligation_id] = obligation
    point = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=obligation.contract_id,
        obligation_id=obligation.obligation_id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    repo.renomination_points[point.renomination_point_id] = point

    updated = await contracts.exercise_renomination_point(point.renomination_point_id, "RESELECTED")

    assert updated.outcome == "RESELECTED"
    assert updated.exercised_at is not None
    assert repo.obligations[obligation.obligation_id].state == "DELIVERING"
    assert repo.obligations[obligation.obligation_id].version == obligation.version + 1
    assert any(r["event_class"] == "RENOMINATION" for r in trace_backend.rows)


async def test_confirmed_outcome_does_not_touch_the_obligation(repo: FakeContractsRepo) -> None:
    obligation = _delivering_obligation()
    repo.obligations[obligation.obligation_id] = obligation
    point = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=obligation.contract_id,
        obligation_id=obligation.obligation_id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    repo.renomination_points[point.renomination_point_id] = point

    await contracts.exercise_renomination_point(point.renomination_point_id, "CONFIRMED")

    assert repo.obligations[obligation.obligation_id].version == obligation.version


async def test_already_exercised_point_cannot_be_exercised_again(repo: FakeContractsRepo) -> None:
    obligation = _delivering_obligation()
    repo.obligations[obligation.obligation_id] = obligation
    point = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=obligation.contract_id,
        obligation_id=obligation.obligation_id,
        scheduled_at=datetime.now(UTC) - timedelta(days=1),
        exercised_at=datetime.now(UTC) - timedelta(hours=1),
        outcome="CONFIRMED",
    )
    repo.renomination_points[point.renomination_point_id] = point

    with pytest.raises(RenominationError):
        await contracts.exercise_renomination_point(point.renomination_point_id, "RESELECTED")


async def test_unknown_point_raises(repo: FakeContractsRepo) -> None:
    with pytest.raises(RenominationError):
        await contracts.exercise_renomination_point(uuid4(), "RESELECTED")


async def test_due_renomination_points_only_returns_due_unexercised_points(repo: FakeContractsRepo) -> None:
    now = datetime.now(UTC)
    due = RenominationPoint(
        renomination_point_id=uuid4(), contract_id=uuid4(), scheduled_at=now - timedelta(minutes=1)
    )
    not_yet_due = RenominationPoint(
        renomination_point_id=uuid4(), contract_id=uuid4(), scheduled_at=now + timedelta(hours=1)
    )
    already_done = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=uuid4(),
        scheduled_at=now - timedelta(hours=1),
        exercised_at=now - timedelta(minutes=30),
        outcome="SKIPPED",
    )
    for point in (due, not_yet_due, already_done):
        repo.renomination_points[point.renomination_point_id] = point

    result = await contracts.due_renomination_points(as_of=now)

    assert [p.renomination_point_id for p in result] == [due.renomination_point_id]


async def test_other_obligations_are_unaffected_by_a_renomination(repo: FakeContractsRepo) -> None:
    """K13: exercising one contract's re-nomination point must never touch any other obligation."""
    renominating = _delivering_obligation()
    bystander = _delivering_obligation()
    repo.obligations[renominating.obligation_id] = renominating
    repo.obligations[bystander.obligation_id] = bystander
    point = RenominationPoint(
        renomination_point_id=uuid4(),
        contract_id=renominating.contract_id,
        obligation_id=renominating.obligation_id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    repo.renomination_points[point.renomination_point_id] = point

    await contracts.exercise_renomination_point(point.renomination_point_id, "RESELECTED")

    assert repo.obligations[bystander.obligation_id] == bystander
