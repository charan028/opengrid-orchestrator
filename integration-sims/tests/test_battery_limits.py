"""ogsim.fleet.battery_limits: S1.9 F1 SoC/temperature derating curves and cell temperature
(09-optimizer-dispatcher-update.md S1.9, G11)."""

from __future__ import annotations

import numpy as np
import pytest

from ogsim.fleet.battery_limits import (
    cell_temperature_c,
    charge_soc_derate,
    charge_temp_derate,
    discharge_soc_derate,
    discharge_temp_derate,
    p_ch_max_kw,
    p_dis_max_kw,
)


def test_discharge_soc_derate_matches_documented_breakpoints():
    assert discharge_soc_derate(0.20) == pytest.approx(0.30)
    assert discharge_soc_derate(0.30) == pytest.approx(1.0)
    assert discharge_soc_derate(0.25) == pytest.approx(0.65)  # halfway 0.30 -> 1.0
    assert discharge_soc_derate(0.99) == pytest.approx(1.0)


def test_charge_soc_derate_tapers_above_90_pct():
    assert charge_soc_derate(0.0) == pytest.approx(1.0)
    assert charge_soc_derate(0.90) == pytest.approx(1.0)
    assert charge_soc_derate(0.95) == pytest.approx(0.5)
    assert charge_soc_derate(1.00) == pytest.approx(0.0)


def test_discharge_temp_derate_matches_documented_breakpoints():
    assert discharge_temp_derate(-10.0) == pytest.approx(0.0)
    assert discharge_temp_derate(0.0) == pytest.approx(0.3)
    assert discharge_temp_derate(25.0) == pytest.approx(1.0)  # inside the 15-35 plateau
    assert discharge_temp_derate(55.0) == pytest.approx(0.0)
    assert discharge_temp_derate(-50.0) == pytest.approx(0.0)  # clamps flat below range
    assert discharge_temp_derate(80.0) == pytest.approx(0.0)  # clamps flat above range


def test_charge_temp_derate_matches_documented_breakpoints():
    assert charge_temp_derate(-5.0) == pytest.approx(0.0)
    assert charge_temp_derate(40.0) == pytest.approx(1.0)
    assert charge_temp_derate(50.0) == pytest.approx(0.0)


def test_p_dis_max_kw_full_rating_in_the_sweet_spot():
    """SoC 0.5, 25 degC C is comfortably inside both plateaus -- full rated power."""
    assert p_dis_max_kw(11.0, 0.5, 25.0) == pytest.approx(11.0)


def test_p_dis_max_kw_derates_near_reserve_floor():
    assert p_dis_max_kw(11.0, 0.20, 25.0) == pytest.approx(11.0 * 0.30)


def test_p_ch_max_kw_derates_near_full_and_cold():
    # SoC 1.0 (0 taper) AND cold (0 taper) both zero out charging.
    assert p_ch_max_kw(11.0, 1.0, 25.0) == pytest.approx(0.0)
    assert p_ch_max_kw(11.0, 0.5, -5.0) == pytest.approx(0.0)


def test_derating_is_vectorized():
    p_kw_limit = np.array([11.0, 20.0])
    soc_frac = np.array([0.20, 0.5])
    cell_temp_c = np.array([25.0, 25.0])
    result = p_dis_max_kw(p_kw_limit, soc_frac, cell_temp_c)
    assert result == pytest.approx([11.0 * 0.30, 20.0])


def test_cell_temperature_c_rises_with_load_and_noise():
    hour = np.array([15.0, 15.0])  # ambient peak hour
    idle = cell_temperature_c(hour, np.array([0.0, 0.0]), np.array([11.0, 11.0]), np.array([0.0, 0.0]))
    full_discharge = cell_temperature_c(
        hour, np.array([-11.0, -11.0]), np.array([11.0, 11.0]), np.array([0.0, 0.0])
    )
    assert np.all(full_discharge > idle)  # self-heating adds on top of ambient+garage


def test_cell_temperature_c_is_deterministic_given_the_same_noise():
    hour = np.array([10.0])
    a = cell_temperature_c(hour, np.array([5.0]), np.array([11.0]), np.array([1.5]))
    b = cell_temperature_c(hour, np.array([5.0]), np.array([11.0]), np.array([1.5]))
    assert a == pytest.approx(b)
