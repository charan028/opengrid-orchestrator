"""TS-05: the thin adapter (`run_cycle`/`substitute_hub`) against fake fleet/ledger gateways
(BUILD.md S5's "use fakes for the ledger and fleet" -- no DB/MQTT here).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opengrid.allocator import reasons, run_cycle, substitute_hub
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    ProposedGrant,
    Schedule,
)

_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class FakeFleetGateway:
    def __init__(self, fleet_state: FleetState, bank_ids: tuple[str, ...]) -> None:
        self._fleet_state = fleet_state
        self._bank_ids = bank_ids

    async def bank_ids(self):
        return self._bank_ids

    async def fleet_state(self, bank_ids, interval_start):
        return self._fleet_state


class FakeLedgerGateway:
    def __init__(self, ledger_view: LedgerView) -> None:
        self._ledger_view = ledger_view
        self.persisted: list[ProposedGrant] = []
        self.substitutions: list[tuple[str, str, str, str]] = []
        self.version = 7

    async def ledger_view(self, bank_ids, interval_start):
        return self._ledger_view

    async def ledger_version(self):
        return self.version

    async def persist_grants(self, cycle_id, grants):
        self.persisted.extend(grants)

    async def record_substitution(self, obligation_id, from_hub_id, to_hub_id, reason_code):
        self.substitutions.append((obligation_id, from_hub_id, to_hub_id, reason_code))


@pytest.mark.asyncio
async def test_ts_05_60_run_cycle_without_gateways_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        await run_cycle("cycle-1")


@pytest.mark.asyncio
async def test_ts_05_61_run_cycle_builds_grant_rows_from_fakes() -> None:
    fleet_state = FleetState(
        hubs=(HubSnapshot(hub_id="h1", bank_id="b1", free_discharge_kw=50.0),),
        banks=(BankSnapshot(bank_id="b1", capability_kw=50.0, kva_rating=50.0),),
    )
    ledger_view = LedgerView(
        calls=(
            ObligationCall(
                obligation_id="o1",
                bank_id="b1",
                service_type="DIST_DEFERRAL",
                tier="T1",
                committed_kw=20.0,
                eligible_hub_ids=("h1",),
            ),
        )
    )
    fleet = FakeFleetGateway(fleet_state, ("b1",))
    ledger = FakeLedgerGateway(ledger_view)

    grants = await run_cycle("cycle-1", fleet=fleet, ledger=ledger, now=_T0)

    assert len(grants) == 1
    assert grants[0].cycle_id == "cycle-1"
    assert grants[0].ledger_version == 7
    assert float(grants[0].granted_kw) == 20.0
    assert len(ledger.persisted) == 1


@pytest.mark.asyncio
async def test_ts_05_62_substitute_hub_rejects_wrong_reason_code() -> None:
    with pytest.raises(ValueError, match="R-SUBSTITUTION"):
        await substitute_hub("o1", "h1", "h2", "R-NOT-A-VALID-REASON")


@pytest.mark.asyncio
async def test_ts_05_63_substitute_hub_rejects_same_hub() -> None:
    with pytest.raises(ValueError, match="from_hub_id"):
        await substitute_hub("o1", "h1", "h1", reasons.R_SUBSTITUTION)


@pytest.mark.asyncio
async def test_ts_05_64_substitute_hub_without_ledger_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        await substitute_hub("o1", "h1", "h2", reasons.R_SUBSTITUTION)


@pytest.mark.asyncio
async def test_ts_05_65_substitute_hub_records_via_ledger_gateway() -> None:
    from opengrid.allocator import _substitute_hub

    ledger = FakeLedgerGateway(LedgerView(calls=()))
    await _substitute_hub("o1", "h1", "h2", reasons.R_SUBSTITUTION, ledger=ledger)
    assert ledger.substitutions == [("o1", "h1", "h2", reasons.R_SUBSTITUTION)]


def test_ts_05_66_schedule_import_still_usable() -> None:
    # Sanity check that the adapter's default Schedule() fallback constructs cleanly.
    assert Schedule().prices == ()
