"""K1 homeowner reserve (00-invariants.md K1; TS-01): no command drives a hub's SoC below its reserve."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.physics import HubParams, hub_capability, hub_sustainable_discharge_kw, soc_step
from opengrid.guardian.ports import ProposedItem

from .support import BASE_HUB, HUB_ID, Signer, evaluate, make_hub, passing_world

SIGNER = Signer.new()
_EPSILON_KWH = 1e-9


@st.composite
def hub_params(draw: st.DrawFn) -> HubParams:
    e_kwh = draw(st.floats(min_value=10.0, max_value=80.0))
    r_kwh = e_kwh * draw(st.floats(min_value=0.0, max_value=0.5))
    p_kw = draw(st.floats(min_value=1.0, max_value=20.0))
    return HubParams(e_kwh=e_kwh, r_kwh=r_kwh, p_kw=p_kw)


def _self_discharge_kwh(params: HubParams, dt_s: float) -> float:
    return params.self_discharge_kwh_per_h * dt_s / 3600.0


@given(hub_params(), st.floats(min_value=0.0, max_value=1.0), st.floats(min_value=1.0, max_value=900.0))
def test_k01_sustainable_discharge_draws_nothing_below_reserve(params, soc_fraction, dt_s):
    soc = params.e_kwh * soc_fraction
    p_kw = hub_sustainable_discharge_kw(soc, params.r_kwh, params.p_kw, dt_s / 3600.0, params.eta_d)

    new_soc = soc_step(soc, -p_kw, dt_s, params)

    floor = min(soc, params.r_kwh) - _self_discharge_kwh(params, dt_s)
    assert new_soc >= floor - _EPSILON_KWH


@given(hub_params(), st.integers(min_value=1, max_value=200), st.floats(min_value=1.0, max_value=300.0))
def test_k01_sustained_discharge_settles_at_reserve_not_below(params, steps, dt_s):
    soc = params.e_kwh
    loss = _self_discharge_kwh(params, dt_s)
    for step in range(1, steps + 1):
        p_kw = hub_sustainable_discharge_kw(soc, params.r_kwh, params.p_kw, dt_s / 3600.0, params.eta_d)
        soc = soc_step(soc, -p_kw, dt_s, params)
        assert soc >= params.r_kwh - step * loss - _EPSILON_KWH


@given(hub_params(), st.floats(min_value=0.0, max_value=1.0))
def test_k01_no_discharge_capability_at_or_below_reserve(params, soc_fraction):
    soc = params.e_kwh * soc_fraction
    max_discharge_kw, _max_charge_kw = hub_capability(soc, params)

    assert (max_discharge_kw > 0) == (soc > params.r_kwh)
    if soc <= params.r_kwh:
        assert hub_sustainable_discharge_kw(soc, params.r_kwh, params.p_kw, 0.25, params.eta_d) == 0.0


@given(
    st.floats(min_value=0.0, max_value=BASE_HUB.r_kwh + BASE_HUB.e_kwh * 0.01 - 0.001),
    st.floats(min_value=-BASE_HUB.p_kw, max_value=0.0),
)
def test_k01_guardian_never_signs_a_discharge_below_the_reserve_floor(soc_kwh, setpoint_kw):
    world = passing_world([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")], soc_kwh=soc_kwh)
    world.hub = make_hub(soc_kwh=soc_kwh, prev_p_kw=setpoint_kw)

    verdict = evaluate(world, SIGNER)

    assert verdict.outcome != "PASS"
    assert verdict.signature is None


@given(st.floats(min_value=BASE_HUB.r_kwh + 1.0, max_value=BASE_HUB.e_kwh))
def test_k01_guardian_signs_a_modest_discharge_above_the_reserve_floor(soc_kwh):
    world = passing_world([ProposedItem(HUB_ID, -1.0, "SELECTOR")], soc_kwh=soc_kwh)
    world.hub = make_hub(soc_kwh=soc_kwh, prev_p_kw=-1.0)

    assert evaluate(world, SIGNER).outcome == "PASS"
