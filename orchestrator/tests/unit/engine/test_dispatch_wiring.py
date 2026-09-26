"""DISPATCH engine wiring 2026-09-26: settings (activation gate, feature flags), stored-energy threshold,
contract markets and AS durations in the ledger view, closed-loop runner, S5.4 ladder execution, cycle
extras traces, PQ hub items, and the flow-limit registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from opengrid import engine
from opengrid.allocator import reasons
from opengrid.allocator.models import (
    CycleResult,
    FleetState,
    FlowLimits,
    HubAllocation,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    PqCapabilityReduction,
    PqDispatchContext,
    ProposedGrant,
    TerritoryBlock,
)
from opengrid.allocator.pq_monitor import CorrectionActionType
from opengrid.core.models.engine import Grant
from opengrid.core.pq import PqEnvelopeLimits, PqMeasurement
from opengrid.engine import gateways as gw
from opengrid.engine.closed_loop import ClosedLoopRunner, ClosedLoopSpec, SiteSignals, spec_from_row
from opengrid.engine.dispatch_extras import PQ_CAPABILITY_REDUCED, TERRITORY_BLOCK, EngineCycleExtras
from opengrid.engine.flow_topology import topology_limits
from opengrid.engine.pq_ladder import R_PQ_DRIFT_RECOVERED, PqLadderExecutor
from opengrid.engine.settings import DispatchSettings, dispatch_settings
from opengrid.market.territory import FREE, MarketRef
from opengrid.platform.config import Config
from opengrid.site_ingest import FeedbackValue

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=1.5,
    voltage_band_pct=2.0,
    freq_tolerance_hz=0.5,
    pf_min=0.95,
    thd_voltage_limit_pct=2.5,
    thd_current_limit_pct=2.5,
)
GOOD = PqMeasurement(0.1, 0.1, 0.01, 0.99, 0.5, 0.5)
BAD = PqMeasurement(5.0, 0.1, 0.01, 0.99, 0.5, 0.5)  # imbalance 5 % vs a 1.5 % limit


@dataclass
class FakeTrace:
    appended: list[tuple[str, str, str, dict[str, Any], list[str] | None]] = field(default_factory=list)

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
        self.appended.append((stream_id, decision_type, event_class, payload, reason_codes))
        return None

    def classes(self) -> list[str]:
        return [a[2] for a in self.appended]


# --- 3. settings: the activation gate and the feature flags ---------------------------------------------


def test_activation_gate_and_flags_default_off_and_read_from_config() -> None:
    off = dispatch_settings(Config({}))
    assert (off.data_center_activation, off.site_ingest_enabled, off.closed_loop_enabled) == (
        False,
        False,
        False,
    )
    assert off.enforce_territory is True and off.flow_limits.enabled is True
    on = dispatch_settings(
        Config(
            {
                "contracts": {"activation": {"data_center": True}},
                "site_ingest": {"enabled": True},
                "allocator": {
                    "closed_loop": {"enabled": True, "data_center": {"target_import_kw": {"site-1": 400}}},
                    "wear_usd_per_kwh": 0.02,
                    "flow_limits": {"enabled": False, "xfmr_kva": {"x1": 50}},
                },
            }
        )
    )
    assert on.data_center_activation and on.site_ingest_enabled and on.closed_loop_enabled
    assert on.dc_target_import_kw == {"site-1": 400.0}
    assert on.wear_usd_per_kwh == 0.02
    assert on.flow_limits == FlowLimits(enabled=False, xfmr_kva={"x1": 50.0})


def test_engine_main_passes_the_activation_setting_to_contracts() -> None:
    import inspect

    source = inspect.getsource(engine.main)
    assert "data_center_activation_enabled=settings.data_center_activation" in source


# --- 1. stored-energy threshold ------------------------------------------------------------------------


class _Cursor:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[str, Any]] = []
        self._current: Any = None

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responses.pop(0) if self._responses else None

    async def fetchall(self):
        return self._current or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, cursor: _Cursor) -> None:
        self._conn = _Conn(cursor)

    def connection(self):
        return self._conn


@pytest.mark.asyncio
async def test_threshold_is_the_optimizers_value_plus_wear_else_the_replacement_cost(monkeypatch) -> None:
    monkeypatch.setattr(gw, "_bank_zone", lambda bank_id: "LZ_NORTH")

    async def reader(bank_ids, now):
        return {"bank-a": 120.0, "bank-b": float("nan")}

    cursor = _Cursor([[("LZ_NORTH", 40.0)], [], [("LZ_NORTH", 20.0)]])
    schedule_gw = gw.EngineScheduleGateway(_Pool(cursor), stored_energy_value=reader, wear_usd_per_kwh=0.03)
    schedule = await schedule_gw.schedule(["bank-a", "bank-b"])
    by_bank = {p.bank_id: p.threshold_usd_per_mwh for p in schedule.prices}
    assert by_bank["bank-a"] == pytest.approx(120.0)  # the optimizer's break-even (wear included)
    # No usable value (NaN): recharge cost + the zone's M1, over round-trip efficiency, plus wear.
    assert by_bank["bank-b"] == pytest.approx((20.0 + 60.295) / (0.9487**2) + 30.0, rel=1e-3)


@pytest.mark.asyncio
async def test_an_unreadable_optimizer_falls_back_for_every_bank(monkeypatch) -> None:
    monkeypatch.setattr(gw, "_bank_zone", lambda bank_id: "LZ_NORTH")

    async def broken(bank_ids, now):
        raise RuntimeError("selector down")

    cursor = _Cursor([[("LZ_NORTH", 40.0)], []])
    schedule = await gw.EngineScheduleGateway(_Pool(cursor), stored_energy_value=broken).schedule(["b"])
    assert schedule.prices[0].threshold_usd_per_mwh is not None


def test_wear_uses_the_core_wear_formula() -> None:
    assert gw.wear_usd_per_mwh(0.03) == pytest.approx(30.0)


def test_m1_table_comes_from_the_tariff_file_with_no_m1_in_regulated_zones() -> None:
    table = gw.m1_usd_per_mwh_by_zone(NOW.date(), config_path="config/orchestrator.toml")
    assert table["LZ_NORTH"] == pytest.approx(60.295)
    assert table["LZ_HOUSTON"] == pytest.approx(64.130)
    assert table["LZ_AEN"] == 0.0 and table["LZ_CPS"] == 0.0


# --- 2 / 7. ledger view: AS duration and contract market -----------------------------------------------


@pytest.mark.asyncio
async def test_ledger_view_carries_the_as_duration_and_the_contract_market(monkeypatch) -> None:
    oid_as, oid_reg, oid_bad = str(uuid4()), str(uuid4()), str(uuid4())
    monkeypatch.setattr(gw.fleet, "hub_capabilities", lambda bank_id: [])
    rows = [
        (oid_as, "b1", Decimal("40"), "ERCOT_AS", "T2", None, "COMMITTED", False, 240),
        (oid_reg, "b1", Decimal("10"), "PARTNER_CAPACITY", "T1", None, "DELIVERING", False, None),
        (oid_bad, "b1", Decimal("10"), "PARTNER_CAPACITY", "T1", None, "DELIVERING", False, None),
    ]
    markets = [
        (oid_reg, "REGULATED", "AUSTIN_ENERGY", "PARTNER_CAPACITY"),
        (oid_bad, "REGULATED", None, "PARTNER_CAPACITY"),  # inconsistent: unknown market
    ]
    view = await gw.EngineLedgerGateway(_Pool(_Cursor([rows, [], markets]))).ledger_view(["b1"], NOW)
    calls = {c.obligation_id: c for c in view.calls}
    assert calls[oid_as].hold_duration_h == 4.0
    assert calls[oid_as].market_ref == FREE
    assert calls[oid_reg].market_ref == MarketRef("REGULATED", "AUSTIN_ENERGY")
    assert calls[oid_bad].market_ref is None


# --- 5. closed-loop runner ------------------------------------------------------------------------------


def _signals(value: float | None, *, quality: str = "GOOD", age_s: float = 0.5) -> SiteSignals:
    def resolve(ref, *, customer_id, max_age_s, now):
        if value is None:
            return None
        return FeedbackValue(ref, value, now, age_s, quality, max_age_s)  # type: ignore[arg-type]

    return SiteSignals(resolve=resolve, latest_site=lambda c, s: None, latest_corridor=lambda c, s: None)


def _dc_spec(oid: str = "dc-1") -> ClosedLoopSpec:
    return ClosedLoopSpec(oid, "DATA_CENTER", "cust-1", "site_meter:site-1:p_kw", LIMITS)


def _dc_fleet_and_view(oid: str = "dc-1") -> tuple[FleetState, LedgerView]:
    hubs = tuple(HubSnapshot(f"h{i}", "b1", 50.0) for i in range(4))
    call = ObligationCall(oid, "b1", "DATA_CENTER", "T1", 100.0, tuple(h.hub_id for h in hubs))
    return FleetState(hubs=hubs, banks=()), LedgerView(calls=(call,))


def _runner(signals: SiteSignals, *, control: bool = True, target: float | None = 400.0) -> ClosedLoopRunner:
    async def load(now):
        return [_dc_spec()]

    settings = DispatchSettings(dc_target_import_kw={"site-1": target} if target is not None else {})
    return ClosedLoopRunner(load, settings, dc_profile=None, signals=signals, control_enabled=control)


@pytest.mark.asyncio
async def test_closed_loop_tracks_the_site_meter_inside_the_commitment() -> None:
    fleet_state, view = _dc_fleet_and_view()
    # Site imports 420 kW against a 400 kW target: the fleet delivers more than its feed-forward, capped at 100.
    runner = _runner(_signals(420.0))
    caps = await runner.caps(fleet_state, view, NOW)
    assert caps[("dc-1", "b1")] == pytest.approx(100.0)
    # Importing less than the target: the fleet backs off (need basis), ramp-limited from 100 kW.
    runner_low = _runner(_signals(300.0))
    caps_low = await runner_low.caps(fleet_state, view, NOW)
    assert caps_low[("dc-1", "b1")] < 100.0
    assert runner_low.mode_changes[0].mode == "TRACKING"


@pytest.mark.asyncio
async def test_closed_loop_signal_loss_holds_and_control_off_returns_no_caps() -> None:
    fleet_state, view = _dc_fleet_and_view()
    runner = _runner(_signals(None))
    caps = await runner.caps(fleet_state, view, NOW)
    assert caps[("dc-1", "b1")] == pytest.approx(100.0)  # HOLD the delivered level
    assert runner.mode_changes[0].mode == "HOLD"
    off = _runner(_signals(300.0), control=False)
    assert await off.caps(fleet_state, view, NOW) == {}


def test_reconcile_holds_the_level_actually_granted() -> None:
    runner = _runner(_signals(None))
    runner._dc_states["dc-1"] = runner._dc_states.get("dc-1") or __import__(
        "opengrid.allocator.closed_loop_data_center", fromlist=["DataCenterState"]
    ).DataCenterState(output_kw=100.0)
    runner.reconcile(CycleResult("c", grants=(ProposedGrant("b1", 60.0, "dc-1"),)))
    assert runner._dc_states["dc-1"].output_kw == 60.0


def test_spec_row_without_an_envelope_has_no_limits() -> None:
    row = ("o", "DATA_CENTER", "c", "site_meter:s:p_kw", None, None, None, None, None, None, None)
    assert spec_from_row(row).limits is None
    full = spec_from_row(("o", "PIPELINE_AC", "c", None, 1.5, 2.0, 0.5, 0.95, 2.5, 2.5, 80.0))
    assert full.limits is not None and full.limits.current_limit_a == 80.0


# --- 4. S5.4 ladder execution ------------------------------------------------------------------------------


def _ladder(trace: FakeTrace, flags: list[tuple[Any, ...]], calib: list[str]) -> PqLadderExecutor:
    async def set_at_risk(oid, at_risk, reason, payload):
        flags.append((str(oid), at_risk, reason))

    async def request_calibration(hub_id, now):
        calib.append(hub_id)
        return True

    return PqLadderExecutor(
        trace, set_at_risk=set_at_risk, candidate_of=lambda h: None, request_calibration=request_calibration
    )


@pytest.mark.asyncio
async def test_ladder_substitutes_recalibrates_excludes_escalates_and_recovers() -> None:
    trace, flags, calib = FakeTrace(), [], []
    ladder = _ladder(trace, flags, calib)
    oid = str(uuid4())
    acted_all: list[CorrectionActionType] = []
    t = NOW
    # BREACH needs the hysteresis dwell: feed breaching readings until the ladder escalates.
    for _ in range(200):
        acted_all += await ladder.observe(oid, BAD, LIMITS, ["h1", "h2", "h3"], spare_hubs=3, now=t)
        t += timedelta(seconds=2)
        if flags:
            break
    assert CorrectionActionType.SUBSTITUTE_HUBS in acted_all
    assert CorrectionActionType.RECALIBRATE in acted_all
    assert calib  # a hub already taken off the delivery
    assert flags[0][1:] == (True, reasons_pq_at_risk())
    assert ladder.excluded_by_obligation()[oid]
    # Recovery: the hubs come back and the flag clears.
    for _ in range(400):
        await ladder.observe(oid, GOOD, LIMITS, ["h1", "h2", "h3"], spare_hubs=3, now=t)
        t += timedelta(seconds=2)
        if len(flags) > 1:
            break
    assert flags[-1][1:] == (False, R_PQ_DRIFT_RECOVERED)
    assert ladder.excluded_by_obligation() == {}
    assert "PQ_LADDER" in trace.classes() and "PQ_RECOVERED" in trace.classes()


def reasons_pq_at_risk() -> str:
    from opengrid.allocator.pq_monitor import R_PQ_DRIFT_AT_RISK

    return R_PQ_DRIFT_AT_RISK


@pytest.mark.asyncio
async def test_a_missing_measurement_is_recorded_once_and_never_compliant() -> None:
    trace, flags, calib = FakeTrace(), [], []
    ladder = _ladder(trace, flags, calib)
    oid = str(uuid4())
    for _ in range(3):
        assert await ladder.observe(oid, None, LIMITS, ["h1"], spare_hubs=0, now=NOW) == []
    assert trace.classes() == ["PQ_MEASUREMENT_MISSING"]


# --- cycle extras: traces on entry -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extras_trace_pq_capability_reduced_and_territory_blocks_on_entry_only() -> None:
    trace = FakeTrace()
    extras = EngineCycleExtras(trace, pq_context=lambda excluded: PqDispatchContext())
    result = CycleResult(
        "c",
        pq_reductions=(PqCapabilityReduction("dc", "b1", 20.0, 30.0, 10.0, ("h2",)),),
        territory_blocks=(TerritoryBlock("b2", None, "R-TERRITORY-NO-FREE-ACCESS"),),
    )
    for _ in range(3):
        await extras.observe(result, LedgerView(calls=()), NOW)
    assert trace.classes().count(PQ_CAPABILITY_REDUCED) == 1
    assert trace.classes().count(TERRITORY_BLOCK) == 1
    await extras.observe(CycleResult("c"), LedgerView(calls=()), NOW)  # cleared
    await extras.observe(result, LedgerView(calls=()), NOW)  # re-entry is traced again
    assert trace.classes().count(PQ_CAPABILITY_REDUCED) == 2


@pytest.mark.asyncio
async def test_extras_default_to_territory_on_and_carry_the_pq_context() -> None:
    ctx = PqDispatchContext(phase_by_hub_id={"h1": "A"})
    extras = EngineCycleExtras(FakeTrace(), pq_context=lambda excluded: ctx)
    got = await extras.extras(FleetState((), ()), LedgerView(calls=()), NOW)
    assert got.enforce_territory is True and got.pq is ctx and got.closed_loop_caps == {}


# --- PQ hub items and closed-loop zero grants -------------------------------------------------------------


@dataclass
class _HubCap:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    health: str = "online"


class _Fleet:
    def __init__(self, hubs: list[_HubCap]) -> None:
        self._hubs = hubs

    def hub_capabilities(self, bank_id: str) -> list[_HubCap]:
        return self._hubs


def _g(kw: str, *, obligation=None, reason: str | None = None, headroom: bool = False) -> Grant:
    return Grant(
        grant_id=uuid4(),
        cycle_id="c",
        obligation_id=obligation,
        bank_id="b1",
        granted_kw=Decimal(kw),
        is_headroom=headroom,
        ledger_version=1,
        reason_code=reason,
    )


def test_pq_obligation_items_follow_the_allocation_and_other_grants_avoid_those_hubs() -> None:
    fleet = _Fleet([_HubCap(f"h{i}", "b1", 10.0) for i in range(4)])
    dc, firm = uuid4(), uuid4()
    grants = [_g("15", obligation=dc), _g("10", obligation=firm)]
    items = engine._distribute_hub_items(
        "b1", grants, fleet_module=fleet, hub_allocations={(str(dc), "b1"): {"h0": 10.0, "h1": 5.0}}
    )
    dc_hubs = {i["hub_id"] for i in items if i["obligation_id"] == str(dc)}
    firm_hubs = {i["hub_id"] for i in items if i["obligation_id"] == str(firm)}
    assert dc_hubs == {"h0", "h1"} and firm_hubs == {"h2", "h3"}
    assert sum(
        float(i["obligation_granted_kw"]) for i in items if i["obligation_id"] == str(firm)
    ) == pytest.approx(10)


def test_a_zero_kw_closed_loop_grant_carries_its_reason_into_the_batch() -> None:
    fleet = _Fleet([_HubCap("h0", "b1", 10.0)])
    dc = uuid4()
    items = engine._distribute_hub_items(
        "b1", [_g("0", obligation=dc, reason=reasons.R_GRANT_CLOSED_LOOP)], fleet_module=fleet
    )
    assert [(i["reason_code"], i["obligation_granted_kw"]) for i in items] == [
        (reasons.R_GRANT_CLOSED_LOOP, "0")
    ]
    partial = engine._distribute_hub_items(
        "b1", [_g("4", obligation=dc, reason=reasons.R_GRANT_CLOSED_LOOP)], fleet_module=fleet
    )
    assert partial[0]["reason_code"] == reasons.R_GRANT_CLOSED_LOOP


def test_shares_follow_the_derated_bound_with_unknown_temperature() -> None:
    @dataclass
    class _Rated(_HubCap):
        rated_kw: float = 10.0
        soc_kwh: float = 30.0
        reserve_kwh: float = 7.84
        e_kwh: float = 39.2

    fleet = _Fleet([_Rated("h0", "b1", 10.0), _HubCap("h1", "b1", 10.0)])
    items = engine._distribute_hub_items("b1", [_g("15", headroom=True)], fleet_module=fleet)
    shares = {i["hub_id"]: -float(i["p_kw_setpoint"]) for i in items}
    assert shares["h0"] == pytest.approx(15.0 * 5.0 / 15.0)  # derated to 5 kW (unknown temperature x 0.5)


# --- flow-limit registry ------------------------------------------------------------------------------------


def test_registry_rows_merge_into_the_flow_limits() -> None:
    limits = topology_limits(
        FlowLimits(enabled=True, xfmr_kva={"x9": 1.0}),
        {
            "hubs": [("h1", "x1", 7.0), ("h2", None, 5.0)],
            "xfmr": [("x1", 25.0)],
            "bank_feeder": [("b1", "f1")],
            "bank_substation": [("b1", "s1")],
            "feeder": [("f1", 300.0)],
            "substation": [("s1", 0.0)],
        },
    )
    assert limits.xfmr_by_hub == {"h1": "x1"}
    assert limits.export_limit_by_hub == {"h1": 7.0, "h2": 5.0}
    assert limits.xfmr_kva == {"x9": 1.0, "x1": 25.0}
    assert limits.feeder_budget_kw == {"f1": 300.0} and limits.substation_budget_kw == {"s1": 0.0}
    assert limits.feeder_by_bank == {"b1": "f1"} and limits.substation_by_bank == {"b1": "s1"}


def test_hub_allocation_model_round_trip() -> None:
    alloc = HubAllocation("o", "b", (("h1", 1.0),))
    assert dict(alloc.per_hub_kw) == {"h1": 1.0}


# --- 5. MQTT routing of site/# and corridor/# -------------------------------------------------------------


class _FakeMqtt:
    def __init__(self, messages: list[tuple[str, dict[str, Any]]]) -> None:
        import json
        from types import SimpleNamespace

        import aiomqtt

        self.subscribed: list[str] = []
        self._messages = [
            SimpleNamespace(topic=aiomqtt.Topic(t), payload=json.dumps(p).encode()) for t, p in messages
        ]

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self):
        async def _gen():
            for m in self._messages:
                yield m

        return _gen()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_site_and_corridor_topics_route_to_site_ingest_only_when_enabled(monkeypatch, enabled) -> None:
    from opengrid import site_ingest

    seen: list[str] = []
    monkeypatch.setattr(site_ingest, "ingest_site_meter", lambda payload, *, topic, root: seen.append(topic))
    monkeypatch.setattr(
        site_ingest, "ingest_corridor_current", lambda payload, *, topic, root: seen.append(topic)
    )
    cfg = Config({"mqtt": {"topic_root": "og/v1"}})
    client = _FakeMqtt([("og/v1/site/c1/s1/meter", {}), ("og/v1/corridor/c1/k1/current", {})])
    await engine._mqtt_ingest_loop(client, cfg, raw_worker=None, site_ingest_on=enabled)  # type: ignore[arg-type]
    if enabled:
        assert seen == ["og/v1/site/c1/s1/meter", "og/v1/corridor/c1/k1/current"]
        assert {"og/v1/site/#", "og/v1/corridor/#"} <= set(client.subscribed)
    else:
        assert seen == []
        assert "og/v1/site/#" not in client.subscribed


@pytest.mark.asyncio
async def test_the_reader_uses_the_selectors_published_break_even(monkeypatch) -> None:
    from opengrid.engine.wiring import stored_energy_value_reader
    from opengrid.selector import energy_value

    monkeypatch.setattr(
        energy_value, "discharge_threshold_usd_per_mwh", lambda bank_id, at: 95.0 if bank_id == "b1" else None
    )
    assert await stored_energy_value_reader()(["b1", "b2"], NOW) == {"b1": 95.0}


@pytest.mark.asyncio
async def test_a_bank_without_its_zone_price_takes_no_headroom(monkeypatch) -> None:
    """Review R3 (2): never priced off other zones' prices, never at $0 -- no headroom that cycle."""
    zones = {"b-north": "LZ_NORTH", "b-west": "LZ_WEST", "b-none": None}
    monkeypatch.setattr(gw, "_bank_zone", lambda bank_id: zones[bank_id])
    cursor = _Cursor([[("LZ_NORTH", 40.0)], [], []])
    schedule = await gw.EngineScheduleGateway(_Pool(cursor)).schedule(list(zones))
    thresholds = {p.bank_id: p.threshold_usd_per_mwh for p in schedule.prices}
    assert thresholds["b-north"] is not None
    assert thresholds["b-west"] is None and thresholds["b-none"] is None
    empty = await gw.EngineScheduleGateway(_Pool(_Cursor([[], []]))).schedule(["b-north"])
    assert empty.prices[0].threshold_usd_per_mwh is None


