"""09 S2.6 discharge-flow checks (G-02 derated, G-26..G-33) and the core bounds they share with DISPATCH."""

from __future__ import annotations

import math
from dataclasses import replace
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core import limits
from opengrid.core.physics import HubParams
from opengrid.guardian import flow_checks
from opengrid.guardian.ports import (
    AggregateFlow,
    HubFlowTelemetry,
    HubSite,
    HubSnapshot,
    ObligationMarket,
    PoiLimit,
    ProposedItem,
    Reading,
    ServiceTransformer,
)

HUB = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
POLICY = flow_checks.FlowPolicy(
    telemetry_required=False,
    max_age_s=30.0,
    unknown_temp_factor=0.5,
    load_drop_kw=0.5,
    inverter_cap_kw=11.0,
    default_pv_rated_kw=0.0,
    default_service_kw=48.0,
    xfmr_forward_pct=1.0,
    xfmr_reverse_pct=1.0,
    xfmr_max_stale_fraction=0.2,
    unmapped_xfmr_kva_per_home=5.0,
)
SITE = HubSite(
    export_limit_kw=10.0, service_kw=48.0, pv_rated_kw=7.0, peak_kw=None, tau_peak_s=None, transformer_id="x1"
)
FRESH, STALE = 1.0, 999.0


def hub(*, soc: float = 30.0, prev: float = 0.0, **flow: Reading | None) -> HubSnapshot:
    return HubSnapshot(
        params=HUB, soc_kwh=soc, prev_p_kw=prev, health="online", flow=HubFlowTelemetry(**flow)
    )


def item(p_kw: float, hub_id: str = "hub-1") -> ProposedItem:
    return ProposedItem(hub_id, p_kw, "R-GRANT-COMMITTED")


# --- G-02 derated (F1) ---------------------------------------------------------------------------------


def test_stale_temperature_at_11_kw_is_vetoed_above_5_5_kw():
    h = hub(cell_temp_c=Reading(25.0, STALE))
    assert flow_checks.check_g02_derated(item(-5.5), h, POLICY).ok
    assert not flow_checks.check_g02_derated(item(-5.6), h, POLICY).ok


def test_45_c_at_11_kw_is_vetoed_above_7_7_kw():
    h = hub(cell_temp_c=Reading(45.0, FRESH))
    assert flow_checks.check_g02_derated(item(-7.7), h, POLICY).ok
    result = flow_checks.check_g02_derated(item(-7.8), h, POLICY)
    assert not result.ok and result.reason == "HUB_POWER_DERATED"


def test_a_never_reported_temperature_uses_the_nameplate_until_required():
    assert flow_checks.check_g02_derated(item(-11.0), hub(), POLICY).ok
    required = replace(POLICY, telemetry_required=True)
    assert not flow_checks.check_g02_derated(item(-11.0), hub(), required).ok


def test_the_bms_limit_binds_and_a_stale_one_drops_out():
    assert not flow_checks.check_g02_derated(item(-6.0), hub(p_dis_max_kw=Reading(5.0, FRESH)), POLICY).ok
    assert flow_checks.check_g02_derated(item(-6.0), hub(p_dis_max_kw=Reading(5.0, STALE)), POLICY).ok


def test_soc_derating_near_the_floor_and_charge_taper_near_full():
    near_floor = hub(soc=HUB.r_kwh + 0.02 * HUB.e_kwh)  # factor 0.3 + 7 x 0.02 = 0.44
    assert flow_checks.check_g02_derated(item(-4.8), near_floor, POLICY).ok
    assert not flow_checks.check_g02_derated(item(-5.0), near_floor, POLICY).ok
    near_full = hub(soc=0.95 * HUB.e_kwh)  # charge factor 0.5
    assert flow_checks.check_g02_derated(item(5.5), near_full, POLICY).ok
    assert not flow_checks.check_g02_derated(item(5.6), near_full, POLICY).ok


@settings(max_examples=300)
@given(
    st.floats(min_value=0.0, max_value=39.2),
    st.one_of(st.none(), st.floats(min_value=-20.0, max_value=60.0)),
    st.floats(min_value=-15.0, max_value=15.0),
)
def test_g02_passes_exactly_within_the_shared_core_bound(soc, temp, p_kw):
    """PASS <=> |p| within the SAME `core.limits.derated_power_bounds_kw` the allocator caps with."""
    h = hub(soc=soc, cell_temp_c=Reading(temp, FRESH) if temp is not None else None)
    bounds = limits.derated_power_bounds_kw(HUB, soc, temp, unknown_temp_factor=1.0)
    expected = -p_kw <= bounds.discharge_kw + 1e-9 and p_kw <= bounds.charge_kw + 1e-9
    assert flow_checks.check_g02_derated(item(p_kw), h, POLICY).ok == expected


