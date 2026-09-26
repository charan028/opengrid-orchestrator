from hypothesis import given
from hypothesis import strategies as st

from opengrid.core import reasons
from opengrid.core.limits import (
    check_bank_kva,
    check_commitment_lock,
    check_feeder_ramp_ceiling,
    check_fleet_ramp_cap,
    check_hub_power,
    check_hub_ramp,
    check_one_buyer,
    check_reserve_floor,
    check_reserve_floor_over_lease,
    continuous_power_kw,
    unit_rating_kw,
)
from opengrid.core.physics import BankParams, HubParams, project_soc_over_lease_kwh

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


def test_dual_unit_home_is_capped_at_its_own_rating_not_one_inverter():
    """Regression: min(inverter_cap_kw=11, p_kw) capped the 20 kW dual-unit homes at 11 kW."""
    dual = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=20.0, units=2)
    single = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0, units=1)
    assert check_hub_power(20.0, dual, inverter_cap_kw=11.0).ok
    assert check_hub_power(-20.0, dual, inverter_cap_kw=11.0).ok
    assert not check_hub_power(21.0, dual, inverter_cap_kw=11.0).ok
    assert not check_hub_power(11.5, single, inverter_cap_kw=11.0).ok


def test_dual_unit_rating_is_20_kw_not_twice_the_per_unit_cap():
    """Base battery specs: 78.4 kWh / 20 kW per dual-unit home -- a p_kw seeded above 20 does not lift it to 22."""
    dual = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=24.0, units=2)
    assert check_hub_power(20.0, dual, inverter_cap_kw=11.0).ok
    assert not check_hub_power(20.5, dual, inverter_cap_kw=11.0).ok
    assert not check_hub_power(22.0, dual, inverter_cap_kw=11.0).ok


def test_mis_seeded_p_kw_on_a_single_unit_home_is_vetoed():
    """Review fix (G-02 dead cap): a single-unit home mis-seeded at p_kw=20 is still held to 11 kW."""
    mis_seeded = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=20.0, units=1)
    result = check_hub_power(20.0, mis_seeded)
    assert not result.ok and result.reason == reasons.R_HUB_POWER_LIMIT
    assert check_hub_power(11.0, mis_seeded).ok


def test_unknown_unit_count_fails_closed_to_one_unit():
    """No unit count (a caller that does not pass `units`): assume ONE unit, cap = min(p_kw, 11 kW)."""
    unknown = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=20.0)
    assert not check_hub_power(20.0, unknown).ok
    assert check_hub_power(11.0, unknown).ok
    assert continuous_power_kw(HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=5.0)) == 5.0  # p_kw below the cap binds


def test_out_of_range_unit_count_fails_closed_to_one_unit():
    assert unit_rating_kw(3) == 11.0
    assert unit_rating_kw(0) == 11.0
    assert unit_rating_kw(2) == 20.0
    assert unit_rating_kw(2, inverter_cap_kw=9.0) == 18.0  # a tighter per-unit cap still binds


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


def test_feeder_ramp_ceiling_firm_event_passes_when_within_ceiling():
    """CORE-006: a firm event's delta strictly inside the per-minute ceiling passes cleanly (the
    existing test above only exercises the failing/non-firm cases)."""
    # ceiling = 100 kW/min * (2s / 60) = ~3.33 kW; 2.0 kW is comfortably within it.
    r = check_feeder_ramp_ceiling(2.0, dt_s=2.0, feeder_ceiling_kw_per_min=100.0, is_firm_event=True)
    assert r.ok


# --- CORE-005: Hypothesis property tests for the K1/K4/K13 checks ---------------------------------


@given(
    soc_kwh=st.floats(min_value=0, max_value=1000, allow_nan=False),
    e_kwh=st.floats(min_value=1, max_value=1000, allow_nan=False),
    r_kwh=st.floats(min_value=0, max_value=999, allow_nan=False),
    margin_pct=st.floats(min_value=0, max_value=0.5, allow_nan=False),
)
def test_check_reserve_floor_property_matches_definition(soc_kwh, e_kwh, r_kwh, margin_pct):
    """K1: `check_reserve_floor` fails iff `soc_kwh` is strictly below `r_kwh + margin_pct * e_kwh`,
    for any hub sizing/margin combination -- never lets a command pass below the reserve floor."""
    params = HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=5.0)
    result = check_reserve_floor(soc_kwh, params, margin_pct=margin_pct)
    assert result.ok == (soc_kwh >= r_kwh + margin_pct * e_kwh)
    if not result.ok:
        assert result.reason == reasons.R_RESERVE_FLOOR


@given(
    new_kw=st.floats(min_value=-100, max_value=100, allow_nan=False),
    frozen_kw=st.floats(min_value=0, max_value=100, allow_nan=False),
    prior_kw=st.floats(min_value=0, max_value=100, allow_nan=False),
    reason_code=st.sampled_from(
        [None, *reasons.COMMIT_LOCK_OVERRIDE_REASONS, "R-AS-RELEASE", "R-NOT-A-REAL-REASON"]
    ),
    as_release_enabled=st.booleans(),
)
def test_check_commitment_lock_property_never_allows_an_unreasoned_reduction(
    new_kw, frozen_kw, prior_kw, reason_code, as_release_enabled
):
    """K13: a reduction below `min(frozen_kw, prior_kw)` is allowed if and only if `reason_code` is one
    of the documented overrides (or `R-AS-RELEASE` with the audited path enabled) -- for any arrival
    order/price combination the property tests below and elsewhere in the allocator exercise, this
    floor-and-allowlist relationship must always hold."""
    result = check_commitment_lock(
        new_kw=new_kw,
        frozen_kw=frozen_kw,
        prior_kw=prior_kw,
        reason_code=reason_code,
        as_release_enabled=as_release_enabled,
    )
    floor = min(frozen_kw, prior_kw)
    if new_kw >= floor - 1e-9:
        assert result.ok
        return
    allowed = reason_code in reasons.COMMIT_LOCK_OVERRIDE_REASONS or (
        reason_code == reasons.R_AS_RELEASE and as_release_enabled
    )
    assert result.ok == allowed
    if not result.ok:
        assert result.reason == reasons.R_COMMIT_LOCK_VIOLATION