# --- review R3 (1): NO_NEW_COMMITMENTS blocks intake -------------------------------------------------------


@dataclass
class _Trigger:
    gate_kind: str = "SCHEDULED_15MIN"
    contract_scope: Any = None


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "expect_intake"), [(False, True), (True, False), ("error", False)])
async def test_intake_is_skipped_while_no_new_commitments_and_when_unreadable(state, expect_intake) -> None:
    from opengrid.engine.gates import run_due_gates

    intakes: list[str] = []
    gates: list[str] = []

    async def run_intake(kind, scope, *, now):
        intakes.append(kind)

    async def run_gate(kind, scope):
        gates.append(kind)

    async def no_new():
        if state == "error":
            raise RuntimeError("db down")
        return state

    async def raise_alert(finding):
        return None

    trace = FakeTrace()

    class _T:
        async def append(self, stream, dt, ec, payload, /):
            trace.appended.append((stream, dt, ec, payload, None))

    failed = await run_due_gates(
        [_Trigger()],
        now=NOW,
        run_intake=run_intake,
        run_gate=run_gate,
        trace=_T(),
        raise_alert=raise_alert,
        no_new_commitments=no_new,
    )
    assert failed == 0
    assert gates == ["SCHEDULED_15MIN"]  # the gate still runs for committed obligations
    assert bool(intakes) is expect_intake
    assert ("INTAKE_SKIPPED" in trace.classes()) is (not expect_intake)


@pytest.mark.asyncio
async def test_no_new_commitments_reads_the_degraded_mode_state(monkeypatch) -> None:
    from opengrid.health import queries

    async def modes(pool):
        return [("NO_NEW_COMMITMENTS", NOW)]

    monkeypatch.setattr(queries, "fetch_degraded_modes", modes)
    assert await engine.no_new_commitments_active(object()) is True  # type: ignore[arg-type]


def test_a_zero_kw_territory_grant_carries_its_reason_into_the_batch() -> None:
    fleet = _Fleet([_HubCap("h0", "b1", 10.0)])
    items = engine._distribute_hub_items(
        "b1", [_g("0", obligation=uuid4(), reason="R-TERRITORY-OUTSIDE")], fleet_module=fleet
    )
    assert [(i["reason_code"], i["obligation_granted_kw"]) for i in items] == [("R-TERRITORY-OUTSIDE", "0")]
