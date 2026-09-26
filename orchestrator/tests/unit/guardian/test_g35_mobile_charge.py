"""G-35 (D-31): a MOBILE_STORAGE unit is charged only at its home station -- never from the fleet or at a
deployment site. Any charging setpoint on a mobile unit away from home, or whose location is unknown, is
vetoed at item level (R-MOBILE-CHARGE-AWAY-FROM-HOME-STATION)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from opengrid.core import reasons
from opengrid.guardian import checks
from opengrid.guardian.ports import ProposedItem
from opengrid.guardian.repo import ConfigMobileUnitPort
from opengrid.selector.gate import parse_mobile_home_stations

from .conftest import BANK_ID, make_batch_row, make_proposal, service_with, wire_default_passing_scenario


class _Mobile:
    def __init__(self, ids: set[str], at_home: bool | None) -> None:
        self.ids, self.at_home = ids, at_home

    def is_mobile(self, hub_or_bank_id: str) -> bool:
        return hub_or_bank_id in self.ids

    async def at_home_station(self, hub_id: str) -> bool | None:
        return self.at_home


@pytest.mark.parametrize(
    ("p_kw", "mobile", "at_home", "ok"),
    [
        (3.0, True, None, False),  # unknown location: fail closed
        (3.0, True, False, False),  # away from home
        (3.0, True, True, True),  # charging at its home station
        (-3.0, True, None, True),  # discharge (serving a deployment) is not G-35's concern
        (0.0, True, None, True),  # a 0 kW hold
        (3.0, False, None, True),  # not a mobile unit
    ],
)
def test_g35_check(p_kw, mobile, at_home, ok):
    outcome = checks.check_g35_mobile_charge("trailer-mb-01", p_kw, is_mobile=mobile, at_home_station=at_home)
    assert outcome.ok is ok
    if not ok:
        assert outcome.rule_id == "G-35"
        assert (
            outcome.reason
            == reasons.R_MOBILE_CHARGE_AWAY_FROM_HOME_STATION
            == ("R-MOBILE-CHARGE-AWAY-FROM-HOME-STATION")
        )


async def _verdict(fakes, config, seed, mobile: _Mobile, items: list[ProposedItem]):
    proposal = replace(make_proposal(), items=items)
    wire_default_passing_scenario(fakes, proposal)
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), mobile_units=mobile)
    return await service.evaluate_and_sign(make_batch_row(proposal))


async def test_charging_a_mobile_unit_away_from_home_is_vetoed_at_item_level(
    fakes, guardian_config, signing_seed
):
    verdict = await _verdict(
        fakes,
        guardian_config,
        signing_seed,
        _Mobile({"hub-0001"}, at_home=None),
        [ProposedItem("hub-0001", 3.0, "SELECTOR"), ProposedItem("hub-0002", 3.0, "SELECTOR")],
    )
    assert verdict.vetoed_rule_ids == ["G-35"] and verdict.outcome == "PARTLY_VETOED"  # item level
    payload = fakes.trace.appended[-1][1]
    assert payload["vetoed_hub_ids"] == ["hub-0001"]


async def test_a_mobile_unit_is_judged_on_its_net_setpoint(fakes, guardian_config, signing_seed):
    """Two items on the same unit summing to a discharge are not a charge."""
    verdict = await _verdict(
        fakes,
        guardian_config,
        signing_seed,
        _Mobile({"hub-0001"}, at_home=None),
        [ProposedItem("hub-0001", 2.0, "SELECTOR"), ProposedItem("hub-0001", -3.0, "SELECTOR")],
    )
    assert "G-35" not in verdict.vetoed_rule_ids


async def test_the_bank_id_of_a_single_hub_mobile_bank_identifies_it(fakes, guardian_config, signing_seed):
    verdict = await _verdict(
        fakes,
        guardian_config,
        signing_seed,
        _Mobile({BANK_ID}, at_home=False),  # the registry lists the unit by its bank id
        [ProposedItem("hub-0001", 3.0, "SELECTOR")],
    )
    assert "G-35" in verdict.vetoed_rule_ids


async def test_charging_at_home_and_fleet_hubs_pass(fakes, guardian_config, signing_seed):
    at_home = await _verdict(
        fakes,
        guardian_config,
        signing_seed,
        _Mobile({"hub-0001"}, at_home=True),
        [ProposedItem("hub-0001", 3.0, "S")],
    )
    assert "G-35" not in at_home.vetoed_rule_ids


async def test_the_config_registry_marks_assigned_units_mobile_with_an_unknown_location():
    raw = {
        "home_station": [{"home_station_id": "hs-1", "zone": "LZ_AEN"}],
        "assignment": [{"bank_id": "trailer-mb-01", "home_station_id": "hs-1"}],
    }
    port = ConfigMobileUnitPort(parse_mobile_home_stations(raw))
    assert port.is_mobile("trailer-mb-01") and not port.is_mobile("bank-000")
    assert await port.at_home_station("trailer-mb-01") is None  # no location source yet: G-35 fails closed