# --- G-31 peak (F5) ------------------------------------------------------------------------------------


def test_a_30_s_lease_above_continuous_with_a_10_s_peak_window_is_vetoed():
    site = HubSite(10.0, 48.0, 0.0, peak_kw=15.0, tau_peak_s=10.0, transformer_id=None)
    h = hub(peak_budget_kws=Reading(1_000.0, FRESH))
    assert not flow_checks.check_g31_peak(item(-13.0), h, site, 30.0, POLICY).ok
    assert flow_checks.check_g31_peak(item(-13.0), h, site, 10.0, POLICY).ok


def test_above_continuous_without_a_budget_or_peak_rating_is_vetoed():
    site = HubSite(10.0, 48.0, 0.0, peak_kw=15.0, tau_peak_s=10.0, transformer_id=None)
    assert not flow_checks.check_g31_peak(item(-13.0), hub(), site, 5.0, POLICY).ok  # no budget
    no_peak = HubSite(10.0, 48.0, 0.0, peak_kw=None, tau_peak_s=None, transformer_id=None)
    assert not flow_checks.check_g31_peak(
        item(-13.0), hub(peak_budget_kws=Reading(1e6, FRESH)), no_peak, 5.0, POLICY
    ).ok
    assert flow_checks.check_g31_peak(item(-11.0), hub(), no_peak, 60.0, POLICY).ok  # continuous: G-31 silent


@settings(max_examples=300)
@given(
    st.floats(min_value=0.0, max_value=25.0),
    st.floats(min_value=0.1, max_value=60.0),
    st.floats(min_value=0.1, max_value=30.0),
    st.floats(min_value=0.0, max_value=200.0),
)
def test_g31_never_admits_more_than_the_peak_budget(p_abs, ttl_s, tau_s, budget):
    site = HubSite(20.0, 48.0, 0.0, peak_kw=16.0, tau_peak_s=tau_s, transformer_id=None)
    ok = flow_checks.check_g31_peak(
        item(-p_abs), hub(peak_budget_kws=Reading(budget, FRESH)), site, ttl_s, POLICY
    ).ok
    if ok and p_abs > 11.0 + 1e-9:
        assert p_abs <= 16.0 + 1e-9 and ttl_s <= tau_s + 1e-9 and (p_abs - 11.0) * ttl_s <= budget + 1e-9


# --- G-26 home meter (F2) --------------------------------------------------------------------------------


def test_stale_meter_with_7_kw_pv_and_10_kw_export_limit_vetoes_discharge_above_3_kw():
    h = hub(meter_kw=Reading(2.0, STALE))
    # L := -PV = -7: export side -7 - 0.5 + p >= -10 -> p >= -2.5 (with the 0.5 kW load-drop allowance)
    assert flow_checks.check_g26_home_meter(item(-2.5), h, SITE, POLICY).ok
    result = flow_checks.check_g26_home_meter(item(-3.0), h, SITE, POLICY)
    assert not result.ok and result.reason == "HOME_EXPORT_LIMIT"


def test_unknown_export_limit_allows_discharge_only_into_the_measured_load():
    site = HubSite(None, 48.0, 0.0, None, None, None)
    h = hub(meter_kw=Reading(4.0, FRESH), prev=0.0)  # load 4 kW
    assert flow_checks.check_g26_home_meter(item(-3.5), h, site, POLICY).ok
    assert not flow_checks.check_g26_home_meter(item(-4.0), h, site, POLICY).ok


def test_import_side_is_bounded_by_the_service_rating():
    h = hub(meter_kw=Reading(40.0, FRESH), prev=0.0)
    assert not flow_checks.check_g26_home_meter(item(8.0), h, SITE, POLICY).ok
    assert flow_checks.check_g26_home_meter(item(7.5), h, SITE, POLICY).ok


def test_meter_relief_passes_and_unknown_site_is_vetoed():
    h = hub(meter_kw=Reading(-15.0, FRESH), prev=-11.0)  # exporting 15 > 10 already
    assert flow_checks.check_g26_home_meter(item(-9.0), h, SITE, POLICY).ok
    assert not flow_checks.check_g26_home_meter(item(-3.0), h, None, POLICY).ok


@settings(max_examples=300)
@given(
    st.floats(min_value=-10.0, max_value=30.0),
    st.floats(min_value=-11.0, max_value=11.0),
    st.floats(min_value=-11.0, max_value=11.0),
    st.floats(min_value=0.0, max_value=20.0),
)
def test_g26_signed_setpoints_keep_the_meter_within_its_limits(load, prev, p_kw, export_limit):
    site = HubSite(export_limit, 48.0, 0.0, None, None, None)
    h = hub(meter_kw=Reading(load + prev, FRESH), prev=prev)
    ok = flow_checks.check_g26_home_meter(item(p_kw), h, site, POLICY).ok
    lo, hi = -export_limit - (load - 0.5), 48.0 - (load + 0.5)
    export_breach = p_kw < lo - 1e-9 and p_kw < prev - 1e-9
    import_breach = p_kw > hi + 1e-9 and p_kw > prev + 1e-9
    assert ok == (not export_breach and not import_breach)