@given(
    prev_p_kw=st.floats(min_value=-100, max_value=100, allow_nan=False),
    target_p_kw=st.floats(min_value=-100, max_value=100, allow_nan=False),
    dt_s=st.floats(min_value=0.1, max_value=60, allow_nan=False),
    ramp_kw_per_s=st.floats(min_value=0, max_value=50, allow_nan=False),
)
def test_check_hub_ramp_property_matches_the_ramp_bound(prev_p_kw, target_p_kw, dt_s, ramp_kw_per_s):
    """K4/G-04: `check_hub_ramp` fails iff the requested step exceeds `ramp_kw_per_s * dt_s`, for any
    previous/target setpoint pair (a non-positive rate is unconstrained, matching `apply_ramp_limit`'s
    own convention when only a single rate is given)."""
    result = check_hub_ramp(prev_p_kw, target_p_kw, dt_s, ramp_kw_per_s)
    if ramp_kw_per_s <= 0:
        assert result.ok
        return
    assert result.ok == (abs(target_p_kw - prev_p_kw) <= ramp_kw_per_s * dt_s + 1e-9)


@given(
    fleet_delta_kw=st.floats(min_value=-100_000, max_value=100_000, allow_nan=False),
    dt_s=st.floats(min_value=0.1, max_value=300, allow_nan=False),
    is_firm_event=st.booleans(),
)
def test_check_fleet_ramp_cap_property_uses_the_right_cap_for_firm_vs_non_firm(
    fleet_delta_kw, dt_s, is_firm_event
):
    """K4/G-05: firm events get the (looser) discretionary cap, non-firm events the tighter one --
    for any delta/duration, the check's pass/fail must match whichever cap applies."""
    result = check_fleet_ramp_cap(fleet_delta_kw, dt_s, is_firm_event=is_firm_event)
    cap_per_min = 50_000.0 if is_firm_event else 10_000.0
    cap_kw = cap_per_min * (dt_s / 60.0)
    assert result.ok == (abs(fleet_delta_kw) <= cap_kw + 1e-9)


# --- G-01-ENERGY: lease-duration energy projection (K1) --------------------------------------------


@given(
    e_kwh=st.floats(min_value=1, max_value=200, allow_nan=False),
    soc_frac=st.floats(min_value=0, max_value=1, allow_nan=False),
    r_frac=st.floats(min_value=0, max_value=1, allow_nan=False),
    p_kw=st.floats(min_value=1e-3, max_value=100, allow_nan=False),  # a genuine (nonzero) discharge
    lease_ttl_h=st.floats(min_value=1e-3, max_value=24, allow_nan=False),
    margin_pct=st.floats(min_value=0, max_value=0.5, allow_nan=False),
)
def test_property_a_no_command_ever_implies_discharging_below_reserve_within_its_lease(
    e_kwh, soc_frac, r_frac, p_kw, lease_ttl_h, margin_pct
):
    """Property (a): whenever `check_reserve_floor_over_lease` PASSES a genuine discharge command, the
    hub's projected SoC at the END of the command's full lease duration is still >= reserve + margin --
    for ANY physically-valid combination of SoC/capacity/reserve/power/lease-length/margin (0 <= r_kwh
    <= soc_kwh's own [0, e_kwh] range is not required here -- soc CAN start below reserve, that's exactly
    the case this check must catch). This is the exact guarantee G-01-ENERGY exists to make (capacity/kW
    headroom alone is not enough)."""
    soc_kwh = soc_frac * e_kwh
    r_kwh = r_frac * e_kwh
    params = HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=p_kw)
    result = check_reserve_floor_over_lease(soc_kwh, -p_kw, lease_ttl_h, params, margin_pct=margin_pct)
    projected = project_soc_over_lease_kwh(soc_kwh, -p_kw, lease_ttl_h, params.eta_c, params.eta_d)
    if result.ok:
        assert projected >= r_kwh + e_kwh * margin_pct - 1e-6
    else:
        assert projected < r_kwh + e_kwh * margin_pct + 1e-6
        assert result.reason == reasons.R_RESERVE_FLOOR_LEASE


@given(
    e_kwh=st.floats(min_value=1, max_value=200, allow_nan=False),
    soc_frac=st.floats(min_value=0, max_value=1, allow_nan=False),
    p_kw=st.floats(min_value=1e-3, max_value=100, allow_nan=False),  # a genuine (nonzero) charge
    lease_ttl_h=st.floats(min_value=1e-3, max_value=24, allow_nan=False),
)
def test_check_reserve_floor_over_lease_symmetric_charge_ceiling(e_kwh, soc_frac, p_kw, lease_ttl_h):
    """Symmetric charge-direction half of property (a): a charge command that would overfill above
    `e_kwh` before the lease expires is vetoed too."""
    soc_kwh = soc_frac * e_kwh
    params = HubParams(e_kwh=e_kwh, r_kwh=0.0, p_kw=p_kw)
    result = check_reserve_floor_over_lease(soc_kwh, p_kw, lease_ttl_h, params)
    projected = project_soc_over_lease_kwh(soc_kwh, p_kw, lease_ttl_h, params.eta_c, params.eta_d)
    assert result.ok == (projected <= e_kwh + 1e-9)
