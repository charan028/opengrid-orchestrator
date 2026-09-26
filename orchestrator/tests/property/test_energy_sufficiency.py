"""K1/K13 energy additions (00-invariants.md, 2026-09-25; NOTICES.md #1): reserve and promises hold in kWh too.

A test marked `xfail(strict=True)` describes behaviour the spec requires that the code does not implement yet;
it turns into a failure the moment it is implemented, prompting removal of the marker.
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import assume, example, given
from hypothesis import strategies as st

from opengrid.core.limits import derated_power_bounds_kw
from opengrid.core.physics import hub_sustainable_discharge_kw
from opengrid.guardian.ports import ProposedItem

from .support import BASE_HUB, HUB_ID, Signer, World, evaluate, make_hub, make_proposal, passing_world

SIGNER = Signer.new()
RESERVE_MARGIN_KWH = BASE_HUB.e_kwh * 0.01
FLOOR_KWH = BASE_HUB.r_kwh + RESERVE_MARGIN_KWH
_soc_kwh = st.floats(min_value=FLOOR_KWH + 0.01, max_value=BASE_HUB.e_kwh)
_discharge_kw = st.floats(min_value=-BASE_HUB.p_kw, max_value=-0.5)
_lease_s = st.floats(min_value=1.0, max_value=7_200.0)


def _seconds_to_drain_kwh(energy_kwh: float, setpoint_kw: float) -> float:
    """Lease length over which discharging at `setpoint_kw` draws `energy_kwh` from the battery."""
    return energy_kwh * BASE_HUB.eta_d * 3600.0 / -setpoint_kw


@st.composite
def _leases_crossing_reserve(draw: st.DrawFn) -> tuple[float, float, float]:
    soc_kwh, setpoint_kw = draw(_soc_kwh), draw(_discharge_kw)
    drain_to_reserve_s = _seconds_to_drain_kwh(soc_kwh - BASE_HUB.r_kwh, setpoint_kw)
    return soc_kwh, setpoint_kw, drain_to_reserve_s * draw(st.floats(min_value=1.05, max_value=5.0))


@st.composite
def _leases_within_the_margin(draw: st.DrawFn) -> tuple[float, float, float]:
    """Within the energy margin AND within P_max(SoC) (G-02 derating, 09 S1.9: 0.3 x P at the floor)."""
    soc_kwh, setpoint_kw = draw(_soc_kwh), draw(_discharge_kw)
    derated_kw = derated_power_bounds_kw(BASE_HUB, soc_kwh, None, unknown_temp_factor=1.0).discharge_kw
    assume(derated_kw >= 0.5)
    setpoint_kw = max(setpoint_kw, -derated_kw)
    drain_to_floor_s = _seconds_to_drain_kwh(soc_kwh - FLOOR_KWH, setpoint_kw)
    return soc_kwh, setpoint_kw, drain_to_floor_s * draw(st.floats(min_value=0.05, max_value=0.9))


def _world(soc_kwh: float, setpoint_kw: float, lease_s: float) -> World:
    world = passing_world([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")], soc_kwh=soc_kwh, lease_s=lease_s)
    world.hub = make_hub(soc_kwh=soc_kwh, prev_p_kw=setpoint_kw)
    return world


@example(case=(8.3, -11.0, 3_600.0))
@given(_leases_crossing_reserve())
def test_energy_guardian_never_signs_a_discharge_that_crosses_reserve_within_its_lease(case):
    verdict = evaluate(_world(*case), SIGNER)

    assert verdict.outcome != "PASS"
    assert verdict.signature is None


@given(_leases_within_the_margin())
def test_energy_guardian_still_signs_a_discharge_whose_projection_stays_above_reserve(case):
    assert evaluate(_world(*case), SIGNER).outcome == "PASS"


@example(health="stale", setpoint_kw=-5.0)
@given(st.sampled_from(["stale", "fault"]), _discharge_kw)
def test_energy_a_stale_or_faulted_soc_means_zero_discharge(health, setpoint_kw):
    world = _world(30.0, setpoint_kw, 10.0)
    world.hub = replace(world.hub, health=health)

    assert evaluate(world, SIGNER).outcome != "PASS"


@given(_discharge_kw)
def test_energy_an_unknown_hub_means_zero_discharge(setpoint_kw):
    world = World(proposal=make_proposal([ProposedItem(HUB_ID, setpoint_kw, "SELECTOR")]), hub=None)

    assert evaluate(world, SIGNER).outcome != "PASS"


@given(
    st.lists(
        st.tuples(st.floats(min_value=0.0, max_value=39.2), st.floats(min_value=0.0, max_value=11.0)),
        min_size=1,
        max_size=50,
    ),
    st.floats(min_value=0.05, max_value=1.0),
)
def test_energy_planned_discharge_over_an_interval_never_exceeds_the_energy_above_reserve(hubs, dt_h):
    promised_kwh = available_kwh = 0.0
    for soc_kwh, p_kw in hubs:
        sustainable_kw = hub_sustainable_discharge_kw(soc_kwh, BASE_HUB.r_kwh, p_kw, dt_h, BASE_HUB.eta_d)
        promised_kwh += sustainable_kw * dt_h / BASE_HUB.eta_d
        available_kwh += max(soc_kwh - BASE_HUB.r_kwh, 0.0)

    assert promised_kwh <= available_kwh + 1e-6
