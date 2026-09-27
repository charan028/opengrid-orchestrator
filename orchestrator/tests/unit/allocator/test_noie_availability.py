"""D-37: the allocator dispatches nothing on an UNAVAILABLE bank (regulated, no contract) -- no headroom, no
obligation -- except a K13-grandfathered call, which completes untouched (also exempt from K15)."""

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
from opengrid.core.reasons import R_BANK_UNAVAILABLE
from opengrid.market.territory import FREE, MarketRef

T = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


def _fleet(available: bool = False) -> FleetState:
    hub = HubSnapshot(
        hub_id="hub-lcra", bank_id="bank-067", free_discharge_kw=50.0, soc_kwh=1e6, reserve_kwh=0.0, e_kwh=1e6
    )
    bank = BankSnapshot(
        bank_id="bank-067", capability_kw=50.0, kva_rating=50.0, territory="RAYBURN", available=available
    )
    return FleetState(hubs=(hub,), banks=(bank,))


def _call(oid: str, market_ref: MarketRef | None, *, grandfathered: bool = False) -> ObligationCall:
    return ObligationCall(
        obligation_id=oid,
        bank_id="bank-067",
        service_type="PARTNER_CAPACITY",
        tier="T1",
        committed_kw=20.0,
        eligible_hub_ids=("hub-lcra",),
        market_ref=market_ref,
        grandfathered=grandfathered,
    )


def _priced() -> Schedule:
    return Schedule(prices=(PriceSignal("bank-067", 900.0, threshold_usd_per_mwh=10.0),))


def test_no_headroom_on_an_unavailable_bank() -> None:
    result = cycle(T, _fleet(), LedgerView(calls=()), _priced(), {}, (), enforce_territory=True)
    assert [g for g in result.grants if g.is_headroom] == []
    assert any(
        b.reason_code == R_BANK_UNAVAILABLE and b.obligation_id is None for b in result.territory_blocks
    )


def test_no_headroom_even_with_territory_enforcement_off() -> None:
    result = cycle(T, _fleet(), LedgerView(calls=()), _priced(), {}, (), enforce_territory=False)
    assert [g for g in result.grants if g.is_headroom] == []


def test_a_utility_obligation_is_not_served_on_an_unavailable_bank() -> None:
    calls = (_call("toll", MarketRef("REGULATED", "RAYBURN")),)
    result = cycle(T, _fleet(), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True)
    assert [g.granted_kw for g in result.grants if g.obligation_id == "toll"] == [0.0]
    assert [b.reason_code for b in result.territory_blocks if b.obligation_id == "toll"] == [
        R_BANK_UNAVAILABLE
    ]


def test_a_grandfathered_ercot_obligation_completes_untouched() -> None:
    """K13: committed while RAYBN was ERCOT competitive; it is neither blocked by K15 nor by availability."""
    calls = (_call("ecrs-old", FREE, grandfathered=True),)
    result = cycle(T, _fleet(), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True)
    assert [g.granted_kw for g in result.grants if g.obligation_id == "ecrs-old"] == [20.0]
    assert [b for b in result.territory_blocks if b.obligation_id == "ecrs-old"] == []
    assert [s for s in result.shortfalls if s.obligation_id == "ecrs-old"] == []


def test_a_new_ercot_obligation_on_the_same_bank_is_blocked() -> None:
    calls = (_call("ecrs-new", FREE),)
    result = cycle(T, _fleet(), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True)
    assert [g.granted_kw for g in result.grants if g.obligation_id == "ecrs-new"] == [0.0]


def test_an_available_regulated_bank_still_serves_its_utility() -> None:
    """Flip to AVAILABLE (a real contract): the utility's own obligation is served again."""
    calls = (_call("toll", MarketRef("REGULATED", "RAYBURN")),)
    result = cycle(
        T, _fleet(available=True), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True
    )
    assert [g.granted_kw for g in result.grants if g.obligation_id == "toll"] == [20.0]
