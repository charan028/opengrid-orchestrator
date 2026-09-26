"""K1/K4 on the per-hub SUM: a hub carrying several items (one per obligation) executes their sum, so the
guardian must never sign a batch whose per-hub sum breaks the power, reserve or lease-energy limits -- even
when every item alone would pass (two 11 kW items on an 11 kW hub command 22 kW)."""

from __future__ import annotations

from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core.limits import derated_power_bounds_kw
from opengrid.core.physics import HubParams, project_soc_over_lease_kwh
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import ProposedItem

from .support import HUB_ID, LEASE_S, Signer, evaluate, make_hub, passing_world

_SIGNER = Signer.new()
_FAST_HUB = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0, ramp_kw_per_s=1000.0)
_CONFIG = GuardianConfig(
    key_path="",
    cycle_interval_s=2.0,
    discretionary_ramp_cap_kw_per_min=1e9,
    non_firm_ramp_cap_kw_per_min=1e9,
)
_EPSILON = 1e-6


@settings(max_examples=400, deadline=None)
@given(
    st.lists(st.floats(min_value=-11.0, max_value=11.0, allow_nan=False), min_size=1, max_size=5),
    st.floats(min_value=0.0, max_value=39.2, allow_nan=False),
)
def test_k04_the_guardian_never_signs_a_per_hub_sum_beyond_its_limits(setpoints, soc_kwh):
    items = [ProposedItem(HUB_ID, p, "R-GRANT-COMMITTED", uuid4()) for p in setpoints]
    total = sum(setpoints)
    world = passing_world(items)
    world.hub = make_hub(soc_kwh=soc_kwh, prev_p_kw=total, params=_FAST_HUB)  # no ramp: prev == the sum

    verdict = evaluate(world, _SIGNER, config=_CONFIG)

    if verdict.signature is not None:
        bounds = derated_power_bounds_kw(_FAST_HUB, soc_kwh, None, unknown_temp_factor=1.0)
        assert -total <= bounds.discharge_kw + _EPSILON
        assert total <= bounds.charge_kw + _EPSILON
        projected = project_soc_over_lease_kwh(
            soc_kwh, total, LEASE_S / 3600.0, _FAST_HUB.eta_c, _FAST_HUB.eta_d
        )
        if total < 0:
            assert projected >= _FAST_HUB.r_kwh + 0.01 * _FAST_HUB.e_kwh - _EPSILON
        assert soc_kwh >= _FAST_HUB.r_kwh + 0.01 * _FAST_HUB.e_kwh - _EPSILON


def test_two_full_power_items_on_one_hub_are_vetoed_although_each_alone_passes():
    items = [ProposedItem(HUB_ID, -11.0, "R-GRANT-COMMITTED", uuid4()) for _ in range(2)]
    world = passing_world(items)
    world.hub = make_hub(soc_kwh=35.0, prev_p_kw=-22.0, params=_FAST_HUB)

    verdict = evaluate(world, _SIGNER, config=_CONFIG)

    assert verdict.signature is None and "G-02" in verdict.vetoed_rule_ids

    single = passing_world([ProposedItem(HUB_ID, -11.0, "R-GRANT-COMMITTED", uuid4())])
    single.hub = make_hub(soc_kwh=35.0, prev_p_kw=-11.0, params=_FAST_HUB)
    assert evaluate(single, _SIGNER, config=_CONFIG).signature is not None
