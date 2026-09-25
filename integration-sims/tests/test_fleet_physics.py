"""Tests for ogsim.fleet.physics -- SoC step, efficiency, reserve clipping."""

from __future__ import annotations

import math

from ogsim.fleet.physics import clip_commanded_setpoint, soc_step, tick

ETA = math.sqrt(0.90)


def test_soc_step_charging_applies_efficiency() -> None:
    new_soc = soc_step(soc_kwh=5.0, p_kw=4.0, eta_c=ETA, eta_d=ETA, dt_s=3600.0, self_discharge_kwh_per_h=0.0)
    assert new_soc == 5.0 + ETA * 4.0


def test_soc_step_discharging_applies_inverse_efficiency() -> None:
    new_soc = soc_step(
        soc_kwh=10.0, p_kw=-4.0, eta_c=ETA, eta_d=ETA, dt_s=3600.0, self_discharge_kwh_per_h=0.0
    )
    assert new_soc == 10.0 - 4.0 / ETA


def test_soc_step_self_discharge_reduces_soc_when_idle() -> None:
    new_soc = soc_step(
        soc_kwh=10.0, p_kw=0.0, eta_c=ETA, eta_d=ETA, dt_s=3600.0, self_discharge_kwh_per_h=0.0005
    )
    assert new_soc == 10.0 - 0.0005


def test_round_trip_efficiency_is_approximately_90_percent() -> None:
    # Charge 1 kWh worth of grid-side energy in (stores eta_c kWh), then
    # discharge that stored energy straight back out (delivers stored*eta_d
    # kWh to the grid/load): round trip is eta_c*eta_d == 0.90 exactly,
    # per 02b §4.2's eta_c=eta_d=sqrt(0.90).
    soc_after_charge = soc_step(0.0, 1.0, ETA, ETA, 3600.0, 0.0)
    discharge_p_kw = soc_after_charge * ETA  # drains soc_after_charge exactly in 1h
    soc_after_discharge = soc_step(soc_after_charge, -discharge_p_kw, ETA, ETA, 3600.0, 0.0)
    delivered_kwh = discharge_p_kw * 1.0  # 1h at discharge_p_kw
    assert math.isclose(soc_after_discharge, 0.0, abs_tol=1e-9)
    assert math.isclose(delivered_kwh, 0.90, rel_tol=1e-6)


def test_clip_commanded_setpoint_blocks_discharge_below_reserve() -> None:
    # SoC sits right at the reserve floor: any commanded discharge must clip to 0.
    applied = clip_commanded_setpoint(
        soc_kwh=2.7,
        p_kw_setpoint=-5.0,
        home_net_kw=0.0,
        p_kw_limit=5.0,
        e_kwh=13.5,
        r_kwh=2.7,
        eta_c=ETA,
        eta_d=ETA,
        dt_s=2.0,
    )
    assert applied == 0.0


def test_clip_commanded_setpoint_allows_discharge_above_reserve() -> None:
    applied = clip_commanded_setpoint(
        soc_kwh=10.0,
        p_kw_setpoint=-5.0,
        home_net_kw=0.0,
        p_kw_limit=5.0,
        e_kwh=13.5,
        r_kwh=2.7,
        eta_c=ETA,
        eta_d=ETA,
        dt_s=2.0,
    )
    assert applied == -5.0


def test_clip_commanded_setpoint_blocks_charge_above_full() -> None:
    applied = clip_commanded_setpoint(
        soc_kwh=13.5,
        p_kw_setpoint=5.0,
        home_net_kw=0.0,
        p_kw_limit=5.0,
        e_kwh=13.5,
        r_kwh=2.7,
        eta_c=ETA,
        eta_d=ETA,
        dt_s=2.0,
    )
    assert applied == 0.0


def test_clip_commanded_setpoint_respects_p_limit_shared_with_home_load() -> None:
    # PV surplus already charges the battery at 4 kW (home_net_kw < 0); P
    # limit is 5 kW, so at most 1 kW of *additional* commanded charging
    # headroom remains before the combined inverter throughput hits P_i.
    applied = clip_commanded_setpoint(
        soc_kwh=10.0,
        p_kw_setpoint=5.0,
        home_net_kw=-4.0,
        p_kw_limit=5.0,
        e_kwh=13.5,
        r_kwh=2.7,
        eta_c=ETA,
        eta_d=ETA,
        dt_s=2.0,
    )
    assert applied == 1.0


def test_tick_home_load_can_draw_below_reserve_but_not_below_zero() -> None:
    # No commanded setpoint; home load alone should be able to draw the
    # battery below the reserve floor (reserve only gates the *commanded*
    # setpoint), but never below 0.
    new_soc, applied = tick(
        soc_kwh=0.1,
        p_kw_commanded=0.0,
        home_net_kw=5.0,
        p_kw_limit=5.0,
        e_kwh=13.5,
        r_kwh=2.7,
        eta_c=ETA,
        eta_d=ETA,
        dt_s=3600.0,
        self_discharge_kwh_per_h=0.0,
    )
    assert applied == 0.0
    assert new_soc == 0.0
