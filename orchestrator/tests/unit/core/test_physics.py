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