def test_a_stale_meter_never_forces_a_discharge_but_blocks_new_charging():
    h = hub(meter_kw=Reading(2.0, STALE), prev=0.0)
    assert flow_checks.check_g26_home_meter(item(0.0), h, SITE, POLICY).ok
    assert not flow_checks.check_g26_home_meter(item(1.0), h, SITE, POLICY).ok


# --- G-27 service transformer (group) -------------------------------------------------------------------


def _members(meters: dict[str, float | None]) -> dict[str, flow_checks.TransformerMember]:
    return {
        h: flow_checks.TransformerMember(
            hub(meter_kw=Reading(m, FRESH) if m is not None else Reading(0.0, STALE)), SITE
        )
        for h, m in meters.items()
    }


def test_midday_reverse_flow_above_the_rating_is_vetoed():
    x = ServiceTransformer("x1", 25.0, ("a", "b", "c"))
    members = _members({"a": -8.0, "b": -8.0, "c": -8.0})  # -24 kW
    ok, reason = flow_checks.check_g27_transformer(x, members, -2.0, POLICY)
    assert not ok and reason == "XFMR_LIMIT"
    assert flow_checks.check_g27_transformer(x, members, 0.5, POLICY)[0]


def test_one_stale_member_plus_an_increase_is_vetoed():
    x = ServiceTransformer("x1", 25.0, ("a", "b", "c"))
    members = _members({"a": 1.0, "b": 1.0, "c": None})  # 1 of 3 stale > 20%
    ok, reason = flow_checks.check_g27_transformer(x, members, -1.0, POLICY)
    assert not ok and reason == "XFMR_MEMBERS_STALE"


@settings(max_examples=300)
@given(
    st.lists(st.floats(min_value=-11.0, max_value=15.0), min_size=1, max_size=8),
    st.floats(min_value=-30.0, max_value=30.0),
    st.floats(min_value=5.0, max_value=75.0),
)
def test_g27_matches_a_brute_force_evaluation(meters, delta, rating):
    ids = [f"h{i}" for i in range(len(meters))]
    x = ServiceTransformer("x", rating, tuple(ids))
    ok, _ = flow_checks.check_g27_transformer(x, _members(dict(zip(ids, meters, strict=True))), delta, POLICY)
    now = sum(meters)
    projected = now + delta
    excess = limits.band_excess_kw
    expected = (
        excess(projected, -rating, rating) <= 1e-9
        or excess(projected, -rating, rating) <= excess(now, -rating, rating) + 1e-9
    )
    assert ok == expected


# --- G-28/G-29/G-30 aggregate flows -------------------------------------------------------------------------


def _flow(
    load: float | None, *, age: float = 1.0, lower: float | None = -1000.0, upper: float | None = 5000.0
):
    return AggregateFlow("f1", load, age, lower, upper)


def test_two_banks_individually_safe_jointly_reversing_the_feeder_are_vetoed():
    first = flow_checks.check_aggregate_flow(
        "G-28",
        _flow(200.0),
        0.0,
        -700.0,
        max_age_s=30.0,
        reverse_reason="REV",
        forward_reason="FWD",
        ref="f1",
    )
    second = flow_checks.check_aggregate_flow(
        "G-28",
        _flow(200.0),
        -700.0,
        -1400.0,
        max_age_s=30.0,
        reverse_reason="REV",
        forward_reason="FWD",
        ref="f1",
    )
    assert first.ok and not second.ok and second.reason == "REV"


@pytest.mark.parametrize("flow", [_flow(None), _flow(100.0, age=math.inf), _flow(100.0, lower=None)])
def test_unknown_feeder_flow_or_limit_vetoes_increases_and_passes_relief(flow):
    increase = flow_checks.check_aggregate_flow(
        "G-28", flow, -10.0, -50.0, max_age_s=30.0, reverse_reason="R", forward_reason="F", ref="f1"
    )
    relief = flow_checks.check_aggregate_flow(
        "G-28", flow, -50.0, -10.0, max_age_s=30.0, reverse_reason="R", forward_reason="F", ref="f1"
    )
    assert not increase.ok and increase.reason == "FLOW_UNKNOWN"
    assert relief.ok


