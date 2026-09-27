"""`opengrid.core.nameplate`: ONE nameplate rule for the guardian (G-02) and the planners (the fleet twin the
selector/allocator plan with, the engine's utility-scale banks, the invariants) -- r3.4 review LOW: a D-31
truck (og.asset MOBILE_STORAGE) was nameplate-rated in the guardian only, so the planners capped it at 11 kW."""

from __future__ import annotations

from opengrid.core import nameplate
from opengrid.core.limits import continuous_power_kw
from opengrid.core.physics import HubParams
from opengrid.engine import gateways
from opengrid.fleet import pg_backend
from opengrid.guardian import repo


def test_nameplate_classes_are_the_substation_set_and_the_trucks():
    assert nameplate.NAMEPLATE_ASSET_CLASSES == ("SUBSTATION", "MOBILE_STORAGE")
    listed = ", ".join(f"'{c}'" for c in nameplate.NAMEPLATE_ASSET_CLASSES)
    for sql in (nameplate.NAMEPLATE_HUB_EXISTS_SQL, nameplate.NAMEPLATE_BANKS_SQL):
        assert f"IN ({listed})" in sql  # the literal SQL lists exactly NAMEPLATE_ASSET_CLASSES
    # Linked by the hub's own (one-hub) bank or by its own id, as the trucks' og.asset rows are.
    assert "a.bank_id = h.bank_id" in nameplate.NAMEPLATE_HUB_EXISTS_SQL
    assert "a.asset_id = h.hub_id" in nameplate.NAMEPLATE_HUB_EXISTS_SQL


def test_every_reader_uses_the_one_rule():
    assert nameplate.NAMEPLATE_HUB_EXISTS_SQL in repo._ALL_HUB_PARAMS_SQL  # guardian G-02
    assert nameplate.NAMEPLATE_HUB_EXISTS_SQL in pg_backend._LOAD_HUBS_SQL  # fleet twin: selector/allocator
    assert gateways._SUBSTATION_BANKS_SQL == nameplate.NAMEPLATE_BANKS_SQL  # engine utility-scale banks


def test_a_nameplate_truck_plans_at_500_kw_and_a_home_hub_at_11():
    truck = HubParams(e_kwh=1000.0, r_kwh=200.0, p_kw=500.0, units=1, utility_scale=True)
    home = HubParams(e_kwh=1000.0, r_kwh=200.0, p_kw=500.0, units=1)
    assert (continuous_power_kw(truck), continuous_power_kw(home)) == (500.0, 11.0)
