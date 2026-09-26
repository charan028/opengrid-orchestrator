"""ERCOT_AS is a CAPACITY HOLD, not a continuous discharge (lead decision 2026-09-26, NPRR1282).

Live 2026-09-26: every ERCOT_AS award was dispatched as a 500 kW continuous discharge for hours, draining
bank-000/001 to their reserve (G-01-ENERGY partial vetoes, SHORTFALL, K2 flags). Undeployed, an AS award
now gets 0 kW with its capacity kept locked (never exported as headroom); deployed, it discharges up to
its committed kW."""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    PriceSignal,
    Schedule,
)

T = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)
AS_ID = "8e1cdcde-0000-4000-8000-000000000001"
FIRM_ID = "8e1cdcde-0000-4000-8000-000000000002"


def _fleet(bank_id: str = "b1") -> FleetState:
    hubs = tuple(
        HubSnapshot(
            hub_id=f"h{i}",
            bank_id=bank_id,
            free_discharge_kw=10.0,
            health="OK",
            soc_kwh=1e6,
            reserve_kwh=0.0,
            e_kwh=1e6,
        )
        for i in range(10)
    )
    return FleetState(
        hubs=hubs, banks=(BankSnapshot(bank_id=bank_id, capability_kw=100.0, kva_rating=100.0),)
    )


def _as_call(*, deployed: bool, kw: float = 40.0) -> ObligationCall:
    return ObligationCall(
        obligation_id=AS_ID,
        bank_id="b1",
        service_type="ERCOT_AS",
        tier="T2",
        committed_kw=kw,
        eligible_hub_ids=tuple(f"h{i}" for i in range(10)),
        as_deployed=deployed,
    )


# A high price: an unheld bank would export its free headroom.
_PRICED = Schedule(prices=(PriceSignal(bank_id="b1", price_usd_per_mwh=500.0, threshold_usd_per_mwh=80.0),))


def _hold_tight_fleet() -> FleetState:
    """Stored energy above reserve exactly covers the 40 kW x 1 h hold / eta_d plus the 1 % margin."""
    per_hub_above_reserve = (40.0 * 1.0 / 0.9487 + 0.01 * 39.2 * 10) / 10
    hubs = tuple(
        HubSnapshot(
            hub_id=f"h{i}",
            bank_id="b1",
            free_discharge_kw=10.0,
            health="OK",
            soc_kwh=7.84 + per_hub_above_reserve,
            reserve_kwh=7.84,
            e_kwh=39.2,
        )
        for i in range(10)
    )
    return FleetState(hubs=hubs, banks=(BankSnapshot(bank_id="b1", capability_kw=100.0, kva_rating=100.0),))


def test_an_undeployed_as_award_is_held_at_zero_with_no_shortfall_and_no_headroom_export():
    result = cycle(T, _hold_tight_fleet(), LedgerView(calls=(_as_call(deployed=False),)), _PRICED, {}, ())

    assert result.held == (AS_ID,)
    # Present at 0 kW with R-GRANT-AS-HOLD: the guardian's G-19 needs the hold's reason in the batch.
    (hold,) = [g for g in result.grants if g.obligation_id == AS_ID]
    assert hold.granted_kw == 0.0 and hold.reason_code == "R-GRANT-AS-HOLD"
    assert result.shortfalls == ()  # holding IS the service, nothing is short
    # The locked capacity and the held energy are never exported as headroom.
    assert [g for g in result.grants if g.is_headroom] == []


def test_a_deployed_as_award_discharges_its_committed_kw():
    result = cycle(T, _fleet(), LedgerView(calls=(_as_call(deployed=True),)), Schedule(), {}, ())

    assert result.held == ()
    (grant,) = [g for g in result.grants if g.obligation_id == AS_ID]
    assert grant.granted_kw == 40.0


def test_other_services_are_unchanged_by_the_hold_rule():
    firm = ObligationCall(
        obligation_id=FIRM_ID,
        bank_id="b1",
        service_type="PARTNER_CAPACITY",
        tier="T3",
        committed_kw=30.0,
        eligible_hub_ids=tuple(f"h{i}" for i in range(10)),
    )
    result = cycle(T, _fleet(), LedgerView(calls=(firm,)), Schedule(), {}, ())
    (grant,) = [g for g in result.grants if g.obligation_id == FIRM_ID]
    assert grant.granted_kw == 30.0
    assert result.held == ()


def test_a_conservative_bank_takes_no_headroom_but_keeps_committed_dispatch():
    """K7 escalation (og.scope_posture CONSERVATIVE): no new uncommitted/market dispatch on the bank;
    committed obligations continue."""
    firm = ObligationCall(
        obligation_id=FIRM_ID,
        bank_id="b1",
        service_type="PARTNER_CAPACITY",
        tier="T3",
        committed_kw=30.0,
        eligible_hub_ids=tuple(f"h{i}" for i in range(10)),
    )
    normal = cycle(T, _fleet(), LedgerView(calls=(firm,)), _PRICED, {}, ())
    assert [g for g in normal.grants if g.is_headroom]  # precondition: this price exports headroom

    conservative = Schedule(prices=_PRICED.prices, conservative_bank_ids=frozenset({"b1"}))
    result = cycle(T, _fleet(), LedgerView(calls=(firm,)), conservative, {}, ())
    assert [g for g in result.grants if g.is_headroom] == []
    (grant,) = [g for g in result.grants if g.obligation_id == FIRM_ID]
    assert grant.granted_kw == 30.0
