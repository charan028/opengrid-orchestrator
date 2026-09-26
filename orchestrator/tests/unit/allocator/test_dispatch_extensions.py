"""DISPATCH build items 2026-09-26: stored-energy threshold (09 D7, TS-19-15), AS energy hold (Frank #6,
TS-19-36), closed-loop caps (06 S4.a/S4.b), PQ eligibility in real time (06 S5.2), flow limits (09 S1.9)
and K15 territory (09 D2, TS-19-17)."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.allocator import cycle as cycle_module
from opengrid.allocator import energy_hold, flow_limits, reasons
from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    FlowLimits,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    PqDispatchContext,
    PriceSignal,
    Schedule,
)
from opengrid.allocator.pq_eligibility import EligibilityResult, HubEligibilityVerdict
from opengrid.core.limits import derated_power_bounds_kw
from opengrid.core.physics import HubParams
from opengrid.market.territory import FREE, MarketRef

T = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
ETA = 0.9487


def _hub(hub_id: str, bank_id: str = "b1", kw: float = 10.0, **extra: object) -> HubSnapshot:
    # Real homes are 11 kW (1 unit) or 20 kW (2 units); larger aggregate test hubs carry no rating (F1 exempt).
    fields: dict[str, object] = {
        "free_discharge_kw": kw,
        "soc_kwh": 1e6,
        "reserve_kwh": 0.0,
        "e_kwh": 1e6,
        "rated_kw": kw if kw <= 20.0 else None,
        "cell_temp_c": 25.0,
    }
    fields.update(extra)
    return HubSnapshot(hub_id=hub_id, bank_id=bank_id, **fields)  # type: ignore[arg-type]


def _bank(bank_id: str = "b1", cap: float = 100.0, **extra: object) -> BankSnapshot:
    return BankSnapshot(bank_id=bank_id, capability_kw=cap, kva_rating=cap, **extra)  # type: ignore[arg-type]


def _call(oid: str, kw: float, hubs: tuple[str, ...], *, service: str = "PARTNER_CAPACITY", **extra: object):
    return ObligationCall(
        obligation_id=oid,
        bank_id=str(extra.pop("bank_id", "b1")),
        service_type=service,  # type: ignore[arg-type]
        tier="T1",
        committed_kw=kw,
        eligible_hub_ids=hubs,
        **extra,  # type: ignore[arg-type]
    )


def _priced(threshold: float | None, price: float = 500.0, bank_id: str = "b1") -> Schedule:
    return Schedule(prices=(PriceSignal(bank_id, price, threshold_usd_per_mwh=threshold),))


def _headroom(result) -> float:
    return sum(g.granted_kw for g in result.grants if g.is_headroom)


# --- 1. stored-energy threshold (09 D7, TS-19-15) ------------------------------------------------------


def test_no_fixed_threshold_remains_in_the_cycle() -> None:
    assert not hasattr(cycle_module, "_DEFAULT_PRICE_THRESHOLD_USD_PER_MWH")


def test_a_bank_without_a_stored_energy_value_takes_no_headroom() -> None:
    fleet = FleetState(hubs=(_hub("h1", kw=50.0),), banks=(_bank(cap=50.0),))
    result = cycle(T, fleet, LedgerView(calls=()), _priced(None, price=10_000.0), {}, ())
    assert _headroom(result) == 0.0


def test_headroom_follows_the_banks_own_stored_energy_value() -> None:
    fleet = FleetState(hubs=(_hub("h1", kw=50.0),), banks=(_bank(cap=50.0),))
    below = cycle(T, fleet, LedgerView(calls=()), _priced(150.0, price=120.0), {}, ())
    above = cycle(T, fleet, LedgerView(calls=()), _priced(150.0, price=160.0), {}, ())
    assert _headroom(below) == 0.0
    assert _headroom(above) == 50.0


# --- 2. AS energy hold (Frank #6) ------------------------------------------------------------------------


def _as_call(kw: float, *, duration_h: float | None = None, deployed: bool = False) -> ObligationCall:
    return _call("as-1", kw, ("h0",), service="ERCOT_AS", as_deployed=deployed, hold_duration_h=duration_h)


def _energy_hubs(n: int, above_reserve_kwh: float, *, e_kwh: float = 39.2) -> tuple[HubSnapshot, ...]:
    reserve = 0.2 * e_kwh
    return tuple(
        _hub(f"h{i}", kw=10.0, soc_kwh=reserve + above_reserve_kwh, reserve_kwh=reserve, e_kwh=e_kwh)
        for i in range(n)
    )


def test_hold_floor_is_reserve_plus_one_percent_plus_kw_times_duration_over_eta() -> None:
    hubs = _energy_hubs(10, 20.0)  # 200 kWh above reserve
    hold = _as_call(40.0, duration_h=4.0)  # Non-Spin: 160 kWh / eta_d
    excess = 200.0 - 0.01 * 39.2 * 10 - 40.0 * 4.0 / ETA
    lease_s = 30.0
    expected_kw = excess * ETA / (lease_s / 3600.0)
    assert energy_hold.headroom_energy_cap_kw(hubs, [hold], lease_s) == pytest.approx(expected_kw)


def test_no_headroom_when_the_hold_uses_all_the_energy_above_the_floor() -> None:
    hubs = _energy_hubs(10, (40.0 * 4.0 / ETA + 0.01 * 39.2 * 10) / 10)
    assert energy_hold.headroom_energy_cap_kw(hubs, [_as_call(40.0, duration_h=4.0)], 30.0) == pytest.approx(
        0.0, abs=1e-6
    )


def test_hubs_without_live_soc_count_no_energy() -> None:
    stale = tuple(_hub(f"h{i}", soc_kwh=None) for i in range(5))
    assert energy_hold.headroom_energy_cap_kw(stale, [_as_call(1.0)], 30.0) == 0.0


def test_ecrs_default_duration_is_one_hour() -> None:
    assert energy_hold.hold_duration_h(_as_call(10.0)) == energy_hold.DEFAULT_AS_DEPLOYMENT_H == 1.0


def test_cycle_headroom_never_erodes_the_as_hold() -> None:
    """TS-19-36 shape: 350 kWh above reserve, a 100 kW Non-Spin hold (4 h -> 421.6 kWh): no headroom."""
    hubs = _energy_hubs(10, 35.0)
    fleet = FleetState(hubs=hubs, banks=(_bank(cap=100.0),))
    ledger = LedgerView(calls=(_as_call(100.0, duration_h=4.0),))
    result = cycle(T, FleetState(hubs=hubs, banks=(_bank(cap=300.0),)), ledger, _priced(50.0), {}, ())
    assert _headroom(result) == 0.0
    assert result.held == ("as-1",)
    # Enough energy beyond the hold: headroom flows, capped by the energy above the floor over the lease.
    rich = FleetState(hubs=_energy_hubs(10, 100.0), banks=(_bank(cap=300.0),))
    result = cycle(T, rich, ledger, _priced(50.0), {}, (), lease_ttl_s=30.0)
    assert 0.0 < _headroom(result) <= 200.0 + 1e-9
    assert fleet  # unused fixture kept readable


@settings(max_examples=60, deadline=None)
@given(
    above=st.floats(min_value=0.0, max_value=40.0),
    hold_kw=st.floats(min_value=0.0, max_value=100.0),
    duration=st.sampled_from([1.0, 4.0]),
)
def test_property_headroom_over_the_lease_keeps_the_hold(
    above: float, hold_kw: float, duration: float
) -> None:
    hubs = _energy_hubs(10, above)
    lease_s = 30.0
    cap_kw = energy_hold.headroom_energy_cap_kw(hubs, [_as_call(hold_kw, duration_h=duration)], lease_s)
    stored_after = 10 * above - cap_kw * (lease_s / 3600.0) / ETA
    assert stored_after >= 0.01 * 39.2 * 10 + hold_kw * duration / ETA - 1e-6 or cap_kw == 0.0


# --- 5. closed-loop caps (need basis) ----------------------------------------------------------------------


def test_closed_loop_cap_sets_the_grant_with_its_reason_and_keeps_the_rest_idle() -> None:
    fleet = FleetState(hubs=tuple(_hub(f"h{i}") for i in range(10)), banks=(_bank(cap=100.0),))
    ledger = LedgerView(calls=(_call("dc", 60.0, tuple(f"h{i}" for i in range(10)), service="DATA_CENTER"),))
    result = cycle(T, fleet, ledger, _priced(50.0), {}, (), closed_loop_caps={("dc", "b1"): 25.0})
    (grant,) = [g for g in result.grants if g.obligation_id == "dc"]
    assert grant.granted_kw == pytest.approx(25.0)
    assert grant.reason_code == reasons.R_GRANT_CLOSED_LOOP
    assert result.shortfalls == ()
    # The 35 kW the controller left unused is never exported as headroom (K13): only 100 - 60 is.
    assert _headroom(result) == pytest.approx(40.0)


def test_a_zero_closed_loop_setpoint_is_an_explicit_zero_grant() -> None:
    fleet = FleetState(hubs=(_hub("h1"),), banks=(_bank(cap=10.0),))
    ledger = LedgerView(calls=(_call("dc", 10.0, ("h1",), service="DATA_CENTER"),))
    result = cycle(T, fleet, ledger, Schedule(), {}, (), closed_loop_caps={("dc", "b1"): 0.0})
    (grant,) = result.grants
    assert grant.granted_kw == 0.0 and grant.reason_code == reasons.R_GRANT_CLOSED_LOOP


def test_a_closed_loop_cap_at_or_above_the_commitment_changes_nothing() -> None:
    fleet = FleetState(hubs=(_hub("h1", kw=20.0),), banks=(_bank(cap=20.0),))
    ledger = LedgerView(calls=(_call("dc", 10.0, ("h1",), service="DATA_CENTER"),))
    result = cycle(T, fleet, ledger, Schedule(), {}, (), closed_loop_caps={("dc", "b1"): 15.0})
    (grant,) = result.grants
    assert grant.granted_kw == 10.0 and grant.reason_code == reasons.R_GRANT_COMMITTED


def test_a_bank_short_for_the_commitment_but_not_for_the_need_reports_no_shortfall() -> None:
    fleet = FleetState(hubs=(_hub("h1", kw=30.0),), banks=(_bank(cap=30.0),))
    ledger = LedgerView(calls=(_call("dc", 50.0, ("h1",), service="DATA_CENTER"),))
    result = cycle(T, fleet, ledger, Schedule(), {}, (), closed_loop_caps={("dc", "b1"): 20.0})
    assert result.shortfalls == ()
    (grant,) = result.grants
    assert grant.granted_kw == 20.0 and grant.reason_code == reasons.R_GRANT_CLOSED_LOOP


# --- 4. PQ eligibility in real time (S5.2) ------------------------------------------------------------------


def _eligibility(eligible: dict[str, float], excluded: tuple[str, ...] = ()) -> EligibilityResult:
    verdicts = [HubEligibilityVerdict(h, True, diversity_weight=w) for h, w in eligible.items()]
    verdicts += [HubEligibilityVerdict(h, False, reason_code="PQ_ELIGIBILITY_OUTLIER_THD") for h in excluded]
    return EligibilityResult(tuple(sorted(verdicts, key=lambda v: v.hub_id)))


def _pq_fleet() -> FleetState:
    hubs = tuple(_hub(f"h{i}", kw=10.0) for i in range(6))
    return FleetState(hubs=hubs, banks=(_bank(cap=60.0),))


def test_pq_sensitive_obligation_uses_only_eligible_hubs_and_reports_its_allocation() -> None:
    ctx = PqDispatchContext(
        results_by_service={
            "DATA_CENTER": _eligibility({"h0": 1.0, "h1": 1.0, "h2": 1.0}, ("h3", "h4", "h5"))
        }
    )
    ledger = LedgerView(calls=(_call("dc", 20.0, tuple(f"h{i}" for i in range(6)), service="DATA_CENTER"),))
    result = cycle(T, _pq_fleet(), ledger, Schedule(), {}, (), pq=ctx)
    (alloc,) = result.hub_allocations
    assert {h for h, _ in alloc.per_hub_kw} <= {"h0", "h1", "h2"}
    assert sum(kw for _, kw in alloc.per_hub_kw) == pytest.approx(20.0)
    assert result.pq_reductions == ()  # 30 kW eligible >= 20 kW needed


def test_eligibility_below_the_need_is_reported_and_attributed_to_no_substitute() -> None:
    ctx = PqDispatchContext(results_by_service={"DATA_CENTER": _eligibility({"h0": 1.0}, ("h1", "h2"))})
    ledger = LedgerView(calls=(_call("dc", 25.0, ("h0", "h1", "h2"), service="DATA_CENTER"),))
    result = cycle(T, _pq_fleet(), ledger, Schedule(), {}, (), pq=ctx)
    (reduction,) = result.pq_reductions
    assert reduction.capability_before_kw == pytest.approx(30.0)
    assert reduction.capability_after_kw == pytest.approx(10.0)
    assert reduction.excluded_hub_ids == ("h1", "h2")
    (short,) = [s for s in result.shortfalls if s.obligation_id == "dc"]
    assert short.reason_code == reasons.R_COMMIT_LOCK_INFEASIBLE


def test_ladder_excluded_hub_is_substituted_with_a_recorded_event() -> None:
    ctx = PqDispatchContext(
        results_by_service={"DATA_CENTER": _eligibility({"h0": 1.0, "h1": 1.0, "h2": 1.0})},
        excluded_by_obligation={"dc": frozenset({"h0"})},
    )
    ledger = LedgerView(calls=(_call("dc", 15.0, ("h0", "h1", "h2"), service="DATA_CENTER"),))
    result = cycle(T, _pq_fleet(), ledger, Schedule(), {}, (), pq=ctx)
    (alloc,) = result.hub_allocations
    assert "h0" not in {h for h, _ in alloc.per_hub_kw}
    (event,) = result.substitutions
    assert event.from_hub_ids == ("h0",)


def test_phase_balance_prefers_the_under_loaded_phase() -> None:
    hubs = (
        _hub("hA", kw=10.0, p_kw=-9.0),
        _hub("hA2", kw=10.0, p_kw=-9.0),
        _hub("hB", kw=10.0, p_kw=0.0),
        _hub("hC", kw=10.0, p_kw=-9.0),
    )
    ctx = PqDispatchContext(
        results_by_service={"DATA_CENTER": _eligibility(dict.fromkeys(("hA", "hA2", "hB", "hC"), 1.0))},
        phase_by_hub_id={"hA": "A", "hA2": "A", "hB": "B", "hC": "C"},
    )
    ledger = LedgerView(calls=(_call("dc", 12.0, ("hA", "hA2", "hB", "hC"), service="DATA_CENTER"),))
    result = cycle(T, FleetState(hubs=hubs, banks=(_bank(cap=40.0),)), ledger, Schedule(), {}, (), pq=ctx)
    per_hub = dict(result.hub_allocations[0].per_hub_kw)
    assert per_hub["hB"] == max(per_hub.values())


def test_other_obligations_do_not_share_hubs_delivering_a_pq_obligation() -> None:
    ctx = PqDispatchContext(results_by_service={"DATA_CENTER": _eligibility({"h0": 1.0, "h1": 1.0})})
    ledger = LedgerView(
        calls=(
            _call("dc", 15.0, ("h0", "h1"), service="DATA_CENTER"),
            _call("a-firm", 20.0, tuple(f"h{i}" for i in range(6))),
        )
    )
    result = cycle(T, _pq_fleet(), ledger, Schedule(), {}, (), pq=ctx)
    firm = next(g for g in result.grants if g.obligation_id == "a-firm")
    assert firm.granted_kw == pytest.approx(20.0)  # served from h2..h5
    assert {h for h, _ in result.hub_allocations[0].per_hub_kw} <= {"h0", "h1"}


# --- 6. flow limits (09 S1.9) -------------------------------------------------------------------------------


def test_f1_uses_the_guardians_core_derating_including_unknown_temperature() -> None:
    """SAFETY: one formula (`core.limits.derated_power_bounds_kw`); unknown temperature -> factor 0.5."""
    hot = _hub("h1", kw=11.0, soc_kwh=30.0, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=45.0)
    assert flow_limits.derated_discharge_kw(hot) == pytest.approx(7.7)
    unknown = _hub("h2", kw=11.0, soc_kwh=30.0, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=None)
    assert flow_limits.derated_discharge_kw(unknown) == pytest.approx(5.5)
    near_floor = _hub(
        "h3", kw=11.0, soc_kwh=7.84 + 0.05 * 39.2, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=25.0
    )
    assert flow_limits.derated_discharge_kw(near_floor) == pytest.approx(11.0 * 0.65)
    bms = _hub("h4", kw=11.0, soc_kwh=30.0, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=25.0, p_dis_max_kw=6.0)
    assert flow_limits.derated_discharge_kw(bms) == 6.0
    assert flow_limits.derated_discharge_kw(_hub("h5", rated_kw=None)) is None


def test_f1_applies_even_with_flow_limits_off_since_g02_always_does() -> None:
    hub = _hub("h1", kw=11.0, soc_kwh=30.0, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=None)
    assert flow_limits.cap_hub(hub, FlowLimits()).free_discharge_kw == pytest.approx(5.5)
    fleet = FleetState(hubs=(hub,), banks=(_bank(cap=11.0),))
    result = cycle(T, fleet, LedgerView(calls=(_call("o1", 11.0, ("h1",)),)), Schedule(), {}, ())
    (grant,) = result.grants
    assert grant.granted_kw == pytest.approx(5.5)


def test_export_cap_serves_home_load_first() -> None:
    """F2 via `core.limits.home_meter_setpoint_band_kw`: X_exp + (load - the 0.5 kW load-drop allowance)."""
    drop = flow_limits.LOAD_DROP_ALLOWANCE_KW
    hub = _hub("h1", meter_kw=3.0, p_kw=-2.0, export_limit_kw=5.0)  # home load 5 kW
    assert flow_limits.export_cap_kw(hub, None) == pytest.approx(5.0 + 5.0 - drop)
    unknown_load = _hub("h2", export_limit_kw=5.0)  # unknown load counts as 0
    assert flow_limits.export_cap_kw(unknown_load, None) == pytest.approx(5.0 - drop)
    assert flow_limits.export_cap_kw(_hub("h3"), None) is None
    assert flow_limits.export_cap_kw(_hub("h4"), 4.0) == pytest.approx(4.0 - drop)


def test_f2_is_off_with_flow_limits_disabled() -> None:
    hub = _hub("h1", kw=11.0, export_limit_kw=1.0)
    assert flow_limits.cap_hub(hub, FlowLimits()) is hub


def test_cycle_applies_derating_export_and_transformer_caps() -> None:
    hubs = (
        _hub("h0", kw=11.0, cell_temp_c=45.0, xfmr_id="x1"),  # 7.7 kW
        _hub("h1", kw=11.0, export_limit_kw=3.5, xfmr_id="x1"),  # 3.5 - 0.5 load-drop = 3 kW
        _hub("h2", kw=11.0, xfmr_id="x2"),
    )
    limits = FlowLimits(enabled=True, xfmr_kva={"x2": 4.0})
    ledger = LedgerView(calls=(_call("o1", 33.0, ("h0", "h1", "h2")),))
    result = cycle(
        T, FleetState(hubs=hubs, banks=(_bank(cap=33.0),)), ledger, Schedule(), {}, (), flow_limits=limits
    )
    (grant,) = [g for g in result.grants if g.obligation_id == "o1"]
    assert grant.granted_kw == pytest.approx(7.7 + 3.0 + 4.0)
    assert {s.obligation_id for s in result.shortfalls} == {"o1"}


def test_feeder_budget_is_split_across_its_banks_by_capability() -> None:
    banks = (_bank("b1", 60.0, feeder_id="f1"), _bank("b2", 40.0, feeder_id="f1"))
    caps = flow_limits.bank_budget_caps_kw(banks, FlowLimits(enabled=True, feeder_budget_kw={"f1": 50.0}))
    assert caps == {"b1": pytest.approx(30.0), "b2": pytest.approx(20.0)}


@settings(max_examples=100, deadline=None)
@given(
    soc=st.floats(min_value=0.2, max_value=1.0),
    temp=st.one_of(st.none(), st.floats(min_value=-20.0, max_value=60.0)),
    bms=st.one_of(st.none(), st.floats(min_value=0.0, max_value=20.0)),
)
def test_property_allocator_cap_never_exceeds_the_derated_bound(
    soc: float, temp: float | None, bms: float | None
) -> None:
    """TS-19-20 (allocator side): the capped hub never exceeds min(P x f_SoC x f_T, P_BMS)."""
    hub = _hub(
        "h1", kw=11.0, soc_kwh=soc * 39.2, reserve_kwh=7.84, e_kwh=39.2, cell_temp_c=temp, p_dis_max_kw=bms
    )
    capped = flow_limits.cap_hub(hub, FlowLimits(enabled=True))
    params = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
    bound = derated_power_bounds_kw(params, soc * 39.2, temp, bms_discharge_kw=bms).discharge_kw
    assert capped.free_discharge_kw <= bound + 1e-9


# --- 7. K15 territory (two markets) ------------------------------------------------------------------------


AE = MarketRef("REGULATED", "AUSTIN_ENERGY")


def _territory_fleet() -> FleetState:
    return FleetState(
        hubs=(_hub("a1", "ae", kw=50.0), _hub("n1", "oncor", kw=50.0)),
        banks=(
            _bank("ae", 50.0, territory="AUSTIN_ENERGY"),
            _bank("oncor", 50.0, territory="ERCOT_COMPETITIVE"),
        ),
    )


def test_regulated_obligation_is_served_only_inside_its_territory() -> None:
    calls = (
        _call("reg-in", 20.0, ("a1",), bank_id="ae", market_ref=AE),
        _call("reg-out", 20.0, ("n1",), bank_id="oncor", market_ref=AE),
    )
    result = cycle(T, _territory_fleet(), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True)
    served = {g.obligation_id for g in result.grants if g.granted_kw > 0}
    assert served == {"reg-in"}
    (block,) = [b for b in result.territory_blocks if b.obligation_id == "reg-out"]
    assert block.reason_code == "R-TERRITORY-OUTSIDE"
    assert [s.reason_code for s in result.shortfalls if s.obligation_id == "reg-out"] == [
        "R-TERRITORY-OUTSIDE"
    ]


def test_free_headroom_runs_only_where_the_territory_allows_it() -> None:
    schedule = Schedule(
        prices=(
            PriceSignal("ae", 500.0, threshold_usd_per_mwh=50.0),
            PriceSignal("oncor", 500.0, threshold_usd_per_mwh=50.0),
        )
    )
    result = cycle(T, _territory_fleet(), LedgerView(calls=()), schedule, {}, (), enforce_territory=True)
    assert {g.bank_id for g in result.grants if g.is_headroom} == {"oncor"}
    # A utility that grants wholesale access lets its territory banks take FREE work.
    fleet = FleetState(
        hubs=_territory_fleet().hubs,
        banks=(_bank("ae", 50.0, territory="AUSTIN_ENERGY", free_access=True), _territory_fleet().banks[1]),
    )
    result = cycle(T, fleet, LedgerView(calls=()), schedule, {}, (), enforce_territory=True)
    assert {g.bank_id for g in result.grants if g.is_headroom} == {"ae", "oncor"}


def test_unknown_territory_or_market_fails_closed() -> None:
    fleet = FleetState(hubs=(_hub("u1", "u", kw=10.0),), banks=(_bank("u", 10.0),))
    calls = (_call("free", 5.0, ("u1",), bank_id="u", market_ref=FREE),)
    result = cycle(
        T, fleet, LedgerView(calls=calls), _priced(1.0, bank_id="u"), {}, (), enforce_territory=True
    )
    assert [g for g in result.grants if g.granted_kw > 0] == []
    unknown_market = (_call("x", 5.0, ("n1",), bank_id="oncor", market_ref=None),)
    result = cycle(
        T, _territory_fleet(), LedgerView(calls=unknown_market), Schedule(), {}, (), enforce_territory=True
    )
    (block,) = [b for b in result.territory_blocks if b.obligation_id == "x"]
    assert block.reason_code == "R-TERRITORY-UNKNOWN"


@settings(max_examples=50, deadline=None)
@given(
    territories=st.lists(
        st.sampled_from(["AUSTIN_ENERGY", "CPS_ENERGY", "ERCOT_COMPETITIVE"]), min_size=1, max_size=4
    ),
    markets=st.lists(st.sampled_from(["AE", "CPS", "FREE"]), min_size=1, max_size=4),
)
def test_property_regulated_grants_stay_inside_the_territory(
    territories: list[str], markets: list[str]
) -> None:
    """TS-19-17: the allocator's grants for REG obligations come only from banks in their territory."""
    refs = {"AE": AE, "CPS": MarketRef("REGULATED", "CPS_ENERGY"), "FREE": FREE}
    banks = tuple(_bank(f"b{i}", 10.0, territory=t) for i, t in enumerate(territories))
    hubs = tuple(_hub(f"h{i}", f"b{i}", kw=10.0) for i in range(len(banks)))
    calls = tuple(
        _call(f"o{i}-{j}", 1.0, (f"h{i}",), bank_id=f"b{i}", market_ref=refs[m])
        for i in range(len(banks))
        for j, m in enumerate(markets)
    )
    result = cycle(
        T,
        FleetState(hubs=hubs, banks=banks),
        LedgerView(calls=calls),
        Schedule(),
        {},
        (),
        enforce_territory=True,
    )
    territory_of = {b.bank_id: b.territory for b in banks}
    market_of = {c.obligation_id: c.market_ref for c in calls}
    for grant in result.grants:
        ref = market_of[grant.obligation_id or ""]
        if grant.granted_kw > 0 and ref is not None and ref.is_regulated:
            assert territory_of[grant.bank_id] == ref.utility_id
        if grant.granted_kw > 0 and ref == FREE:
            assert territory_of[grant.bank_id] == "ERCOT_COMPETITIVE"


