"""Selector economics per 09: wear on every discharged kWh (D8, Frank #7), the M1 delivery charge and
regulated charging terms (D5, C25 d), K15 territory eligibility (C25 a/b), two-stage lexicographic
selection (D3), the published stored-energy value (D7) and the ES05-S07 rule shadow run (KPI-22)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args
from uuid import uuid4

import pytest

from opengrid.core.models.engine import ServiceType
from opengrid.market import MarketModel
from opengrid.market.config import DEFAULT_UTILITIES
from opengrid.selector import energy_value
from opengrid.selector.extract import extract_plan
from opengrid.selector.gate import (
    _CATEGORY_BY_SERVICE_TYPE,
    as_energy_hold_h,
    bank_market_terms,
    plan_analytics_rows,
    prepare_obligations,
    regulated_value_per_mwh,
    solve_gate,
)
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, ScenarioPrice, SolverSettings
from opengrid.selector.value import plan_net_value, wear_usd_per_kwh
from opengrid.settle.tariffs import TdspTariff
from unit.selector.factories import binary_candidate, committed, continuous_candidate

SETTINGS = SolverSettings(mip_rel_gap=0.0, time_limit_s=10.0)
H0 = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)  # Monday 00:00 America/Chicago: AE off-peak


def _bank(**overrides: object) -> BankSnapshot:
    fields: dict[str, object] = {
        "bank_id": "B1",
        "max_discharge_kw": dict.fromkeys(range(2), 100.0),
        "max_charge_kw": dict.fromkeys(range(2), 100.0),
        "initial_soc_kwh": 50.0,
        "capacity_kwh": 100.0,
        "reserve_kwh": 0.0,
        "eta_c": 1.0,
        "eta_d": 1.0,
        "self_discharge_kwh_per_h": 0.0,
    }
    fields.update(overrides)
    return BankSnapshot(**fields)  # type: ignore[arg-type]


def _inputs(banks, prices: dict[int, float], candidates=(), committed_=(), slack=1000.0) -> ModelInputs:
    n = len(prices)
    return ModelInputs(
        intervals=tuple(range(n)),
        interval_minutes=15.0,
        banks=tuple(banks),
        scenarios=(ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh=dict(prices)),),
        committed=tuple(committed_),
        candidates=tuple(candidates),
        terminal_soc_slack_kwh=slack,
    )


def _solve(inputs: ModelInputs):
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, SETTINGS)
    return outcome, extract_plan(built, outcome, "L-ID")


# --- D8 wear (Frank #7) ---------------------------------------------------------------------------------


def test_wear_coefficient_is_core_wear_cost_per_kwh():
    assert wear_usd_per_kwh(_bank(wear_usd_per_kwh=0.03)) == pytest.approx(0.03)
    assert wear_usd_per_kwh(_bank()) == 0.0


def test_free_headroom_pays_wear_so_a_price_below_it_is_not_worth_discharging():
    """Arbitrage wear was free in the selector (finding G3). At $25/MWh a $30/MWh-wear bank keeps its
    energy; at $40/MWh it sells it."""
    for price, expect_discharge in ((25.0, False), (40.0, True)):
        bank = _bank(max_charge_kw={}, wear_usd_per_kwh=0.03)
        _outcome, plan = _solve(_inputs([bank], {0: price, 1: price}))
        sold = sum(plan.headroom_schedule.values())
        assert (sold > 1e-6) is expect_discharge, price


def test_wear_is_charged_on_committed_deliveries_but_never_on_holds():
    """A committed delivery of 40 kW for 15 min discharges 10 kWh: $0.30 of wear. The same kW as a held
    AS award discharges nothing: no wear. Charging never pays wear (no charge here)."""
    bank = _bank(max_charge_kw={}, wear_usd_per_kwh=0.03, capacity_kwh=1000.0, initial_soc_kwh=500.0)
    delivery = committed("o-deliver", {0: 40.0}, ("B1",))
    hold = replace(committed("o-hold", {0: 40.0}, ("B1",)), energy_hold_h=1.0)

    outcome_delivery, _ = _solve(_inputs([bank], {0: 0.0}, committed_=[delivery]))
    outcome_hold, _ = _solve(_inputs([bank], {0: 0.0}, committed_=[hold]))

    assert outcome_delivery.objective_value == pytest.approx(-0.30, abs=1e-6)
    assert outcome_hold.objective_value == pytest.approx(0.0, abs=1e-6)


# --- D5 M1 delivery charge and regulated charging terms -------------------------------------------------


def test_m1_makes_night_charging_for_evening_sale_unprofitable_in_the_competitive_area():
    """Charge at $20/MWh, sell at $70/MWh: a $50 spread. With Oncor's M1 ($60.295/MWh on grid-drawn kWh)
    the cycle loses money, so the competitive-area bank does not charge; without M1 it does."""
    prices = {0: 20.0, 1: 70.0}
    no_m1 = _bank(initial_soc_kwh=0.0)
    with_m1 = replace(no_m1, delivery_charge_usd_per_kwh=0.060295)

    _o, plan_no_m1 = _solve(_inputs([no_m1], prices))
    _o, plan_m1 = _solve(_inputs([with_m1], prices))

    assert plan_no_m1.charge_by_bank_interval_scenario["B1", 0, "P50"] > 1.0
    assert plan_m1.charge_by_bank_interval_scenario["B1", 0, "P50"] == pytest.approx(0.0, abs=1e-6)


def test_regulated_bank_charges_at_its_utility_terms_not_the_zone_price_plus_m1():
    """A regulated bank's charging cost is its utility's rate (here 2 cents), whatever the wholesale
    price; M1 never applies there. At a $500/MWh zone price it still charges for a $70/MWh sale."""
    bank = _bank(
        initial_soc_kwh=0.0, charge_price_usd_per_kwh={0: 0.02, 1: 0.02}, delivery_charge_usd_per_kwh=0.06
    )
    assert bank.charge_cost_usd_per_kwh(500.0, 0) == 0.02

    _o, plan = _solve(_inputs([bank], {0: 500.0, 1: 70.0}))

    assert plan.charge_by_bank_interval_scenario["B1", 0, "P50"] > 1.0


def _market() -> MarketModel:
    return MarketModel(
        zone_territory={"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"},
        utilities=DEFAULT_UTILITIES,
        banks=[("b-north", "LZ_NORTH"), ("b-aen", "LZ_AEN"), ("b-cps", "LZ_CPS"), ("b-odd", "ZONE_X")],
        tdsp_tariffs=[
            TdspTariff(
                tdsp="ONCOR",
                effective_from=datetime(2026, 9, 1).date(),
                volumetric_usd_per_kwh=Decimal("0.060295"),
                load_zones=("LZ_NORTH",),
            )
        ],
        zone_default_tdsp={"LZ_NORTH": "ONCOR"},
    )


def test_bank_market_terms_resolve_m1_territory_and_regulated_tariff():
    zones = {"b-north": "LZ_NORTH", "b-aen": "LZ_AEN", "b-cps": "LZ_CPS", "b-odd": "ZONE_X"}
    terms = bank_market_terms(_market(), zones, tuple(zones), H0, 2)

    north, aen, odd = terms["b-north"], terms["b-aen"], terms["b-odd"]
    assert (north.territory, north.free_market_access) == ("ERCOT_COMPETITIVE", True)
    assert north.delivery_charge_usd_per_kwh == pytest.approx(0.060295)
    assert north.charge_price_usd_per_kwh == {}
    # Austin Energy: no M1, no FREE access (default); grid kWh at the off-peak TOU rate, solar at the
    # utility's solar price, a 30% solar floor, grid charging only in the off-peak period (D-22).
    assert (aen.territory, aen.free_market_access, aen.delivery_charge_usd_per_kwh) == (
        "AUSTIN_ENERGY",
        False,
        0.0,
    )
    assert aen.charge_price_usd_per_kwh[0] == pytest.approx(0.02677)
    assert aen.solar_cost_usd_per_kwh == pytest.approx(0.040)
    assert aen.solar_share_floor == pytest.approx(0.30)
    assert aen.grid_charge_intervals == frozenset({0, 1})  # Monday 00:00-00:30 CT is off-peak
    assert north.grid_charge_intervals is None and north.solar_share_floor == 0.0
    assert terms["b-cps"].territory == "CPS_ENERGY"
    # Unknown zone: fail closed.
    assert (odd.territory, odd.free_market_access) == (None, False)
    assert all(t.wear_usd_per_kwh == pytest.approx(0.03) for t in terms.values())


def test_prepare_obligations_enforces_territory_and_prices_regulated_capacity():
    banks = ("b-north", "b-aen", "b-cps", "b-odd")
    free = binary_candidate("free", 10.0, 50.0, (0,), banks)
    reg = replace(
        binary_candidate("reg", 10.0, 0.0, (0,), banks), market="REGULATED", utility_id="AUSTIN_ENERGY"
    )
    bad = replace(binary_candidate("bad", 10.0, 50.0, (0,), banks), market="REGULATED", utility_id=None)
    locked = replace(committed("locked", {0: 5.0}, banks), market="REGULATED", utility_id="CPS_ENERGY")

    (c_free, c_reg, c_bad), (co,) = prepare_obligations((free, reg, bad), (locked,), _market())

    assert c_free.eligible_bank_ids == ("b-north",)  # K15 b: no FREE from regulated or unknown banks
    assert c_reg.eligible_bank_ids == ("b-aen",)  # K15 a: only the utility's own territory
    assert c_bad.eligible_bank_ids == ()  # inconsistent market: fail closed
    assert co.eligible_bank_ids == ("b-cps",)
    # $75/kW-yr held for one hour = 75/8760 $/kW-h = $8.5616/MWh-held.
    assert c_reg.value_per_mwh == pytest.approx(75.0 / 8760.0 * 1000.0)
    assert regulated_value_per_mwh(DEFAULT_UTILITIES["CPS_ENERGY"]) == pytest.approx(45.0 / 8760.0 * 1000.0)


def test_headroom_is_barred_for_a_bank_without_free_market_access():
    bank = _bank(max_charge_kw={}, free_market_access=False)
    _o, plan = _solve(_inputs([bank], {0: 900.0, 1: 900.0}))
    assert sum(plan.headroom_schedule.values()) == pytest.approx(0.0, abs=1e-9)
    assert sum(rule_fallback_f2(_inputs([bank], {0: 900.0, 1: 900.0})).headroom_schedule.values()) == 0.0


# --- D3 two-stage lexicographic selection -------------------------------------------------------------


def test_regulated_capacity_is_selected_first_even_when_a_free_offer_pays_more():
    """One 10 kW bank, one interval. The FREE offer pays $100/MWh, the regulated capacity $8.56/MWh.
    A single weighted objective takes the FREE offer; 09 D3 serves the regulated obligation first."""
    free = binary_candidate("free", 10.0, 100.0, (0,), ("B1",))
    reg = replace(
        binary_candidate("reg", 10.0, 75.0 / 8.76, (0,), ("B1",)),
        market="REGULATED",
        utility_id="AUSTIN_ENERGY",
    )
    bank = _bank(max_discharge_kw={0: 10.0}, max_charge_kw={}, capacity_kwh=0.0)

    _o, single = _solve(_inputs([bank], {0: 0.0}, candidates=[free]))
    outcome, plan = _solve(_inputs([bank], {0: 0.0}, candidates=[free, reg]))

    assert single.selected_x["free"] is True
    assert plan.selected_x == {"free": False, "reg": True}
    assert outcome.stage_r_objective == pytest.approx(10.0 * 0.25 * 75.0 / 8760.0, rel=1e-6)
    assert plan.stage_r_objective == outcome.stage_r_objective


def test_without_regulated_candidates_there_is_a_single_stage():
    outcome, _plan = _solve(
        _inputs([_bank()], {0: 10.0, 1: 10.0}, candidates=[binary_candidate("c", 5.0, 50.0, (0,), ("B1",))])
    )
    assert outcome.stage_r_objective is None


# --- D7 stored-energy value -----------------------------------------------------------------------------


def test_stored_energy_value_is_the_c1_dual_and_the_rt_threshold_adds_losses_and_wear():
    """10 kWh stored, cheap now ($0), dear next interval ($200): one more kWh held through interval 0
    earns $200/MWh x eta_d. The RT break-even for spending it is nu/eta_d + wear."""
    eta = 0.9487
    bank = _bank(max_charge_kw={}, initial_soc_kwh=10.0, eta_d=eta, wear_usd_per_kwh=0.01)
    inputs = _inputs([bank], {0: 0.0, 1: 200.0})
    _o, plan = _solve(inputs)

    assert plan.stored_energy_value["B1", 0] == pytest.approx((0.2 - 0.01) * eta, rel=1e-6)
    values = energy_value.build_energy_values(inputs, plan, H0)
    series = values["B1"]
    assert series.water_value_usd_per_mwh[0] == pytest.approx(1000 * (0.2 - 0.01) * eta, rel=1e-6)
    assert series.discharge_threshold_usd_per_mwh[0] == pytest.approx(200.0, rel=1e-6)
    assert series.planned_floor_kwh[1] == pytest.approx(0.0, abs=1e-6)  # sold out by the end

    energy_value.clear()
    energy_value.publish(values)
    try:
        assert energy_value.discharge_threshold_usd_per_mwh("B1", H0 + timedelta(minutes=5)) == pytest.approx(
            200.0, rel=1e-6
        )
        assert energy_value.discharge_threshold_usd_per_mwh("B1", H0 + timedelta(hours=1)) is None
        assert energy_value.discharge_threshold_usd_per_mwh("B1", H0 - timedelta(minutes=1)) is None
        assert energy_value.discharge_threshold_usd_per_mwh("unknown", H0) is None
    finally:
        energy_value.clear()


def test_a_rule_plan_publishes_its_hold_floor_but_no_energy_value():
    """No duals, no made-up water value or threshold (DISPATCH keeps its fallback); the hard hold floor
    is published all the same -- a rule plan's holds are as real."""
    inputs = _inputs([_bank(reserve_kwh=5.0)], {0: 10.0, 1: 10.0})
    (series,) = energy_value.build_energy_values(inputs, rule_fallback_f2(inputs), H0).values()
    assert series.water_value_usd_per_mwh == (None, None)
    assert series.discharge_threshold_usd_per_mwh == (None, None)
    assert series.hold_floor_kwh == (5.0, 5.0)