@settings(max_examples=300)
@given(
    st.floats(min_value=-2000.0, max_value=6000.0),
    st.lists(st.floats(min_value=-800.0, max_value=800.0), min_size=1, max_size=6),
)
def test_g28_cumulative_cycle_changes_never_leave_the_band_unless_relief(load, bank_deltas):
    cumulative = 0.0
    for delta in bank_deltas:
        outcome = flow_checks.check_aggregate_flow(
            "G-28",
            _flow(load),
            cumulative,
            cumulative + delta,
            max_age_s=30.0,
            reverse_reason="R",
            forward_reason="F",
            ref="f1",
        )
        if outcome.ok:
            excess_after = limits.band_excess_kw(load + cumulative + delta, -1000.0, 5000.0)
            assert (
                excess_after <= limits.band_excess_kw(load + cumulative, -1000.0, 5000.0) + 1e-9
                or excess_after <= 1e-9
            )
            cumulative += delta


def test_a_20_mw_discharge_at_15_mw_load_in_a_territory_is_vetoed():
    territory = AggregateFlow("AUSTIN_ENERGY", 15_000.0, 1.0, 0.0, math.inf)
    outcome = flow_checks.check_aggregate_flow(
        "G-30",
        territory,
        0.0,
        -20_000.0,
        max_age_s=30.0,
        reverse_reason="TERRITORY_EXPORT",
        forward_reason="TERRITORY_EXPORT",
        ref="AUSTIN_ENERGY",
    )
    assert not outcome.ok and outcome.reason == "TERRITORY_EXPORT"


def test_poi_limits_both_directions():
    poi = PoiLimit("sub-1", import_kw=10_000.0, export_kw=20_000.0)
    assert flow_checks.check_g29_poi(poi, -20_000.0).ok
    assert not flow_checks.check_g29_poi(poi, -20_001.0).ok
    assert not flow_checks.check_g29_poi(poi, 10_001.0).ok


# --- G-33 territory (K15) ------------------------------------------------------------------------------------

ZONES = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}


def _g33(market: ObligationMarket | None, zone: str | None, *, headroom: bool = False, access: bool = False):
    it = ProposedItem("hub-1", -3.0, "R", None if headroom else uuid4())
    ref = flow_checks.HEADROOM_MARKET if headroom else flow_checks.obligation_market_ref(market)
    return flow_checks.check_g33_territory(it, ref=ref, zone=zone, zone_territory=ZONES, free_access=access)


def test_an_austin_hub_granted_to_a_cps_obligation_is_vetoed():
    outcome = _g33(ObligationMarket("REGULATED", "CPS_ENERGY", "REGULATED_CAPACITY"), "LZ_AEN")
    assert not outcome.ok and outcome.reason == "R-TERRITORY-OUTSIDE"


def test_a_regulated_obligation_served_from_the_competitive_area_is_vetoed():
    outcome = _g33(ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY"), "LZ_NORTH")
    assert not outcome.ok and outcome.reason == "R-TERRITORY-OUTSIDE"


def test_an_austin_hub_on_free_headroom_without_access_is_vetoed_and_passes_with_access():
    assert _g33(None, "LZ_AEN", headroom=True).reason == "R-TERRITORY-NO-FREE-ACCESS"
    assert _g33(None, "LZ_AEN", headroom=True, access=True).ok


def test_unknown_market_or_zone_fails_closed():
    assert _g33(None, "LZ_AEN").reason == "R-TERRITORY-UNKNOWN"
    assert _g33(ObligationMarket("FREE", None, "ERCOT_ENERGY"), None).reason == "R-TERRITORY-UNKNOWN"
    inconsistent = ObligationMarket("REGULATED", None, "REGULATED_CAPACITY")
    assert _g33(inconsistent, "LZ_AEN").reason == "R-TERRITORY-UNKNOWN"


def test_in_territory_regulated_and_competitive_free_pass():
    assert _g33(ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY"), "LZ_AEN").ok
    assert _g33(ObligationMarket("FREE", None, "ERCOT_ENERGY"), "LZ_NORTH").ok


@settings(max_examples=300)
@given(
    st.sampled_from(["LZ_AEN", "LZ_CPS", "LZ_NORTH", "LZ_HOUSTON", None, "BOGUS"]),
    st.sampled_from(
        [
            ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY"),
            ObligationMarket("REGULATED", "CPS_ENERGY", "REGULATED_CAPACITY"),
            ObligationMarket("FREE", None, "ERCOT_ENERGY"),
            None,
        ]
    ),
    st.booleans(),
)
def test_g33_never_signs_a_cross_territory_grant(zone, market, access):
    outcome = _g33(market, zone, access=access)
    if outcome.ok:
        assert market is not None and zone in {"LZ_AEN", "LZ_CPS", "LZ_NORTH", "LZ_HOUSTON"}
        if market.market == "REGULATED":
            assert ZONES.get(zone) == market.utility_id
        else:
            assert zone not in ZONES or access