# --- performance: everything on, 2,000 hubs ----------------------------------------------------------------


def test_cycle_with_every_extension_on_stays_under_500ms_at_2000_hubs(record_property) -> None:
    """p99 < 500 ms at 2,000 hubs with closed-loop caps, PQ eligibility, territory and flow limits all on.
    Measured as the process's CPU time per cycle: on the shared build host other agents' suites saturate
    the CPU, and wall time there measures the queue, not this code (the production engine host is not
    shared). Wall time is recorded beside it."""
    n_banks, per_bank = 40, 50
    hubs, banks, calls, prices = [], [], [], []
    verdicts = []
    phases = {}
    for b in range(n_banks):
        bank_id = f"bank-{b}"
        ids = []
        for i in range(per_bank):
            hub_id = f"hub-{b}-{i}"
            ids.append(hub_id)
            hubs.append(
                _hub(
                    hub_id,
                    bank_id,
                    kw=10.0,
                    soc_kwh=30.0,
                    reserve_kwh=7.84,
                    e_kwh=39.2,
                    cell_temp_c=30.0,
                    meter_kw=1.0,
                    p_kw=-1.0,
                    export_limit_kw=9.0,
                    xfmr_id=f"x-{b}-{i // 5}",
                )
            )
            verdicts.append(HubEligibilityVerdict(hub_id, i % 3 != 0, diversity_weight=1.0))
            phases[hub_id] = "ABC"[i % 3]
        banks.append(_bank(bank_id, 400.0, feeder_id=f"f{b // 5}", territory="ERCOT_COMPETITIVE"))
        prices.append(PriceSignal(bank_id, 200.0, threshold_usd_per_mwh=90.0))
        calls.append(_call(f"dc-{b}", 80.0, tuple(ids), service="DATA_CENTER", bank_id=bank_id))
        calls.append(_call(f"firm-{b}", 120.0, tuple(ids), bank_id=bank_id))
        calls.append(
            _call(f"as-{b}", 50.0, tuple(ids), service="ERCOT_AS", bank_id=bank_id, hold_duration_h=4.0)
        )
    ctx = PqDispatchContext(
        results_by_service={
            "DATA_CENTER": EligibilityResult(tuple(sorted(verdicts, key=lambda v: v.hub_id)))
        },
        phase_by_hub_id=phases,
        excluded_by_obligation={"dc-0": frozenset({"hub-0-1"})},
    )
    limits = FlowLimits(
        enabled=True,
        xfmr_kva={f"x-{b}-{k}": 40.0 for b in range(n_banks) for k in range(per_bank // 5)},
        feeder_budget_kw={f"f{k}": 1500.0 for k in range(n_banks // 5)},
    )
    caps = {(f"dc-{b}", f"bank-{b}"): 60.0 for b in range(n_banks)}
    fleet, ledger, schedule = (
        FleetState(tuple(hubs), tuple(banks)),
        LedgerView(tuple(calls)),
        Schedule(tuple(prices)),
    )

    def _run() -> None:
        cycle(
            T,
            fleet,
            ledger,
            schedule,
            {},
            (),
            closed_loop_caps=caps,
            pq=ctx,
            enforce_territory=True,
            flow_limits=limits,
        )

    _run()
    samples: list[float] = []
    wall: list[float] = []
    for _ in range(20):
        start, wall_start = time.process_time(), time.perf_counter()
        _run()
        samples.append((time.process_time() - start) * 1000.0)
        wall.append((time.perf_counter() - wall_start) * 1000.0)
    samples.sort()
    p99 = samples[math.ceil(0.99 * len(samples)) - 1]
    record_property("cycle_all_extensions_p99_ms_2000_hubs", p99)
    record_property("cycle_all_extensions_wall_max_ms_2000_hubs", max(wall))
    assert p99 < 500.0


def test_a_territory_blocked_obligation_gets_a_zero_kw_grant_with_the_k15_reason() -> None:
    """G-19 (review fix): an omitted obligation reads as an unexplained reduction to 0."""
    calls = (_call("reg-out", 20.0, ("n1",), bank_id="oncor", market_ref=AE),)
    result = cycle(T, _territory_fleet(), LedgerView(calls=calls), Schedule(), {}, (), enforce_territory=True)
    (grant,) = [g for g in result.grants if g.obligation_id == "reg-out"]
    assert grant.granted_kw == 0.0 and grant.reason_code == "R-TERRITORY-OUTSIDE"


def test_hub_units_come_from_the_registry_else_from_the_rating() -> None:
    assert flow_limits.hub_units(20.0, None) == 2
    assert flow_limits.hub_units(11.0, None) == 1
    assert flow_limits.hub_units(20.0, 2) == 2
    dual = _hub("d1", kw=20.0, soc_kwh=60.0, reserve_kwh=15.68, e_kwh=78.4, cell_temp_c=25.0)
    assert flow_limits.derated_discharge_kw(dual) == pytest.approx(20.0)  # not a single unit's 11 kW
    single_registered = _hub(
        "d2", kw=20.0, soc_kwh=60.0, reserve_kwh=15.68, e_kwh=78.4, cell_temp_c=25.0, units=1
    )
    assert flow_limits.derated_discharge_kw(single_registered) == pytest.approx(11.0)


def test_headroom_stops_at_the_selectors_hard_floor_and_the_as_floor_whichever_is_higher() -> None:
    hubs = _energy_hubs(10, 20.0)  # 10 x (7.84 + 20) kWh = 278.4 kWh stored
    lease_s = 30.0
    to_kw = ETA / (lease_s / 3600.0)
    # Plan floor alone (no AS award): stored - floor.
    assert energy_hold.headroom_energy_cap_kw(hubs, [], lease_s, plan_floor_kwh=270.0) == pytest.approx(
        8.4 * to_kw
    )
    # Plan floor above the stored energy: none.
    assert energy_hold.headroom_energy_cap_kw(hubs, [], lease_s, plan_floor_kwh=300.0) == 0.0
    # AS floor higher than the plan floor: the AS floor binds.
    hold = _as_call(40.0, duration_h=4.0)
    as_only = energy_hold.headroom_energy_cap_kw(hubs, [hold], lease_s)
    assert energy_hold.headroom_energy_cap_kw(hubs, [hold], lease_s, plan_floor_kwh=100.0) == pytest.approx(
        as_only
    )
    # In the cycle: the schedule's published floor caps headroom.
    fleet = FleetState(hubs=hubs, banks=(_bank(cap=100.0),))
    schedule = Schedule(
        prices=(PriceSignal("b1", 500.0, threshold_usd_per_mwh=50.0),), hold_floor_kwh={"b1": 278.4}
    )
    assert _headroom(cycle(T, fleet, LedgerView(calls=()), schedule, {}, ())) == 0.0


def test_a_called_award_holds_energy_only_for_the_rest_of_its_call() -> None:
    """Review R3 (allocator side): held -> full duration; called -> remaining deployment, capped."""
    held = _as_call(40.0, duration_h=4.0)
    called = ObligationCall(
        "as-1",
        "b1",
        "ERCOT_AS",
        "T1",
        40.0,
        ("h0",),
        as_deployed=True,
        hold_duration_h=4.0,
        deployment_remaining_h=0.5,
    )
    long_call = ObligationCall(
        "as-2",
        "b1",
        "ERCOT_AS",
        "T1",
        40.0,
        ("h0",),
        as_deployed=True,
        hold_duration_h=1.0,
        deployment_remaining_h=3.0,
    )
    assert energy_hold.hold_duration_h(held) == 4.0
    assert energy_hold.hold_duration_h(called) == 0.5
    assert energy_hold.hold_duration_h(long_call) == 1.0
    hubs = _energy_hubs(10, 20.0)
    assert energy_hold.headroom_energy_cap_kw(hubs, [called], 30.0) > energy_hold.headroom_energy_cap_kw(
        hubs, [held], 30.0
    )


def test_a_shortfall_caused_by_an_operator_target_carries_the_operator_override() -> None:
    """R-OPERATOR-OVERRIDE: a live manual target took hubs the obligation needed on this bank."""
    from opengrid.core.reasons import R_OPERATOR_OVERRIDE

    hubs = tuple(_hub(f"h{i}", kw=10.0) for i in range(3))
    fleet = FleetState(hubs=hubs, banks=(_bank(cap=30.0),))
    ledger = LedgerView(calls=(_call("o1", 30.0, ("h0", "h1", "h2")),))
    result = cycle(
        T,
        fleet,
        ledger,
        Schedule(),
        {},
        (),
        excluded_hub_ids=frozenset({"h0"}),
        operator_hub_ids=frozenset({"h0"}),
    )
    (grant,) = result.grants
    assert grant.granted_kw == pytest.approx(20.0) and grant.reason_code == R_OPERATOR_OVERRIDE
    assert {s.reason_code for s in result.shortfalls} == {R_OPERATOR_OVERRIDE}
    # A veto exclusion alone is not an operator override.
    vetoed = cycle(T, fleet, ledger, Schedule(), {}, (), excluded_hub_ids=frozenset({"h0"}))
    assert vetoed.grants[0].reason_code != R_OPERATOR_OVERRIDE