# --- ES05-S07 shadow run --------------------------------------------------------------------------------


def test_every_gate_runs_the_rule_shadow_and_reports_the_lp_value_added():
    """The rule (F2) sells stored energy at the first non-negative price ($5/MWh); the LP holds it for
    the $150/MWh interval. Both plans are valued by the same evaluator, so value added = the spread."""
    bank = _bank(max_charge_kw={}, initial_soc_kwh=25.0, max_discharge_kw={0: 100.0, 1: 100.0})
    inputs = _inputs([bank], {0: 5.0, 1: 150.0})

    plan = solve_gate(inputs, "SCHEDULED_15MIN", H0, {}, {})

    assert plan.plan_mode == "L-ID" and plan.shadow is not None
    assert plan.shadow.rule_value.energy_revenue == pytest.approx(25.0 * 0.005)
    assert plan.shadow.lp_value.energy_revenue == pytest.approx(25.0 * 0.150)
    assert plan.shadow.value_added == pytest.approx(25.0 * (0.150 - 0.005))
    assert plan.shadow.rule_plan.plan_mode == "RULE_FALLBACK"


def test_rule_headroom_never_spends_energy_the_bank_does_not_have():
    bank = _bank(max_charge_kw={}, initial_soc_kwh=5.0, reserve_kwh=2.5)
    plan = rule_fallback_f2(_inputs([bank], {0: 10.0, 1: 10.0}))
    sold_kwh = sum(plan.headroom_schedule.values()) * 0.25
    assert sold_kwh == pytest.approx(2.5)
    assert min(plan.soc_by_bank_interval_scenario.values()) >= 2.5 - 1e-9


