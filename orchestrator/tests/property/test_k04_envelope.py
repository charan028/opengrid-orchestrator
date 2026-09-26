"""K4 physical envelope (00-invariants.md K4; G-02..G-06): power, kVA and ramp limits always hold."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.limits import (
    check_bank_kva,
    check_feeder_ramp_ceiling,
    check_fleet_ramp_cap,
    check_hub_power,
    check_hub_ramp,
)
from opengrid.core.physics import BankParams, HubParams, apply_ramp_limit, bank_capability
from opengrid.guardian.ports import BankSnapshot, ProposedItem

from .support import BASE_BANK, HUB_ID, Signer, evaluate, make_hub, passing_world

_SIGNER = Signer.new()
_FAST_HUB = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0, ramp_kw_per_s=100.0)
_EPSILON = 1e-9
_kw = st.floats(min_value=-100.0, max_value=100.0, allow_nan=False)
_positive = st.floats(min_value=0.1, max_value=100.0, allow_nan=False)
_seconds = st.floats(min_value=0.5, max_value=900.0, allow_nan=False)
_hubs = st.builds(HubParams, e_kwh=st.just(39.2), r_kwh=st.just(7.84), p_kw=_positive)
_banks = st.builds(
    BankParams,
    kva_rating=st.floats(min_value=50.0, max_value=1000.0),
    reserve_kva=st.floats(min_value=0.0, max_value=25.0),
)


@given(_kw, _hubs, _positive)
def test_k04_hub_power_is_bounded_by_the_smaller_of_inverter_and_hub_limit(p_kw, hub, inverter_cap_kw):
    result = check_hub_power(p_kw, hub, inverter_cap_kw=inverter_cap_kw)

    assert result.ok == (abs(p_kw) <= min(inverter_cap_kw, hub.p_kw) + _EPSILON)


@given(_kw, _kw, _seconds, _positive)
def test_k04_a_ramp_limited_setpoint_always_satisfies_the_guardian_ramp_check(prev_kw, target_kw, dt_s, rate):
    limited = apply_ramp_limit(prev_kw, target_kw, dt_s, rate)

    assert check_hub_ramp(prev_kw, limited, dt_s, rate).ok
    assert min(prev_kw, target_kw) - _EPSILON <= limited <= max(prev_kw, target_kw) + _EPSILON


@given(
    st.floats(min_value=0.0, max_value=1200.0),
    st.floats(min_value=-200.0, max_value=200.0),
    _banks,
    st.floats(min_value=0.5, max_value=1.0),
)
def test_k04_an_accepted_bank_loading_never_exceeds_the_rated_fraction(load_kva, additional_kw, bank, pct):
    result = check_bank_kva(load_kva, additional_kw, bank, loading_pct=pct)

    if result.ok:
        assert load_kva + additional_kw <= bank.kva_rating * pct + _EPSILON


@given(st.lists(st.floats(min_value=0.0, max_value=11.0), max_size=60), _banks)
def test_k04_bank_capability_never_exceeds_rating_minus_reserve(member_kw, bank):
    capability = bank_capability(member_kw, bank)

    assert capability <= max(bank.kva_rating - bank.reserve_kva, 0.0) + _EPSILON
    assert capability <= sum(member_kw) + _EPSILON


@given(_kw, _seconds, st.booleans())
def test_k04_the_tighter_non_firm_fleet_cap_is_never_looser_than_the_firm_cap(delta_kw, dt_s, _unused):
    non_firm = check_fleet_ramp_cap(delta_kw, dt_s, is_firm_event=False)
    firm = check_fleet_ramp_cap(delta_kw, dt_s, is_firm_event=True)

    assert not non_firm.ok or firm.ok


@given(_kw, _seconds, _positive, st.booleans())
def test_k04_the_feeder_ceiling_binds_firm_events_and_only_firm_events(delta_kw, dt_s, ceiling, is_firm):
    result = check_feeder_ramp_ceiling(delta_kw, dt_s, ceiling, is_firm_event=is_firm)

    expected = (not is_firm) or abs(delta_kw) <= ceiling * dt_s / 60.0 + _EPSILON
    assert result.ok == expected


_guardian_banks = st.builds(
    BankParams,
    kva_rating=st.floats(min_value=5.0, max_value=200.0),
    reserve_kva=st.floats(min_value=0.0, max_value=3.0),
)
_setpoint = st.floats(min_value=-11.0, max_value=11.0, allow_nan=False)


@given(st.floats(min_value=0.0, max_value=250.0), _setpoint, _setpoint, _guardian_banks)
def test_k04_the_guardian_never_signs_a_batch_that_overloads_its_bank_in_either_direction(
    load_kva, prev_kw, setpoint_kw, bank
):
    """G-03 on the guardian's own SCADA read: a signed batch either keeps |bank load + its net change|
    within the rating net of reserve, or does not increase the loading magnitude (relief)."""
    world = passing_world([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")])
    world.hub = make_hub(soc_kwh=30.0, prev_p_kw=prev_kw, params=_FAST_HUB)
    world.bank = BankSnapshot(bank, load_kva, feeder_id=None, feeder_ceiling_kw_per_min=None)

    verdict = evaluate(world, _SIGNER)

    if verdict.signature is not None:
        projected = load_kva + setpoint_kw - prev_kw
        limit = (bank.kva_rating - bank.reserve_kva) * 0.95
        assert abs(projected) <= limit + _EPSILON or abs(projected) <= load_kva + _EPSILON


@given(st.floats(min_value=0.0, max_value=250.0), _setpoint, st.floats(min_value=30.01, max_value=1e9))
def test_k04_the_guardian_never_signs_on_a_stale_or_missing_bank_reading(load_kva, setpoint_kw, age_s):
    world = passing_world([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")])
    world.hub = make_hub(soc_kwh=30.0, prev_p_kw=setpoint_kw, params=_FAST_HUB)
    world.bank = BankSnapshot(BASE_BANK, load_kva, None, None, bank_load_age_s=age_s)

    assert evaluate(world, _SIGNER).signature is None
