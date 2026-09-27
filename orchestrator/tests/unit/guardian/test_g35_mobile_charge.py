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
    assert await port.at_home_station("trailer-mb-01") is None  # no location source: G-35 fails closed


# --- Trucks (owner request 2026-09-26): the at-home read from og.hub lat/lon vs the home station ---------

_TRUCK_REGISTRY = {
    "home_station": [
        {"home_station_id": "hs-dfw-irving-01", "zone": "LZ_NORTH", "lat": 32.8385, "lon": -96.9730},
    ],
    "assignment": [
        {"bank_id": "bank-truck-dfw-01", "hub_id": "truck-dfw-01", "home_station_id": "hs-dfw-irving-01"},
    ],
}


class _Cursor:
    def __init__(self, positions: dict[str, tuple[float | None, float | None]]) -> None:
        self._positions, self._row = positions, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql: str, params: dict[str, str]) -> None:
        assert "FROM og.hub" in sql
        self._row = self._positions.get(params["hub_id"])

    async def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, positions) -> None:
        self._positions = positions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self) -> _Cursor:
        return _Cursor(self._positions)


class _Pool:
    """Just enough of `AsyncConnectionPool` for `ConfigMobileUnitPort._position`."""

    def __init__(self, positions) -> None:
        self._positions = positions

    def connection(self) -> _Conn:
        return _Conn(self._positions)


def _truck_port(position: tuple[float | None, float | None] | None) -> ConfigMobileUnitPort:
    from opengrid.selector.gate import parse_mobile_home_station_sites

    positions = {} if position is None else {"truck-dfw-01": position}
    return ConfigMobileUnitPort(
        parse_mobile_home_stations(_TRUCK_REGISTRY),
        parse_mobile_home_station_sites(_TRUCK_REGISTRY),
        _Pool(positions),  # type: ignore[arg-type]
    )


async def test_a_truck_parked_at_its_home_station_is_at_home():
    port = _truck_port((32.8386, -96.9731))  # ~15 m from the depot
    assert port.is_mobile("truck-dfw-01") and port.is_mobile("bank-truck-dfw-01")
    assert await port.at_home_station("truck-dfw-01") is True


async def test_a_truck_away_from_home_or_without_a_position_is_not_at_home():
    assert await _truck_port((32.7767, -96.7970)).at_home_station("truck-dfw-01") is False  # downtown Dallas
    assert await _truck_port(None).at_home_station("truck-dfw-01") is None  # no og.hub row
    assert await _truck_port((None, None)).at_home_station("truck-dfw-01") is None  # no recorded position


async def test_g35_allows_a_truck_charging_at_home_and_blocks_it_away(fakes, guardian_config, signing_seed):
    item = [ProposedItem("truck-dfw-01", 250.0, "SELECTOR")]
    home = await _verdict(fakes, guardian_config, signing_seed, _truck_port((32.8385, -96.9730)), item)  # type: ignore[arg-type]
    assert "G-35" not in home.vetoed_rule_ids
    away = await _verdict(fakes, guardian_config, signing_seed, _truck_port((29.4241, -98.4936)), item)  # type: ignore[arg-type]
    assert "G-35" in away.vetoed_rule_ids
    # Discharging away from home (serving a deployment) is not G-35's concern.
    serve = await _verdict(
        fakes,
        guardian_config,
        signing_seed,
        _truck_port((29.4241, -98.4936)),  # type: ignore[arg-type]
        [ProposedItem("truck-dfw-01", -250.0, "SELECTOR")],
    )
    assert "G-35" not in serve.vetoed_rule_ids
