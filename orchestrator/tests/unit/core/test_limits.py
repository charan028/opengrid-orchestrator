from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.limits import (
    check_bank_kva,
    check_commitment_lock,
    check_feeder_ramp_ceiling,
    check_fleet_ramp_cap,
    check_hub_power,
    check_hub_ramp,
    check_one_buyer,
    check_reserve_floor,
)
from opengrid.core.physics import BankParams, HubParams

HUB = HubParams(e_kwh=13.5, r_kwh=2.7, p_kw=5.0)
BANK = BankParams(kva_rating=75.0, reserve_kva=5.0)


def test_reserve_floor_breach_fails():
    r = check_reserve_floor(2.7, HUB)
    assert not r.ok and r.reason == "RESERVE_FLOOR"


def test_reserve_floor_ok_above_margin():
    r = check_reserve_floor(10.0, HUB)
    assert r.ok


def test_hub_power_limit():
    assert check_hub_power(5.0, HUB).ok
    assert not check_hub_power(11.5, HUB).ok


def test_bank_kva_limit():
    assert check_bank_kva(50.0, 5.0, BANK).ok
    assert not check_bank_kva(70.0, 20.0, BANK).ok


def test_bank_kva_limit_net_loading_over_rating_even_when_within_headroom_pct():
    # additional_kw <= 0 (a discharge/reduction) skips the headroom branch entirely, but the
    # resulting net loading is still checked against the absolute rating*loading_pct ceiling.
    r = check_bank_kva(80.0, -1.0, BANK)
    assert not r.ok and r.reason == "BANK_KVA_LIMIT"


def test_hub_ramp_limit():
    assert check_hub_ramp(0.0, 4.0, dt_s=2.0, ramp_kw_per_s=2.0).ok
    assert not check_hub_ramp(0.0, 10.0, dt_s=2.0, ramp_kw_per_s=2.0).ok


def test_hub_ramp_unconstrained_when_ramp_rate_non_positive():
    assert check_hub_ramp(0.0, 1000.0, dt_s=2.0, ramp_kw_per_s=0.0).ok


def test_fleet_ramp_cap_firm_vs_non_firm():
    # discretionary cap: 50 MW/min * 2s/60 = ~1667 kW; non-firm cap: 10 MW/min * 2s/60 = ~333 kW
    assert check_fleet_ramp_cap(1000.0, dt_s=2.0, is_firm_event=True).ok
    assert not check_fleet_ramp_cap(1000.0, dt_s=2.0, is_firm_event=False).ok


def test_feeder_ramp_ceiling_only_applies_to_firm_events():
    assert check_feeder_ramp_ceiling(9999, dt_s=2.0, feeder_ceiling_kw_per_min=100, is_firm_event=False).ok
    assert not check_feeder_ramp_ceiling(9999, dt_s=2.0, feeder_ceiling_kw_per_min=100, is_firm_event=True).ok


def test_one_buyer_k2():
    assert check_one_buyer([10.0, 20.0], 30.0).ok
    assert not check_one_buyer([10.0, 20.0, 0.1], 30.0).ok


@given(
    reservations=st.lists(st.floats(min_value=0, max_value=100), min_size=0, max_size=20),
    capability=st.floats(min_value=0, max_value=1000),
)
def test_one_buyer_property(reservations, capability):
    result = check_one_buyer(reservations, capability)
    total = sum(reservations)
    assert result.ok == (total <= capability + 1e-9)


def test_commitment_lock_blocks_unreasoned_reduction():
    r = check_commitment_lock(new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code=None)
    assert not r.ok and r.reason == "R-COMMIT-LOCK-VIOLATION"


def test_commitment_lock_allows_reasoned_override():
    r = check_commitment_lock(
        new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code="R-COMMIT-LOCK-OVERRIDE-L1"
    )
    assert r.ok


def test_commitment_lock_as_release_disabled_by_default():
    r = check_commitment_lock(new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code="R-AS-RELEASE")
    assert not r.ok


def test_commitment_lock_as_release_enabled():
    r = check_commitment_lock(
        new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code="R-AS-RELEASE", as_release_enabled=True
    )
    assert r.ok


def test_commitment_lock_never_blocks_increase_or_hold():
    r = check_commitment_lock(new_kw=10.0, frozen_kw=10.0, prior_kw=10.0, reason_code=None)
    assert r.ok