def test_forgone_upside_and_shadow_rows_for_a_locked_obligation():
    """A committed 10 kW obligation (own value $20/MWh) fills the only bank; a $120/MWh candidate that
    wanted the same bank is left unserved. Forgone upside = ($120 - $20)/MWh x 10 kW x 0.25 h = $0.25."""
    bank = _bank(max_discharge_kw={0: 10.0}, max_charge_kw={}, capacity_kwh=0.0)
    locked = replace(committed(str(uuid4()), {0: 10.0}, ("B1",)), value_per_mwh=20.0)
    rival = continuous_candidate("rival", 10.0, 120.0, (0,), ("B1",))
    inputs = _inputs(
        [bank], {0: 0.0}, candidates=[replace(rival, obligation_id=str(uuid4()))], committed_=[locked]
    )

    plan = solve_gate(inputs, "SCHEDULED_15MIN", H0, {}, {})

    assert plan.shadow is not None
    assert plan.shadow.forgone_upside == pytest.approx(0.25)
    locked_row = next(r for r in plan.shadow.obligation_intervals if r.obligation_id == locked.obligation_id)
    assert (locked_row.lp_kw, locked_row.rule_kw) == (10.0, 10.0)
    assert locked_row.best_competing_value_per_kwh == pytest.approx(0.12)

    plan_id = uuid4()
    value_row, shadow_rows, energy_rows = plan_analytics_rows(plan_id, H0, inputs, plan)
    assert value_row["forgone_upside"] == Decimal("0.25")
    assert value_row["value_added"] == Decimal(str(round(plan.shadow.value_added, 4)))
    assert {r["obligation_id"] for r in shadow_rows} == {
        locked.obligation_id,
        inputs.candidates[0].obligation_id,
    }
    assert shadow_rows[0]["interval_end"] - shadow_rows[0]["interval_start"] == timedelta(minutes=15)
    assert energy_rows == []  # no SoC-modelled bank here


