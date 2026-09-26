from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.physics import (
    BankParams,
    HubParams,
    apply_ramp_limit,
    bank_capability,
    hub_capability,
    recharge_headroom,
    soc_step,
)

HUB = HubParams(e_kwh=13.5, r_kwh=2.7, p_kw=5.0)


def test_soc_step_charge_increases_soc():
    new_soc = soc_step(5.0, 5.0, dt_s=3600, params=HUB)
    assert new_soc > 5.0
    assert new_soc <= HUB.e_kwh


def test_soc_step_discharge_decreases_soc():
    new_soc = soc_step(10.0, -5.0, dt_s=3600, params=HUB)
    assert new_soc < 10.0


def test_soc_step_clamped_to_bounds():
    assert soc_step(13.4, 5.0, dt_s=3600, params=HUB) <= HUB.e_kwh
    assert soc_step(0.1, -5.0, dt_s=3600, params=HUB) >= 0.0


@given(
    soc=st.floats(min_value=0, max_value=13.5),
    p=st.floats(min_value=-5, max_value=5),
    dt=st.floats(min_value=0.1, max_value=7200),
)
def test_soc_step_always_within_bounds(soc, p, dt):
    result = soc_step(soc, p, dt, HUB)
    assert 0.0 - 1e-6 <= result <= HUB.e_kwh + 1e-6


def test_hub_capability_respects_reserve():
    discharge, charge = hub_capability(HUB.r_kwh, HUB)
    assert discharge == 0.0  # at reserve floor exactly, no discharge headroom
    discharge, charge = hub_capability(HUB.e_kwh, HUB)
    assert charge == 0.0  # full, no charge headroom


def test_hub_capability_dual_unit_home_is_capped_at_20_kw():
    """K2/G-02: a full dual-unit home (78.4 kWh) mis-seeded at 24 kW is offered at 20 kW, not 24 or 22."""
    dual = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=24.0, units=2)
    discharge, charge = hub_capability(60.0, dual, dt_h=0.01)
    assert discharge == 20.0
    assert charge == 20.0


def test_hub_capability_single_unit_mis_seeded_is_held_to_11_kw():
    single = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=20.0, units=1)
    discharge, charge = hub_capability(20.0, single, dt_h=0.01)
    assert discharge == 11.0
    assert charge == 11.0


def test_hub_capability_unknown_units_fails_closed_to_one_unit():
    unknown = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=20.0)
    discharge, _charge = hub_capability(60.0, unknown, dt_h=0.01)
    assert discharge == 11.0


def test_hub_capability_p_kw_below_unit_rating_still_binds():
    discharge, _charge = hub_capability(10.0, HUB, dt_h=0.01)
    assert discharge == HUB.p_kw


def test_bank_capability_caps_at_rating():
    bank = BankParams(kva_rating=10.0, reserve_kva=1.0)
    assert bank_capability([5.0, 5.0, 5.0], bank) == 9.0


def test_recharge_headroom_never_negative():
    bank = BankParams(kva_rating=10.0, reserve_kva=1.0)
    assert recharge_headroom(20.0, bank) == 0.0
    assert recharge_headroom(0.0, bank) == 9.0


def test_apply_ramp_limit_clamps():
    assert apply_ramp_limit(0.0, 10.0, dt_s=1.0, ramp_kw_per_s=2.0) == 2.0
    assert apply_ramp_limit(0.0, 1.0, dt_s=1.0, ramp_kw_per_s=2.0) == 1.0
    assert apply_ramp_limit(5.0, -5.0, dt_s=1.0, ramp_kw_per_s=2.0) == 3.0


def test_apply_ramp_limit_unconstrained_when_rate_non_positive():
    assert apply_ramp_limit(3.0, 999.0, dt_s=1.0, ramp_kw_per_s=0.0) == 3.0
