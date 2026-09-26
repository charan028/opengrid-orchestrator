"""Owner decision D-31: mobile storage units (trucks) are NEVER charged from the fleet; they charge only
at their home station (depot), at that station's zone/tariff. No plan row -- LP or rule fallback -- may
feed a mobile obligation from fleet banks or charge a mobile unit away from its home station."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from opengrid.selector import gate
from opengrid.selector.extract import extract_plan
from opengrid.selector.gate import solve_gate
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, ScenarioPrice, SolverSettings
from opengrid.selector.validate import check_mobile_storage, validate_plan
from unit.selector.factories import committed, continuous_candidate

SETTINGS = SolverSettings(mip_rel_gap=0.0, time_limit_s=10.0)
H0 = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)


def _bank(bank_id: str, **overrides: object) -> BankSnapshot:
    fields: dict[str, object] = {
        "bank_id": bank_id,
        "max_discharge_kw": dict.fromkeys(range(4), 100.0),
        "max_charge_kw": dict.fromkeys(range(4), 100.0),
        "initial_soc_kwh": 50.0,
        "capacity_kwh": 400.0,
        "reserve_kwh": 0.0,
        "eta_c": 1.0,
        "eta_d": 1.0,
        "self_discharge_kwh_per_h": 0.0,
    }
    fields.update(overrides)
    return BankSnapshot(**fields)  # type: ignore[arg-type]


def _inputs(banks, prices, candidates=(), committed_=()) -> ModelInputs:
    return ModelInputs(
        intervals=tuple(sorted(prices)),
        interval_minutes=15.0,
        banks=tuple(banks),
        scenarios=(ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh=dict(prices)),),
        committed=tuple(committed_),
        candidates=tuple(candidates),
        terminal_soc_slack_kwh=10_000.0,
    )


def _plans(inputs: ModelInputs):
    built = build_mode_o_model(inputs)
    lp = extract_plan(built, highs_solve(built, SETTINGS), "L-ID")
    return lp, rule_fallback_f2(inputs)


def test_a_mobile_obligation_is_never_fed_from_fleet_banks():
    """A lucrative MOBILE_STORAGE offer eligible on a fleet bank and on the truck: only the truck serves
    it, in the LP and in the rule fallback; the fleet bank never does."""
    fleet = _bank("fleet-1")
    truck = _bank("truck-1", is_mobile=True, home_station_intervals=frozenset({0, 1, 2, 3}))
    offer = replace(
        continuous_candidate("mob", 150.0, 900.0, (1,), ("fleet-1", "truck-1")), service_type="MOBILE_STORAGE"
    )
    inputs = _inputs([fleet, truck], dict.fromkeys(range(4), 30.0), candidates=[offer])
    for plan in _plans(inputs):
        assert plan.bank_interval_allocation.get(("mob", "fleet-1", 1), 0.0) == 0.0
        assert check_mobile_storage(inputs, plan) == []
    lp, _rule = _plans(inputs)
    assert lp.selected_q["mob"] == 100.0  # the truck's own 100 kW, not topped up from the fleet


def test_a_committed_mobile_obligation_without_a_truck_is_never_filled_from_the_fleet():
    fleet = _bank("fleet-1")
    locked = replace(committed("mob-c", {0: 20.0}, ("fleet-1",)), service_type="MOBILE_STORAGE")
    inputs = _inputs([fleet], dict.fromkeys(range(4), 30.0), committed_=[locked])
    lp, rule = _plans(inputs)
    for plan in (lp, rule):
        assert plan.bank_interval_allocation.get(("mob-c", "fleet-1", 0), 0.0) == 0.0
    plan = solve_gate(inputs, "SCHEDULED_15MIN", H0, {}, {})  # validator: a K13 shortfall, never a D-31 row
    assert all(v.startswith("K13") for v in validate_plan(inputs, plan)[1])
    assert check_mobile_storage(inputs, plan) == []


def test_a_mobile_unit_charges_only_at_its_home_station():
    """Charging is paid (negative price) in every interval, but the truck is at its depot only in
    interval 2: it charges there and nowhere else. With no home-station data it never charges."""
    truck = _bank("truck-1", is_mobile=True, home_station_intervals=frozenset({2}), initial_soc_kwh=0.0)
    inputs = _inputs([truck], dict.fromkeys(range(4), -100.0))
    lp, _rule = _plans(inputs)
    charged = {t for (b, t, _w), kw in lp.charge_by_bank_interval_scenario.items() if kw > 1e-6}
    assert charged == {2}
    assert check_mobile_storage(inputs, lp) == []

    unknown = replace(truck, home_station_intervals=None)
    lp_unknown, _ = _plans(_inputs([unknown], dict.fromkeys(range(4), -100.0)))
    assert all(kw <= 1e-6 for kw in lp_unknown.charge_by_bank_interval_scenario.values())


def test_home_station_registry_joins_each_unit_to_its_station_zone():
    raw = {
        "home_station": [
            {"home_station_id": "hs-1", "zone": "LZ_AEN"},
            {"home_station_id": "hs-2", "zone": "LZ_WEST"},
        ],
        "assignment": [{"bank_id": "trailer-1", "home_station_id": "hs-2"}],
    }
    assert gate.parse_mobile_home_stations(raw) == {"trailer-1": "LZ_WEST"}
    with pytest.raises(ValueError, match="unknown home station"):
        gate.parse_mobile_home_stations({"assignment": [{"bank_id": "t", "home_station_id": "nowhere"}]})


def test_the_checked_in_registry_is_read_and_its_unit_takes_the_station_zone(monkeypatch):
    monkeypatch.delenv("OG_CONFIG", raising=False)
    assert gate.load_mobile_units()["trailer-mb-01"] == "LZ_AEN"  # SERVICES' registry, config-first


async def test_a_mobile_units_zone_is_its_home_stations_never_og_bank_zone(monkeypatch):
    async def _db_zones(bank_ids):
        return {"bank-000": "LZ_NORTH", "trailer-mb-01": "LZ_HOUSTON"}  # og.bank.zone: stale for a trailer

    monkeypatch.setattr(gate.db, "load_bank_zones", _db_zones)
    monkeypatch.setattr(gate, "load_mobile_units", lambda: {"trailer-mb-01": "LZ_AEN"})

    zones = await gate.load_bank_zones(("bank-000", "trailer-mb-01"))

    assert zones == {"bank-000": "LZ_NORTH", "trailer-mb-01": "LZ_AEN"}


def test_mobile_units_are_flagged_and_never_charge_until_a_schedule_exists():
    banks = (_bank("bank-000"), _bank("trailer-mb-01", home_station_intervals=frozenset({0})))
    fleet, trailer = gate.mark_mobile_units(banks, {"trailer-mb-01": "LZ_AEN"})
    assert (fleet.is_mobile, trailer.is_mobile, trailer.home_station_intervals) == (False, True, None)
    assert not any(trailer.charging_allowed(t) for t in range(4))


def test_the_validator_flags_a_mobile_unit_charged_away_from_its_station():
    truck = _bank("truck-1", is_mobile=True, home_station_intervals=frozenset({2}))
    inputs = _inputs([truck], dict.fromkeys(range(4), 30.0))
    bad = replace(rule_fallback_f2(inputs), charge_by_bank_interval_scenario={("truck-1", 0, "P50"): 5.0})
    (violation,) = check_mobile_storage(inputs, bad)
    assert violation.startswith("D-31") and "away from its home station" in violation
