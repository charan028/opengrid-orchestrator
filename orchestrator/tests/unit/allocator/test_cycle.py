"""TS-05: the full S1-S7 cycle (02a S5.2). Scenario tests with multiple concurrent obligations across
services, plus property tests for K1, K4, K5, K9, K13.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.allocator import reasons
from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    PriceSignal,
    ScadaSample,
    Schedule,
)

_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def _hub(hub_id: str, bank_id: str, kw: float, health: str = "OK") -> HubSnapshot:
    return HubSnapshot(hub_id=hub_id, bank_id=bank_id, free_discharge_kw=kw, health=health)


def _bank(bank_id: str, cap: float, **kw) -> BankSnapshot:
    return BankSnapshot(bank_id=bank_id, capability_kw=cap, kva_rating=cap, **kw)


def _call(
    oid: str, bank_id: str, tier: str, kw: float, service: str, hubs: tuple[str, ...]
) -> ObligationCall:
    return ObligationCall(
        obligation_id=oid,
        bank_id=bank_id,
        service_type=service,
        tier=tier,
        committed_kw=kw,
        eligible_hub_ids=hubs,
    )


def test_ts_05_50_multiple_concurrent_obligations_across_services_one_bank() -> None:
    """N concurrent obligations across services on the same bank (BUILD.md S2's first-class
    requirement): each gets its committed floor up to bank capability, in tier order.
    """
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 30.0), _hub("h2", "b1", 30.0), _hub("h3", "b1", 30.0), _hub("h4", "b1", 30.0)),
        banks=(_bank("b1", 100.0),),
    )
    ledger = LedgerView(
        calls=(
            _call("o-dist", "b1", "T1", 40.0, "DIST_DEFERRAL", ("h1", "h2")),
            _call("o-as", "b1", "T2", 30.0, "ERCOT_AS", ("h3",)),
            _call("o-energy", "b1", "T3", 40.0, "ERCOT_ENERGY", ("h4",)),
        )
    )
    result = cycle(_T0, fleet, ledger, Schedule(), {}, ())

    by_ob = {g.obligation_id: g.granted_kw for g in result.grants if not g.is_headroom}
    assert by_ob["o-dist"] == 40.0
    assert by_ob["o-as"] == 30.0
    assert by_ob["o-energy"] == 30.0  # only 30 left of the 100 kW bank after T1+T2
    shortfall = next(s for s in result.shortfalls if s.obligation_id == "o-energy")
    assert shortfall.shortfall_kw == 10.0


def test_ts_05_51_k13_substitution_on_hub_loss_keeps_full_delivery() -> None:
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 20.0, health="FAULT"), _hub("h2", "b1", 20.0), _hub("h3", "b1", 20.0)),
        banks=(_bank("b1", 100.0),),
    )
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 30.0, "DIST_DEFERRAL", ("h1", "h2", "h3")),))
    result = cycle(_T0, fleet, ledger, Schedule(), {}, ())

    grant = next(g for g in result.grants if g.obligation_id == "o1")
    assert grant.granted_kw == 30.0  # fully realized despite h1's fault
    assert len(result.substitutions) == 1
    assert result.substitutions[0].from_hub_ids == ("h1",)
    assert result.shortfalls == ()


def test_ts_05_52_k5_l2_block_instruction_zeroes_bank_capability() -> None:
    fleet = FleetState(hubs=(_hub("h1", "b1", 50.0),), banks=(_bank("b1", 100.0),))
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 40.0, "DIST_DEFERRAL", ("h1",)),))
    instructions = (Instruction(scope="BANK", scope_ref="b1", kind="BLOCK"),)
    result = cycle(_T0, fleet, ledger, Schedule(), {}, instructions)

    assert not any(not g.is_headroom for g in result.grants)  # no committed grant survives BLOCK
    shortfall = next(s for s in result.shortfalls if s.obligation_id == "o1")
    assert shortfall.shortfall_kw == 40.0
    assert shortfall.reason_code == reasons.R_COMMIT_LOCK_OVERRIDE_L2


def test_ts_05_53_k5_l2_limit_caps_capability() -> None:
    fleet = FleetState(hubs=(_hub("h1", "b1", 50.0),), banks=(_bank("b1", 100.0),))
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 40.0, "DIST_DEFERRAL", ("h1",)),))
    instructions = (Instruction(scope="BANK", scope_ref="b1", kind="LIMIT", limit_kw=10.0),)
    result = cycle(_T0, fleet, ledger, Schedule(), {}, instructions)

    grant = next(g for g in result.grants if g.obligation_id == "o1")
    assert grant.granted_kw == 10.0


def test_ts_05_54_headroom_scheduled_when_price_clears_threshold() -> None:
    fleet = FleetState(hubs=(_hub("h1", "b1", 50.0),), banks=(_bank("b1", 100.0),))
    ledger = LedgerView(calls=())
    schedule = Schedule(prices=(PriceSignal(bank_id="b1", price_usd_per_mwh=100.0),))
    result = cycle(_T0, fleet, ledger, schedule, {}, (), price_threshold_usd_per_mwh=30.0)

    headroom_grants = [g for g in result.grants if g.is_headroom]
    assert len(headroom_grants) == 1
    assert headroom_grants[0].granted_kw == 100.0
    assert headroom_grants[0].obligation_id is None


def test_ts_05_55_dist_deferral_pi_folds_into_the_committed_grant() -> None:
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 100.0),),
        banks=(_bank("b1", 100.0, reserve_kva=0.0),),
    )
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 10.0, "DIST_DEFERRAL", ("h1",)),))
    scada = {"b1": ScadaSample(bank_id="b1", apparent_power_kva=90.0, sigma_n=1.0, sigma_x=1.0)}
    pi_states: dict = {}
    result = cycle(_T0, fleet, ledger, Schedule(), scada, (), pi_states=pi_states)

    grant = next(g for g in result.grants if g.obligation_id == "o1")
    assert grant.granted_kw >= 10.0  # never below the frozen commitment
    assert "b1" in pi_states  # K9: the integrator persists across cycles


def test_ts_05_56_no_flapping_across_consecutive_cycles_within_dwell() -> None:
    fleet = FleetState(hubs=(_hub("h1", "b1", 50.0),), banks=(_bank("b1", 100.0),))
    ledger = LedgerView(calls=())
    schedule = Schedule(prices=(PriceSignal(bank_id="b1", price_usd_per_mwh=100.0),))
    dwell_states: dict = {}
    r1 = cycle(_T0, fleet, ledger, schedule, {}, (), dwell_states=dwell_states)
    assert r1.grants[0].granted_kw == 100.0

    # Price crashes on the very next 2 s tick; dwell keeps the schedule ON.
    from datetime import timedelta

    schedule_low = Schedule(prices=(PriceSignal(bank_id="b1", price_usd_per_mwh=0.0),))
    r2 = cycle(_T0 + timedelta(seconds=2), fleet, ledger, schedule_low, {}, (), dwell_states=dwell_states)
    assert r2.grants[0].granted_kw == 100.0


def test_ts_05_58_l2_fleet_scope_applies_to_every_bank() -> None:
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 50.0), _hub("h2", "b2", 50.0)),
        banks=(_bank("b1", 100.0), _bank("b2", 100.0)),
    )
    ledger = LedgerView(
        calls=(
            _call("o1", "b1", "T1", 40.0, "DIST_DEFERRAL", ("h1",)),
            _call("o2", "b2", "T1", 40.0, "DIST_DEFERRAL", ("h2",)),
        )
    )
    instructions = (Instruction(scope="FLEET", scope_ref="FLEET", kind="ESTOP"),)
    result = cycle(_T0, fleet, ledger, Schedule(), {}, instructions)
    assert not any(not g.is_headroom for g in result.grants)
    assert {s.obligation_id for s in result.shortfalls} == {"o1", "o2"}
    assert all(s.reason_code == reasons.R_COMMIT_LOCK_OVERRIDE_L2 for s in result.shortfalls)


def test_ts_05_58b_l2_zone_scope_only_touches_that_zones_banks() -> None:
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 50.0), _hub("h2", "b2", 50.0)),
        banks=(_bank("b1", 100.0, zone="north"), _bank("b2", 100.0, zone="south")),
    )
    ledger = LedgerView(
        calls=(
            _call("o1", "b1", "T1", 40.0, "DIST_DEFERRAL", ("h1",)),
            _call("o2", "b2", "T1", 40.0, "DIST_DEFERRAL", ("h2",)),
        )
    )
    instructions = (Instruction(scope="ZONE", scope_ref="north", kind="BLOCK"),)
    result = cycle(_T0, fleet, ledger, Schedule(), {}, instructions)
    by_ob = {g.obligation_id: g.granted_kw for g in result.grants if not g.is_headroom}
    assert "o1" not in by_ob  # north (b1) blocked
    assert by_ob["o2"] == 40.0  # south (b2) unaffected


def test_ts_05_59_pi_relief_draws_from_headroom_beyond_tier_grant() -> None:
    """When SCADA indicates more relief is needed than the frozen commitment, the PI loop's extra
    output is drawn from remaining bank headroom and tagged with its own grant reason. The PI's
    output ramps up (K4), so this drives several cycles to let it clear the committed floor."""
    fleet = FleetState(hubs=(_hub("h1", "b1", 100.0),), banks=(_bank("b1", 100.0, reserve_kva=0.0),))
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 5.0, "DIST_DEFERRAL", ("h1",)),))
    scada = {"b1": ScadaSample(bank_id="b1", apparent_power_kva=150.0, sigma_n=1.0, sigma_x=1.0)}
    pi_states: dict = {}
    grant = None
    for _ in range(20):
        result = cycle(_T0, fleet, ledger, Schedule(), scada, (), pi_states=pi_states)
        grant = next(g for g in result.grants if g.obligation_id == "o1")
    assert grant is not None
    assert grant.granted_kw > 5.0
    assert grant.reason_code == reasons.R_GRANT_DIST_DEFERRAL_PI


def test_ts_05_59b_substitution_shortfall_propagates_to_cycle_result() -> None:
    fleet = FleetState(
        hubs=(_hub("h1", "b1", 10.0, health="FAULT"), _hub("h2", "b1", 5.0)),
        banks=(_bank("b1", 100.0),),
    )
    ledger = LedgerView(calls=(_call("o1", "b1", "T1", 15.0, "DIST_DEFERRAL", ("h1", "h2")),))
    result = cycle(_T0, fleet, ledger, Schedule(), {}, ())
    shortfall = next(s for s in result.shortfalls if s.obligation_id == "o1")
    assert shortfall.reason_code == reasons.R_COMMIT_LOCK_INFEASIBLE
    assert shortfall.shortfall_kw == 10.0


@given(
    demands=st.lists(st.floats(min_value=0, max_value=200, allow_nan=False), min_size=1, max_size=4),
    cap=st.floats(min_value=0, max_value=500, allow_nan=False),
)
@settings(max_examples=50)
def test_ts_05_57_property_reserve_and_bank_limits_never_breached(demands: list[float], cap: float) -> None:
    """K1 (via hub-safe capability figures) and K4 (bank capability) are never exceeded by any
    combination of concurrent committed demands."""
    hub_ids = tuple(f"h{i}" for i in range(len(demands)))
    hubs = tuple(_hub(hid, "b1", cap / max(len(demands), 1) + 1000.0) for hid in hub_ids)  # ample per-hub cap
    fleet = FleetState(hubs=hubs, banks=(_bank("b1", cap),))
    calls = tuple(
        _call(f"o{i}", "b1", "T1", demand, "DIST_DEFERRAL", (hid,))
        for i, (demand, hid) in enumerate(zip(demands, hub_ids, strict=True))
    )
    ledger = LedgerView(calls=calls)
    result = cycle(_T0, fleet, ledger, Schedule(), {}, ())

    total_committed_granted = sum(g.granted_kw for g in result.grants if not g.is_headroom)
    assert total_committed_granted <= cap + 1e-6
