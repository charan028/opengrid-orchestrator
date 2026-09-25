"""Lifecycle tests: `transition_obligation` validates, persists with optimistic-lock CAS, and
traces every transition (K10, K13). Illegal transitions raise and change nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.contracts.errors import ConcurrentUpdateError, IllegalTransitionError
from opengrid.core.models.engine import Obligation

from .fakes import FakeContractsRepo, FakeTraceBackend


def _obligation(**overrides: object) -> Obligation:
    now = datetime.now(UTC)
    base: dict[str, object] = dict(
        obligation_id=uuid4(),
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        service_type="ERCOT_ENERGY",
        tier="T2",
        window_start=now,
        window_end=now + timedelta(minutes=15),
        committed_qty_kw=Decimal("10"),
        state="OFFERED",
        version=1,
    )
    base.update(overrides)
    return Obligation(**base)  # type: ignore[arg-type]


async def test_legal_transition_persists_and_traces(
    repo: FakeContractsRepo, trace_backend: FakeTraceBackend
) -> None:
    obligation = _obligation()
    repo.obligations[obligation.obligation_id] = obligation

    updated = await contracts.transition_obligation(
        obligation.obligation_id, "SELECTED", reason_code="R-GATE-SELECT"
    )

    assert updated.state == "SELECTED"
    assert updated.version == 2
    assert any(
        r["stream_id"] == f"obligation-{obligation.obligation_id}" and r["reason_codes"] == ["R-GATE-SELECT"]
        for r in trace_backend.rows
    )


async def test_illegal_transition_raises_and_changes_nothing(repo: FakeContractsRepo) -> None:
    obligation = _obligation(state="OFFERED")
    repo.obligations[obligation.obligation_id] = obligation

    with pytest.raises(IllegalTransitionError):
        await contracts.transition_obligation(
            obligation.obligation_id, "COMMITTED", reason_code="R-COMMIT-LOCK-ENTER"
        )

    assert repo.obligations[obligation.obligation_id].state == "OFFERED"
    assert repo.obligations[obligation.obligation_id].version == 1


async def test_committed_obligation_cannot_be_rejected_or_expired(repo: FakeContractsRepo) -> None:
    """K13: once COMMITTED, only DELIVERING (and from there FULFILLED/SHORTFALL-with-reason) is
    reachable -- never REJECTED/EXPIRED, regardless of who calls transition_obligation."""
    obligation = _obligation(state="COMMITTED", version=3)
    repo.obligations[obligation.obligation_id] = obligation

    for target, reason in (("REJECTED", "R-ADMIT-REJECT"), ("EXPIRED", "R-EXPIRED-UNSELECTED")):
        with pytest.raises(IllegalTransitionError):
            await contracts.transition_obligation(obligation.obligation_id, target, reason_code=reason)  # type: ignore[arg-type]
    assert repo.obligations[obligation.obligation_id].state == "COMMITTED"


async def test_delivering_shortfall_requires_an_allowed_reason_code(repo: FakeContractsRepo) -> None:
    obligation = _obligation(state="DELIVERING", version=4)
    repo.obligations[obligation.obligation_id] = obligation

    with pytest.raises(IllegalTransitionError):
        await contracts.transition_obligation(
            obligation.obligation_id, "SHORTFALL", reason_code="R-BETTER-PRICE"
        )

    updated = await contracts.transition_obligation(
        obligation.obligation_id, "SHORTFALL", reason_code="R-COMMIT-LOCK-OVERRIDE-L1"
    )
    assert updated.state == "SHORTFALL"


async def test_unknown_obligation_raises_lookup_error(repo: FakeContractsRepo) -> None:
    with pytest.raises(LookupError):
        await contracts.transition_obligation(uuid4(), "SELECTED", reason_code="R-GATE-SELECT")


async def test_stale_version_raises_concurrent_update_error(repo: FakeContractsRepo) -> None:
    obligation = _obligation(state="OFFERED", version=5)
    repo.obligations[obligation.obligation_id] = obligation

    # Simulate a concurrent writer bumping the version between read and write by racing the CAS
    # directly against the repo (transition_obligation always reads current first, so provoke the
    # race at the repo layer the way two concurrent gate runs would).
    await repo.update_obligation_state(
        obligation.obligation_id, to_state="SELECTED", reason_code="R-GATE-SELECT", expected_version=5
    )
    with pytest.raises(ConcurrentUpdateError):
        await repo.update_obligation_state(
            obligation.obligation_id, to_state="SELECTED", reason_code="R-GATE-SELECT", expected_version=5
        )


async def test_expire_unselected_sweeps_offered_past_window_start(
    repo: FakeContractsRepo, trace_backend: FakeTraceBackend
) -> None:
    from opengrid.core.models.engine import Opportunity

    past = datetime.now(UTC) - timedelta(minutes=5)
    obligation = _obligation(state="OFFERED", window_start=past)
    opportunity = Opportunity(
        opportunity_id=obligation.opportunity_id,
        contract_id=obligation.contract_id,
        window_start=past,
        window_end=past + timedelta(minutes=15),
        requested_kw=Decimal("10"),
        state="OFFERED",
        admitted_at=past,
    )
    repo.obligations[obligation.obligation_id] = obligation
    repo.opportunities[opportunity.opportunity_id] = opportunity

    expired = await contracts.expire_unselected()

    assert expired == [obligation.obligation_id]
    assert repo.obligations[obligation.obligation_id].state == "EXPIRED"
    assert repo.opportunities[opportunity.opportunity_id].state == "EXPIRED"