def test_plan_net_value_charges_m1_separately_from_energy():
    bank = _bank(initial_soc_kwh=0.0, delivery_charge_usd_per_kwh=0.05)
    inputs = _inputs([bank], {0: 20.0, 1: 20.0})
    plan = replace(
        rule_fallback_f2(inputs),
        charge_by_bank_interval_scenario={("B1", 0, "P50"): 40.0},
        headroom_schedule={},
    )
    value = plan_net_value(inputs, plan)
    assert value.charging_energy_cost == pytest.approx(10.0 * 0.020)
    assert value.delivery_charge == pytest.approx(10.0 * 0.05)
    # 10 kWh more stored at the end, valued at the average charging cost (20 $/MWh + M1) / eta_c.
    assert value.terminal_energy_value == pytest.approx(10.0 * 0.070)
    assert math.isclose(value.net, -0.2 - 0.5 + 0.7, abs_tol=1e-9)


# --- service types --------------------------------------------------------------------------------------


def test_every_service_type_has_a_selector_category():
    missing = set(get_args(ServiceType)) - set(_CATEGORY_BY_SERVICE_TYPE)
    assert not missing, f"no F2 category for {sorted(missing)}"
    assert _CATEGORY_BY_SERVICE_TYPE["REGULATED_CAPACITY"] == "FIRM"
    assert _CATEGORY_BY_SERVICE_TYPE["PIPELINE_AC"] == "FIRM"
    assert _CATEGORY_BY_SERVICE_TYPE["PJM_CAPACITY"] == "MARKET"


def test_regulated_capacity_is_a_capacity_hold():
    assert as_energy_hold_h("REGULATED_CAPACITY", 120) == 2.0
    assert as_energy_hold_h("REGULATED_CAPACITY", None) == 1.0
    assert as_energy_hold_h("PIPELINE_AC", 120) == 0.0
