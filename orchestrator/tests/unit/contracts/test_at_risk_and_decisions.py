"""`set_obligation_at_risk` (K1 energy check, lead finding 4) and `record_opportunity_decision`
(og.opportunity.state/gate_id never left OFFERED/NULL, lead review 2026-09-26)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

import opengrid.contracts as contracts
from opengrid.core.models.engine import Obligation, Opportunity

from .fakes import FakeContractsRepo, FakeTraceBackend

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def _obligation(state: str = "DELIVERING") -> Obligation:
    return Obligation(
        obligation_id=uuid4(),
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        service_type="PARTNER_CAPACITY",
        tier="T3",
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
        committed_qty_kw=Decimal("50"),
        state=state,  # type: ignore[arg-type]
        version=3,
    )


async def test_at_risk_is_set_and_cleared_without_a_state_change(
    repo: FakeContractsRepo, trace_backend: FakeTraceBackend
) -> None:
    obligation = _obligation()
    repo.obligations[obligation.obligation_id] = obligation

    flagged = await contracts.set_obligation_at_risk(
        obligation.obligation_id, True, reason_code="ALR-ENERGY-SHORTFALL-RISK", payload={"margin_kwh": -3.0}
    )
    assert (flagged.at_risk, flagged.state, flagged.version) == (True, "DELIVERING", 3)

    cleared = await contracts.set_obligation_at_risk(
        obligation.obligation_id, False, reason_code="ALR-ENERGY-SHORTFALL-RISK"
    )
    assert cleared.at_risk is False

    classes = [
        r["event_class"]
        for r in trace_backend.rows
        if r["stream_id"] == f"obligation-{obligation.obligation_id}"
    ]
    assert classes == ["AT_RISK", "AT_RISK_CLEARED"]


async def test_at_risk_no_op_is_not_rewritten_or_traced(
    repo: FakeContractsRepo, trace_backend: FakeTraceBackend
) -> None:
    obligation = _obligation()
    repo.obligations[obligation.obligation_id] = obligation

    await contracts.set_obligation_at_risk(
        obligation.obligation_id, False, reason_code="ALR-ENERGY-SHORTFALL-RISK"
    )

    assert trace_backend.rows == []


async def test_at_risk_unknown_obligation_raises() -> None:
    with pytest.raises(LookupError):
        await contracts.set_obligation_at_risk(uuid4(), True, reason_code="ALR-ENERGY-SHORTFALL-RISK")


async def test_opportunity_decision_records_state_reason_and_gate(repo: FakeContractsRepo) -> None:
    opportunity = Opportunity(
        opportunity_id=uuid4(),
        contract_id=uuid4(),
        window_start=NOW,
        window_end=NOW + timedelta(hours=1),
        requested_kw=Decimal("50"),
        state="OFFERED",
        admitted_at=NOW,
    )
    repo.opportunities[opportunity.opportunity_id] = opportunity
    plan_id = uuid4()

    decided = await contracts.record_opportunity_decision(
        opportunity.opportunity_id, "SELECTED", reason_code="R-GATE-SELECT", gate_id=plan_id, decided_at=NOW
    )

    assert (decided.state, decided.reason_code, decided.gate_id, decided.decided_at) == (
        "SELECTED",
        "R-GATE-SELECT",
        plan_id,
        NOW,
    )
