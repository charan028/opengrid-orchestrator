"""R3 hardening (lead 2026-09-26): the published hard hold floor, the measured solar/grid charging split
(D-22, D-28), the no-wash-trade rule, stored-energy retention and wear on expected AS deployment."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.core.solar_share import PLANNING_SOLAR_SHARE, solar_part_of_charge_kw, solar_share
from opengrid.selector import energy_value, solar_history
from opengrid.selector.extract import extract_plan
from opengrid.selector.gate import expected_deployment_share, plan_analytics_rows, solve_gate
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, ScenarioPrice, SolverSettings
from opengrid.selector.value import plan_net_value
from unit.selector.factories import committed

SETTINGS = SolverSettings(mip_rel_gap=0.0, time_limit_s=10.0)
H0 = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)  # Monday 12:00 America/Chicago


def _bank(n: int = 2, **overrides: object) -> BankSnapshot:
    fields: dict[str, object] = {
        "bank_id": "B1",
        "max_discharge_kw": dict.fromkeys(range(n), 100.0),
        "max_charge_kw": dict.fromkeys(range(n), 100.0),
        "initial_soc_kwh": 50.0,
        "capacity_kwh": 1000.0,
        "reserve_kwh": 10.0,
        "eta_c": 1.0,
        "eta_d": 1.0,
        "self_discharge_kwh_per_h": 0.0,
    }
    fields.update(overrides)
    return BankSnapshot(**fields)  # type: ignore[arg-type]


def _inputs(
    bank: BankSnapshot, prices: dict[int, float], committed_=(), slack: float = 10_000.0
) -> ModelInputs:
    return ModelInputs(
        intervals=tuple(sorted(prices)),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=(ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh=dict(prices)),),
        committed=tuple(committed_),
        candidates=(),
        terminal_soc_slack_kwh=slack,
    )


def _solve(inputs: ModelInputs):
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, SETTINGS)
    return outcome, extract_plan(built, outcome, "L-ID")


# --- 1. the hard hold floor in the published plan -------------------------------------------------------


def test_published_hold_floor_is_reserve_plus_margin_plus_held_energy_over_the_whole_interval():
    """A committed 40 kW AS award held for 1 h in interval 1 (eta_d 1): e^hold = reserve 10 + the
    G-01-ENERGY margin (1% of 1000) + 40 kWh = 60 kWh at the start of interval 1 -- and already over
    interval 0, whose discharge would eat into energy the hold needs at its end."""
    hold = replace(committed("as-hold", {1: 40.0}, ("B1",)), energy_hold_h=1.0)
    inputs = _inputs(_bank(n=3, initial_soc_kwh=200.0), {0: 30.0, 1: 30.0, 2: 30.0}, committed_=[hold])
    _o, plan = _solve(inputs)

    (series,) = energy_value.build_energy_values(inputs, plan, H0).values()

    assert series.hold_floor_kwh == pytest.approx((60.0, 60.0, 10.0))
    energy_value.clear()
    energy_value.publish({"B1": series})
    try:
        assert energy_value.hold_floor_kwh("B1", H0 + timedelta(minutes=20)) == pytest.approx(60.0)
        assert energy_value.hold_floor_kwh("B1", H0 + timedelta(minutes=40)) == pytest.approx(10.0)
        assert energy_value.hold_floor_kwh("B1", H0 + timedelta(hours=2)) is None
    finally:
        energy_value.clear()
    (_value, _shadow, (row,)) = plan_analytics_rows(uuid4(), H0, inputs, plan)
    assert row["hold_floor_kwh"] == pytest.approx([60.0, 60.0, 10.0])


def test_plan_headroom_never_goes_below_the_hold_floor():
    hold = replace(committed("as-hold", {0: 40.0, 1: 40.0}, ("B1",)), energy_hold_h=1.0)
    inputs = _inputs(_bank(max_charge_kw={}, initial_soc_kwh=100.0), {0: 500.0, 1: 500.0}, committed_=[hold])
    _o, plan = _solve(inputs)
    (series,) = energy_value.build_energy_values(inputs, plan, H0).values()
    for t in (1, 2):
        assert plan.soc_by_bank_interval_scenario["B1", t, "P50"] >= series.hold_floor_kwh[t - 1] - 1e-6


# --- 2. solar / grid charging split (D-22, D-28) --------------------------------------------------------


def test_core_solar_part_uses_the_hubs_own_split_then_pv_surplus_else_unknown():
    kw = Decimal
    assert solar_part_of_charge_kw(kw(5), charge_pv_kw=kw(3), pv_kw=kw(9)) == kw(3)
    assert solar_part_of_charge_kw(kw(5), charge_grid_kw=kw(4)) == kw(1)
    assert solar_part_of_charge_kw(kw(5), pv_kw=kw(6), home_load_kw=kw(2)) == kw(4)
    assert solar_part_of_charge_kw(kw(5), pv_kw=kw(9)) == kw(5)  # capped at the charging power
    assert solar_part_of_charge_kw(kw(5)) is None  # the hub does not report: fall back (D-28)
    assert solar_part_of_charge_kw(kw(0), pv_kw=kw(9)) == kw(0)


def test_core_solar_share_source_priority_is_telemetry_then_ercot_then_assumption():
    kw = Decimal
    measured = solar_share(
        measured_charge_kw_sum=kw(10), measured_solar_kw_sum=kw(4), ercot_solar_share=kw("0.2")
    )
    ercot = solar_share(
        measured_charge_kw_sum=kw(0), measured_solar_kw_sum=kw(0), ercot_solar_share=kw("0.2")
    )
    assumed = solar_share(measured_charge_kw_sum=kw(0), measured_solar_kw_sum=kw(0))
    assert (measured.share, measured.source) == (kw("0.4"), "TELEMETRY")
    assert (ercot.share, ercot.source) == (kw("0.2"), "ERCOT_SOLAR")
    assert (assumed.share, assumed.source) == (PLANNING_SOLAR_SHARE, "ASSUMPTION")
    assert measured.grid_share == kw("0.6")


@dataclass
class _Hub:
    p_kw: float | None
    pv_kw: float | None = None
    home_load_kw: float | None = None


def test_measured_trailing_same_hour_share_per_zone_with_recorded_source():
    store = solar_history.SolarShareHistory()
    noon = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)  # 12:00 CT
    solar_history.sample_fleet(
        {"b1": "LZ_NORTH", "b2": "LZ_NORTH"},
        noon,
        lambda bank_id: [_Hub(p_kw=10.0, pv_kw=6.0, home_load_kw=2.0), _Hub(p_kw=5.0)],  # 2nd: no PV report
        store,
    )
    starts = [(0, H0), (1, H0 + timedelta(hours=12))]  # 12:00 CT (measured) and 00:00 CT (not)

    shares = solar_history.planned_shares("LZ_NORTH", starts, H0, {0: 0.0}, store)
    other_zone = solar_history.planned_shares("LZ_WEST", starts, H0, {}, store)

    assert (shares[0].share, shares[0].source) == (Decimal("0.4"), "TELEMETRY")  # 2 x 4 kW of 2 x 10 kW
    assert (shares[1].share, shares[1].source) == (Decimal("0"), "ERCOT_SOLAR")
    assert other_zone[0].source == "ASSUMPTION"
    # Beyond the trailing week a sample no longer counts.
    later = noon + timedelta(days=8)
    assert solar_history.planned_shares("LZ_NORTH", [(0, later)], later, {}, store)[0].source == "ASSUMPTION"


def test_competitive_bank_charges_pv_surplus_before_grid_and_pv_pays_no_m1():
    """Half the charge envelope is measured PV surplus: it costs its forgone export (the zone price) and
    no M1, so the LP charges it first; the grid part pays M1."""
    bank = _bank(
        n=2,
        initial_soc_kwh=10.0,
        delivery_charge_usd_per_kwh=0.06,
        solar_charge_kw={0: 50.0},
        solar_share={0: 0.5},
        solar_share_source={0: "TELEMETRY"},
    )
    inputs = _inputs(bank, {0: 20.0, 1: 150.0})
    _o, plan = _solve(inputs)

    assert plan.solar_charge_by_bank_interval_scenario["B1", 0, "P50"] == pytest.approx(50.0)
    assert plan.charge_by_bank_interval_scenario["B1", 0, "P50"] == pytest.approx(50.0)  # $80 < $150
    value = plan_net_value(inputs, plan)
    assert value.delivery_charge == pytest.approx(50.0 * 0.25 * 0.06)  # only the grid 12.5 kWh
    (series,) = energy_value.build_energy_values(inputs, plan, H0).values()
    assert series.solar_share[0] == 0.5 and series.solar_share_source[0] == "TELEMETRY"


def test_regulated_bank_meets_the_30_percent_solar_floor_and_charges_grid_only_at_night():
    """Night grid at 2 cents (t=0), utility solar at 4 cents only at t=1, day grid barred (D-22). The bank
    buys 20 kWh; the soft floor makes at least 30% of it solar although grid is cheaper."""
    bank = _bank(
        n=3,
        initial_soc_kwh=10.0,
        max_charge_kw=dict.fromkeys(range(3), 80.0),
        charge_price_usd_per_kwh=dict.fromkeys(range(3), 0.02),
        grid_charge_intervals=frozenset({0}),
        solar_charge_kw={1: 80.0},
        solar_cost_usd_per_kwh=0.04,
        solar_share_floor=0.30,
        territory="AUSTIN_ENERGY",
        free_market_access=False,
    )
    # 80 kW for 15 min = 20 kWh delivered at t=2; the bank starts at its reserve, so it must buy 20 kWh.
    need = replace(committed("reg", {2: 80.0}, ("B1",)), market="REGULATED", utility_id="AUSTIN_ENERGY")
    inputs = _inputs(bank, {0: 0.0, 1: 0.0, 2: 0.0}, committed_=[need])
    outcome, plan = _solve(inputs)

    grid = sum(plan.charge_by_bank_interval_scenario.values()) * 0.25
    solar = sum(plan.solar_charge_by_bank_interval_scenario.values()) * 0.25
    assert outcome.status == "OPTIMAL"
    assert grid + solar == pytest.approx(20.0, abs=1e-6)
    assert solar >= 0.30 * (grid + solar) - 1e-6
    assert plan.charge_by_bank_interval_scenario["B1", 1, "P50"] == pytest.approx(0.0)  # no day grid


# --- 3. no wash trade -----------------------------------------------------------------------------------


def test_no_charging_while_a_bank_delivers_a_regulated_obligation():
    """C7(b)' / D13: even when charging is paid (negative price), a bank fully delivering a regulated
    obligation in the interval does not charge; half delivery leaves half the charge envelope; a FREE
    delivery is not barred."""
    prices = {0: -200.0}
    for share, market, expected_cap in (
        (1.0, "REGULATED", 0.0),
        (0.5, "REGULATED", 50.0),
        (1.0, "FREE", 100.0),
    ):
        delivery = replace(committed("o", {0: 100.0 * share}, ("B1",)), market=market)
        _o, plan = _solve(_inputs(_bank(n=1, initial_soc_kwh=500.0), prices, committed_=[delivery]))
        assert plan.charge_by_bank_interval_scenario["B1", 0, "P50"] == pytest.approx(
            expected_cap, abs=1e-6
        ), (
            share,
            market,
        )


def test_grid_charging_and_headroom_sale_share_one_envelope():
    """No arbitrage wash: at a neutral price (no losses, no wear, no M1) the LP is indifferent, and the
    guard row keeps grid charge and headroom sale within one combined envelope in every interval."""
    inputs = _inputs(_bank(n=2, initial_soc_kwh=500.0), {0: 40.0, 1: 40.0})
    _o, plan = _solve(inputs)
    for t in range(2):
        g = plan.charge_by_bank_interval_scenario["B1", t, "P50"]
        h = plan.headroom_schedule["B1", t, "P50"]
        assert g / 100.0 + h / 100.0 <= 1.0 + 1e-9


def test_at_real_parameters_the_plan_never_charges_and_sells_in_the_same_interval():
    bank = _bank(n=4, initial_soc_kwh=300.0, eta_c=0.9487, eta_d=0.9487, wear_usd_per_kwh=0.03)
    plan = solve_gate(_inputs(bank, {0: -20.0, 1: 90.0, 2: 15.0, 3: 200.0}), "SCHEDULED_15MIN", H0, {}, {})
    for t in range(4):
        g = plan.charge_by_bank_interval_scenario.get(("B1", t, "P50"), 0.0)
        h = plan.headroom_schedule.get(("B1", t, "P50"), 0.0)
        assert min(g, h) <= 1e-6, (t, g, h)


# --- 5. wear on expected AS deployment ------------------------------------------------------------------


def test_expected_as_deployment_pays_wear_on_its_expected_discharge_only():
    """A 40 kW held award, psi = 2%, wear $0.03/kWh: 0.02 x 40 kW x 0.25 h x 0.03 = $0.006 per interval."""
    hold = replace(committed("as", {0: 40.0}, ("B1",)), energy_hold_h=1.0, expected_deployment_share=0.02)
    bank = _bank(n=1, max_charge_kw={}, wear_usd_per_kwh=0.03, initial_soc_kwh=500.0)
    inputs = _inputs(bank, {0: 0.0}, committed_=[hold])
    outcome, plan = _solve(inputs)

    assert outcome.objective_value == pytest.approx(-0.006, abs=1e-9)
    assert plan_net_value(inputs, plan).wear == pytest.approx(0.006)
    assert expected_deployment_share("ERCOT_AS") == pytest.approx(0.02)
    assert expected_deployment_share("REGULATED_CAPACITY") == 0.0
