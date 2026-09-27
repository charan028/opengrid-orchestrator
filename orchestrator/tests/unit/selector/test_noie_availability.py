"""D-37: the selector offers and plans nothing on UNAVAILABLE banks (no ERCOT via K15, no utility obligation,
no charging), without cancelling or re-planning committed obligations that are K13-grandfathered there."""

from __future__ import annotations

import dataclasses

from opengrid.market import load_market_model
from opengrid.selector.gate import GateAvailability, apply_availability, prepare_obligations
from opengrid.selector.types import BankSnapshot
from unit.selector.factories import binary_candidate, committed

from ..market.test_noie_switch import TARIFFS

INTERVALS = range(4)
BANKS = ("bank-000", "bank-050", "bank-067")
ZONES = [("bank-000", "LZ_NORTH"), ("bank-050", "LZ_LCRA"), ("bank-067", "LZ_RAYBN")]
AVAIL = GateAvailability(
    unavailable=frozenset({"bank-050", "bank-067"}),
    grandfathered={"ecrs-old": frozenset({"bank-067"})},
)


def _banks() -> tuple[BankSnapshot, ...]:
    return tuple(
        BankSnapshot(
            bank_id=b,
            max_discharge_kw=dict.fromkeys(INTERVALS, 600.0),
            max_charge_kw=dict.fromkeys(INTERVALS, 600.0),
            solar_charge_kw=dict.fromkeys(INTERVALS, 100.0),
        )
        for b in BANKS
    )


def _run():
    market = load_market_model(banks=ZONES, config_path=TARIFFS)
    candidates = (
        binary_candidate("ercot-new", 100.0, 50.0, (0, 1), BANKS),
        binary_candidate("toll-lcra", 100.0, 50.0, (0, 1), BANKS),
    )
    committed_obs = (
        committed("ecrs-old", {0: 500.0}, BANKS),
        committed("ecrs-other", {0: 100.0}, BANKS),
    )
    cands, comm = prepare_obligations(candidates, committed_obs, market)
    return apply_availability(_banks(), cands, comm, AVAIL, BANKS)


def test_nothing_new_is_offered_on_an_unavailable_bank() -> None:
    _banks_out, candidates, _committed = _run()
    for c in candidates:
        assert not set(c.eligible_bank_ids) & {"bank-050", "bank-067"}


def test_a_utility_candidate_for_lcra_gets_no_bank_while_unavailable() -> None:
    market = load_market_model(banks=ZONES, config_path=TARIFFS)
    toll = binary_candidate("toll-lcra", 100.0, 50.0, (0, 1), BANKS)
    toll = dataclasses.replace(toll, market="REGULATED", utility_id="LCRA")

    (prepared,), _ = prepare_obligations((toll,), (), market)
    assert prepared.eligible_bank_ids == ("bank-050",)  # K15 alone would allow its own territory
    _b, (after,), _c = apply_availability(_banks(), (prepared,), (), AVAIL, BANKS)
    assert after.eligible_bank_ids == ()  # but the bank is unavailable: no contract


def test_grandfathered_obligation_keeps_exactly_its_own_unavailable_bank() -> None:
    _b, _c, comm = _run()
    by_id = {c.obligation_id: c for c in comm}
    assert set(by_id["ecrs-old"].eligible_bank_ids) == {"bank-000", "bank-067"}  # not cancelled, not moved
    assert set(by_id["ecrs-other"].eligible_bank_ids) == {"bank-000"}  # never newly placed there
    assert by_id["ecrs-old"].committed_kw_by_interval == {0: 500.0}  # K13: frozen kW untouched


def test_unavailable_banks_are_an_idle_hold() -> None:
    banks, _c, _o = _run()
    by_id = {b.bank_id: b for b in banks}
    lcra, raybn, north = by_id["bank-050"], by_id["bank-067"], by_id["bank-000"]
    for bank in (lcra, raybn):
        assert not bank.free_market_access
        assert set(bank.max_charge_kw.values()) == {0.0}
        assert bank.solar_charge_kw == {} and bank.grid_charge_intervals == frozenset()
    assert set(lcra.max_discharge_kw.values()) == {0.0}  # nothing grandfathered there
    assert set(raybn.max_discharge_kw.values()) == {600.0}  # the grandfathered ECRS still delivers
    assert north == _banks()[0]  # an available bank is untouched


def test_no_unavailable_bank_changes_nothing() -> None:
    banks = _banks()
    cands = (binary_candidate("x", 1.0, 1.0, (0,), BANKS),)
    assert apply_availability(banks, cands, (), GateAvailability(), BANKS) == (banks, cands, ())
