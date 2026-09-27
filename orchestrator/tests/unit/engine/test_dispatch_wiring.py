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
    assert on.flow_limits == FlowLimits(enabled=False, default_export_limit_kw=20.0, xfmr_kva={"x1": 50.0})


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


# --- lead items 9/10: restore after a best-effort shortfall, commitment-lock events, hold floor ------------


class _RecCursor(_Cursor):
    pass


class _CommitConn(_Conn):
    async def commit(self):
        return None


class _CommitPool:
    def __init__(self, cursor: _Cursor) -> None:
        self._conn = _CommitConn(cursor)

    def connection(self):
        return self._conn


def _lock_events(cursor: _Cursor) -> list[tuple[str, str]]:
    return [
        (p["obligation_id"], p["reason_code"])
        for sql, p in cursor.executed
        if "INSERT INTO og.commitment" in sql
    ]


@pytest.mark.asyncio
async def test_shortfall_restored_traces_writes_a_lock_event_and_clears_at_risk(monkeypatch) -> None:
    from opengrid import contracts

    oid = str(uuid4())
    flags: list[tuple[str, bool, str]] = []

    async def set_at_risk(obligation_id, at_risk, *, reason_code, payload=None):
        flags.append((str(obligation_id), at_risk, reason_code))

    async def persist(cycle_id, records):
        return None

    async def version():
        return 1

    monkeypatch.setattr(contracts, "set_obligation_at_risk", set_at_risk)
    monkeypatch.setattr(gw.ledger, "persist_grants", persist)
    monkeypatch.setattr(gw.ledger, "ledger_version", version)
    monkeypatch.setattr(gw.fleet, "hub_capabilities", lambda bank_id: [])
    trace = FakeTrace()
    row = (oid, "b1", Decimal("40"), "PARTNER_CAPACITY", "T1", None, "SHORTFALL", False, None)
    cursor = _Cursor([[row], [], []] * 3)
    ledger_gw = gw.EngineLedgerGateway(_CommitPool(cursor), trace)  # type: ignore[arg-type]
    from opengrid.allocator.models import ShortfallReport

    # Cycle 1: still short under an L2 limit -> lock event with the L2 override reason.
    await ledger_gw.ledger_view(["b1"], NOW)
    await ledger_gw.persist_grants("c1", [ProposedGrant("b1", 10.0, oid)])
    await ledger_gw.record_shortfalls(
        "c1", [ShortfallReport(oid, "b1", 30.0, "R-COMMIT-LOCK-OVERRIDE-L2")], now=NOW
    )
    # Cycle 2: the limit is gone -> served in full -> restored.
    await ledger_gw.ledger_view(["b1"], NOW)
    await ledger_gw.persist_grants("c2", [ProposedGrant("b1", 40.0, oid)])
    await ledger_gw.record_shortfalls("c2", [], now=NOW)
    # Cycle 3: still full -> nothing new.
    await ledger_gw.ledger_view(["b1"], NOW)
    await ledger_gw.persist_grants("c3", [ProposedGrant("b1", 40.0, oid)])
    await ledger_gw.record_shortfalls("c3", [], now=NOW)

    assert _lock_events(cursor) == [(oid, "R-COMMIT-LOCK-OVERRIDE-L2"), (oid, gw.R_SHORTFALL_RESTORED)]
    assert [c for c in trace.classes() if c == "SHORTFALL_RESTORED"] == ["SHORTFALL_RESTORED"]
    assert flags == [(oid, False, gw.R_SHORTFALL_RESTORED)]


@pytest.mark.asyncio
async def test_lock_events_map_detail_codes_and_record_substitutions_once_per_interval() -> None:
    from opengrid.allocator.models import ShortfallReport, SubstitutionEvent

    oid = str(uuid4())
    cursor = _Cursor([])
    ledger_gw = gw.EngineLedgerGateway(_CommitPool(cursor), FakeTrace())  # type: ignore[arg-type]
    for _ in range(3):
        await ledger_gw.record_shortfalls(
            "c", [ShortfallReport(oid, "b1", 5.0, "R-COMMIT-LOCK-OVERRIDE-L0")], now=NOW
        )
        await ledger_gw.record_substitution_events("c", [SubstitutionEvent(oid, "b1", ("h1",), ("h2",))])
    assert sorted(_lock_events(cursor)) == sorted(
        [(oid, "R-COMMIT-LOCK-OVERRIDE-L0"), (oid, "R-SUBSTITUTION")]
    )
    assert gw.lock_reason_of("R-SHORTFALL-L2-INSTRUCTION") == "R-COMMIT-LOCK-OVERRIDE-L2"
    sql = gw._LOCK_EVENT_SQL
    assert "supersedes" in sql and "c.commitment_id" in sql and "c.committed_kw" in sql


def test_an_expired_l2_instruction_no_longer_binds() -> None:
    from opengrid.core.models.mqtt import ScadaUtilityInstruction

    base = {
        "instruction_id": uuid4(),
        "bank_id": "b1",
        "kind": "LIMIT",
        "limit_kw": 5.0,
        "issued_at": NOW,
        "issued_by": "u",
    }
    assert gw.instruction_active(ScadaUtilityInstruction(**base), NOW)
    assert gw.instruction_active(ScadaUtilityInstruction(**base, expires_at=NOW + timedelta(minutes=5)), NOW)
    assert not gw.instruction_active(
        ScadaUtilityInstruction(**base, expires_at=NOW - timedelta(seconds=1)), NOW
    )


@pytest.mark.asyncio
async def test_schedule_gateway_drops_expired_instructions(monkeypatch) -> None:
    from opengrid.core.models.mqtt import ScadaUtilityInstruction

    expired = ScadaUtilityInstruction(
        instruction_id=uuid4(),
        bank_id="b1",
        kind="LIMIT",
        limit_kw=5.0,
        issued_at=NOW - timedelta(hours=1),
        expires_at=NOW - timedelta(minutes=30),
        issued_by="u",
    )
    monkeypatch.setattr(gw.fleet, "utility_instruction", lambda bank_id: expired)
    assert await gw.EngineScheduleGateway(_Pool(_Cursor([]))).instructions(["b1"]) == ()


def test_best_effort_obligation_gets_its_full_commitment_once_the_cause_clears() -> None:
    """K13 best effort: short under an L2 LIMIT; the next cycle without it grants the full commitment."""
    from opengrid.allocator.cycle import cycle
    from opengrid.allocator.models import BankSnapshot, Instruction, Schedule

    hubs = tuple(HubSnapshot(f"h{i}", "b1", 10.0, soc_kwh=1e6, reserve_kwh=0.0, e_kwh=1e6) for i in range(4))
    fleet_state = FleetState(hubs=hubs, banks=(BankSnapshot("b1", 40.0, kva_rating=40.0),))
    call = ObligationCall(
        "o1", "b1", "PARTNER_CAPACITY", "T1", 40.0, tuple(h.hub_id for h in hubs), in_shortfall=True
    )
    limited = cycle(
        NOW, fleet_state, LedgerView((call,)), Schedule(), {}, (Instruction("BANK", "b1", "LIMIT", 10.0),)
    )
    (short,) = limited.grants
    assert short.granted_kw == 10.0 and short.reason_code == "R-SHORTFALL-L2-INSTRUCTION"
    restored = cycle(NOW, fleet_state, LedgerView((call,)), Schedule(), {}, ())
    (full,) = restored.grants
    assert full.granted_kw == 40.0 and full.reason_code == reasons.R_GRANT_COMMITTED
    assert restored.shortfalls == ()


@pytest.mark.asyncio
async def test_hold_floor_in_process_then_db_after_restart_throttled() -> None:
    calls: list[list[str]] = []

    async def db(bank_ids, at):
        calls.append(list(bank_ids))
        return {"b2": 222.0}

    schedule_gw = gw.EngineScheduleGateway(
        _Pool(_Cursor([])),
        hold_floor_in_process=lambda bank_id, at: 111.0 if bank_id == "b1" else None,
        hold_floor_db=db,
    )
    assert await schedule_gw._hold_floors(["b1", "b2", "b3"]) == {"b1": 111.0, "b2": 222.0}
    assert await schedule_gw._hold_floors(["b1", "b2", "b3"]) == {"b1": 111.0, "b2": 222.0}
    assert calls == [["b2", "b3"]]  # read once, then cached for HOLD_FLOOR_DB_REFRESH_S


@pytest.mark.asyncio
async def test_ledger_view_carries_the_remaining_deployment_of_a_called_award(monkeypatch) -> None:
    oid = str(uuid4())
    monkeypatch.setattr(gw.fleet, "hub_capabilities", lambda bank_id: [])
    row = (
        oid,
        "b1",
        Decimal("40"),
        "ERCOT_AS",
        "T2",
        None,
        "DELIVERING",
        True,
        240,
        NOW + timedelta(minutes=45),
    )
    view = await gw.EngineLedgerGateway(_Pool(_Cursor([[row], [], []]))).ledger_view(["b1"], NOW)
    (call,) = view.calls
    assert call.deployment_remaining_h == pytest.approx(0.75)
    assert call.hold_duration_h == 4.0


# --- review R3: K4 veto fail-safe, shared G-26 default, market refresh, utility scale ----------------------


def test_the_export_default_is_the_guardians_own_key() -> None:
    assert dispatch_settings(Config({})).flow_limits.default_export_limit_kw == 20.0
    cfg = Config({"guardian": {"flow": {"default_export_limit_kw": 7.5}}})
    assert dispatch_settings(cfg).flow_limits.default_export_limit_kw == 7.5
    unknown = Config({"guardian": {"flow": {"default_export_limit_kw": "unknown"}}})
    assert dispatch_settings(unknown).flow_limits.default_export_limit_kw is None


def test_hubs_in_a_verdict_come_from_vetoed_hub_ids_and_violations() -> None:
    from opengrid.engine.veto import hubs_in_verdict

    payload = {
        "vetoed_hub_ids": ["h1"],
        "violations": [{"rule_id": "G-02", "hub_id": "h2"}, {"rule_id": "G-03"}],
    }
    assert hubs_in_verdict(payload) == {"h1", "h2"}
    assert hubs_in_verdict({"violations": [{"rule_id": "G-13", "hub_id": None}]}) == set()


def test_exclusions_last_n_cycles_and_restart_on_repeat() -> None:
    from opengrid.engine.veto import HubVetoExclusions

    ex = HubVetoExclusions(exclude_cycles=3)
    assert ex.exclude({"h1"}) == {"h1"}
    ex.next_cycle()
    ex.next_cycle()
    assert ex.active() == frozenset({"h1"})
    assert ex.exclude({"h1"}) == set()  # already excluded: count restarts
    for _ in range(2):
        ex.next_cycle()
    assert ex.active() == frozenset({"h1"})
    ex.next_cycle()
    assert ex.active() == frozenset()


class _FakeVerdicts:
    def __init__(self, outcomes: dict, hubs: dict, *, arrive_after: int = 0) -> None:
        self._outcomes, self._hubs, self._polls, self._after = outcomes, hubs, 0, arrive_after

    async def outcomes(self, batch_ids):
        self._polls += 1
        if self._polls <= self._after:
            return {}
        return {b: self._outcomes[b] for b in batch_ids if b in self._outcomes}

    async def vetoed_hubs(self, batch_ids):
        return {b: self._hubs.get(b, set()) for b in batch_ids}


@pytest.mark.asyncio
async def test_a_partly_vetoed_bank_is_retried_once_without_its_vetoed_hubs() -> None:
    from types import SimpleNamespace

    from opengrid.engine.veto import HubVetoExclusions

    b1, b2, b3 = uuid4(), uuid4(), uuid4()
    reader = _FakeVerdicts(
        {b1: "PARTLY_VETOED", b2: "PASS", b3: "VETOED"},
        {b1: {"h7"}, b3: set()},  # b3: batch-level veto
    )
    trace = FakeTrace()
    state = SimpleNamespace(
        verdict_reader=reader, veto_exclusions=HubVetoExclusions(), pending_verdicts={}, trace=trace
    )
    banks = await engine.handle_vetoes(
        state, {b1: "bank-1", b2: "bank-2", b3: "bank-3"}, wait_s=0.0, cycle_id="c"
    )
    assert banks == ["bank-1"]  # only the bank whose verdict names hubs is re-proposed
    assert state.veto_exclusions.active() == frozenset({"h7"})
    assert trace.appended[0][2] == "HUB_VETO_EXCLUDED" and trace.appended[0][4] == ["R-HUB-VETO-EXCLUDED"]


@pytest.mark.asyncio
async def test_late_verdicts_are_kept_for_the_next_cycle() -> None:
    from types import SimpleNamespace

    from opengrid.engine.veto import HubVetoExclusions

    b1 = uuid4()
    reader = _FakeVerdicts({b1: "PARTLY_VETOED"}, {b1: {"h1"}}, arrive_after=100)
    state = SimpleNamespace(
        verdict_reader=reader, veto_exclusions=HubVetoExclusions(), pending_verdicts={}, trace=FakeTrace()
    )
    assert await engine.handle_vetoes(state, {b1: "bank-1"}, wait_s=0.0, cycle_id="c") == []
    assert state.pending_verdicts == {b1: "bank-1"}


def test_excluded_hubs_get_no_items_and_the_retry_substitutes_them() -> None:
    from opengrid.allocator.cycle import cycle
    from opengrid.allocator.models import BankSnapshot, Schedule

    fleet = _Fleet([_HubCap("h0", "b1", 10.0), _HubCap("h1", "b1", 10.0)])
    items = engine._distribute_hub_items(
        "b1", [_g("10", obligation=uuid4())], fleet_module=fleet, excluded_hub_ids=frozenset({"h0"})
    )
    assert {i["hub_id"] for i in items} == {"h1"}
    hubs = tuple(HubSnapshot(f"h{i}", "b1", 10.0, soc_kwh=1e6, reserve_kwh=0.0, e_kwh=1e6) for i in range(3))
    call = ObligationCall("o1", "b1", "PARTNER_CAPACITY", "T1", 15.0, ("h0", "h1", "h2"))
    result = cycle(
        NOW,
        FleetState(hubs, (BankSnapshot("b1", 30.0, kva_rating=30.0),)),
        LedgerView((call,)),
        Schedule(),
        {},
        (),
        excluded_hub_ids=frozenset({"h0"}),
    )
    (grant,) = result.grants
    assert grant.granted_kw == pytest.approx(15.0)  # served in full by h1/h2
    (event,) = result.substitutions
    assert event.from_hub_ids == ("h0",)


@pytest.mark.asyncio
async def test_market_model_refresh_swaps_the_model(monkeypatch) -> None:
    from opengrid.engine import wiring

    sentinel = object()
    monkeypatch.setattr(wiring, "load_fleet_market_model", lambda fleet_module: sentinel)
    gateway = gw.EngineFleetGateway()
    await engine.refresh_market_model(gateway, object())
    assert gateway._market is sentinel


def test_utility_scale_banks_are_rated_at_nameplate() -> None:
    from opengrid.allocator import flow_limits

    big = HubSnapshot(
        "s1",
        "b-sub",
        20000.0,
        soc_kwh=30000.0,
        reserve_kwh=8000.0,
        e_kwh=40000.0,
        rated_kw=20000.0,
        cell_temp_c=25.0,
        utility_scale=True,
    )
    assert flow_limits.derated_discharge_kw(big) == pytest.approx(20000.0)
    home = HubSnapshot(
        "h1", "b1", 11.0, soc_kwh=30.0, reserve_kwh=7.84, e_kwh=39.2, rated_kw=20000.0, cell_temp_c=25.0
    )
    assert flow_limits.derated_discharge_kw(home) < 100.0
    gw.set_utility_scale_banks({"b-sub"})
    try:
        assert gw.is_utility_scale_bank("b-sub") and not gw.is_utility_scale_bank("b1")

        @dataclass
        class _Sub(_HubCap):
            rated_kw: float = 20000.0
            soc_kwh: float = 30000.0
            reserve_kwh: float = 8000.0
            e_kwh: float = 40000.0
            cell_temp_c: float = 25.0

        assert engine._derated_room_kw(_Sub("s1", "b-sub", 20000.0)) == pytest.approx(20000.0)
    finally:
        gw.set_utility_scale_banks(set())


# --- DEVICE-INFO: retained per-hub message routed off the hot path ------------------------------------------


@pytest.mark.asyncio
async def test_device_info_is_routed_to_a_background_worker_not_the_ingest_path() -> None:
    from opengrid.engine.background import BackgroundIngest

    queued: list[dict] = []

    async def handler(item):
        queued.append(item)

    worker = BackgroundIngest("device-info", handler)
    cfg = Config({"mqtt": {"topic_root": "og/v1"}})
    client = _FakeMqtt([("og/v1/hub/hub-00001/info", {"hub_id": "hub-00001", "firmware": "1.2.3"})])
    await engine._mqtt_ingest_loop(client, cfg, raw_worker=None, device_info_worker=worker)  # type: ignore[arg-type]
    assert "og/v1/hub/+/info" in client.subscribed
    assert queued == []  # only queued by the ingest loop; handled by the worker's own task
    import asyncio

    task = asyncio.create_task(worker.run())
    await worker.drain()
    task.cancel()
    assert queued == [
        {"topic": "og/v1/hub/hub-00001/info", "payload": {"hub_id": "hub-00001", "firmware": "1.2.3"}}
    ]


@pytest.mark.asyncio
async def test_device_info_handler_calls_upsert_or_drops_until_the_module_exists(monkeypatch) -> None:
    import importlib
    import types

    calls: list[tuple] = []

    async def upsert(pool, msg):
        calls.append((pool, msg))

    fake = types.ModuleType("opengrid.fleet.device_info")
    fake.upsert_device_info = upsert  # type: ignore[attr-defined]
    real_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *a: fake if name == "opengrid.fleet.device_info" else real_import(name, *a),
    )
    handler = engine.make_device_info_handler("pool")
    await handler({"topic": "t", "payload": {"hub_id": "h1"}})
    assert calls == [("pool", {"hub_id": "h1"})]

    def missing(name, *a):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    await engine.make_device_info_handler("pool")({"topic": "t", "payload": {}})  # logged, dropped, no raise


# --- firmware campaigns (owner decision: final release) -----------------------------------------------------


class _FakeFirmware:
    def __init__(self, updating: frozenset[str] = frozenset(), *, fail: bool = False) -> None:
        self._updating = updating
        self._fail = fail
        self.steps = 0

    async def step(self, now):
        self.steps += 1
        if self._fail:
            raise RuntimeError("firmware repo down")

    def updating_hub_ids(self) -> frozenset[str]:
        return self._updating


@pytest.mark.asyncio
async def test_firmware_steps_before_allocation_and_its_failure_never_costs_the_cycle() -> None:
    from types import SimpleNamespace

    ok = SimpleNamespace(firmware=_FakeFirmware())
    await engine.step_firmware(ok, NOW)
    assert ok.firmware.steps == 1
    broken = SimpleNamespace(firmware=_FakeFirmware(fail=True))
    await engine.step_firmware(broken, NOW)  # logged, no raise
    await engine.step_firmware(SimpleNamespace(firmware=None), NOW)


def test_updating_hubs_are_excluded_from_dispatch_with_the_vetoed_ones() -> None:
    from types import SimpleNamespace

    from opengrid.engine.veto import HubVetoExclusions

    vetoes = HubVetoExclusions()
    vetoes.exclude({"h1"})
    state = SimpleNamespace(veto_exclusions=vetoes, firmware=_FakeFirmware(frozenset({"h2"})))
    assert engine.excluded_now(state) == frozenset({"h1", "h2"})
    assert engine.excluded_now(SimpleNamespace(veto_exclusions=vetoes, firmware=None)) == frozenset({"h1"})
    fleet = _Fleet([_HubCap("h1", "b1", 10.0), _HubCap("h2", "b1", 10.0), _HubCap("h3", "b1", 10.0)])
    items = engine._distribute_hub_items(
        "b1", [_g("9", obligation=uuid4())], fleet_module=fleet, excluded_hub_ids=engine.excluded_now(state)
    )
    assert {i["hub_id"] for i in items} == {"h3"}


@pytest.mark.asyncio
async def test_firmware_status_is_routed_off_the_ingest_path_and_not_to_the_ack_parser(monkeypatch) -> None:
    from opengrid import fleet
    from opengrid.engine.background import BackgroundIngest

    acks: list[dict] = []

    async def ingest_ack(payload):
        acks.append(payload)

    monkeypatch.setattr(fleet, "ingest_ack", ingest_ack)
    queued: list[dict] = []

    async def handler(item):
        queued.append(item)

    worker = BackgroundIngest("firmware-status", handler)
    cfg = Config({"mqtt": {"topic_root": "og/v1"}})
    status = {
        "hub_id": "hub-1",
        "command_id": "c1",
        "state": "UPDATING",
        "firmware_version": "2.1.0",
        "ts": NOW.isoformat(),
    }
    client = _FakeMqtt([("og/v1/ack/fw/hub-1", status)])
    await engine._mqtt_ingest_loop(client, cfg, raw_worker=None, firmware_status_worker=worker)  # type: ignore[arg-type]
    assert "og/v1/ack/fw/+" in client.subscribed
    import asyncio

    task = asyncio.create_task(worker.run())
    await worker.drain()
    task.cancel()
    assert queued == [status] and acks == []


@pytest.mark.asyncio
async def test_firmware_status_handler_validates_then_records(monkeypatch) -> None:
    from opengrid.firmware import ingest
    from opengrid.platform import mqtt

    recorded: list[dict] = []

    async def record(pool, payload):
        recorded.append(payload)
        return True

    monkeypatch.setattr(ingest, "record_firmware_status", record)

    def invalid(kind, payload):
        raise mqtt.SchemaValidationError("firmware_status: bad")

    monkeypatch.setattr(mqtt, "validate_payload", invalid)
    handler = engine.make_firmware_status_handler("pool")
    await handler({"hub_id": "h1"})
    assert recorded == []  # invalid: dropped
    monkeypatch.setattr(mqtt, "validate_payload", lambda kind, payload: None)
    await handler({"hub_id": "h1"})
    assert recorded == [{"hub_id": "h1"}]


@pytest.mark.asyncio
async def test_a_retained_device_info_burst_of_2501_hubs_is_ingested_without_drops(monkeypatch) -> None:
    """Live r3 19:03: the retained burst overflowed the default 500-slot queue (2,000 dropped)."""
    import asyncio
    import importlib
    import types

    upserted: list[str] = []

    async def upsert(pool, msg):
        upserted.append(msg["hub_id"])

    fake = types.ModuleType("opengrid.fleet.device_info")
    fake.upsert_device_info = upsert  # type: ignore[attr-defined]
    real_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *a: fake if name == "opengrid.fleet.device_info" else real_import(name, *a),
    )
    cfg = Config({"mqtt": {"topic_root": "og/v1"}})
    worker = engine.build_device_info_worker("pool", cfg)
    messages = [(f"og/v1/hub/hub-{i:05d}/info", {"hub_id": f"hub-{i:05d}"}) for i in range(2501)]
    # The whole burst lands before the worker gets a turn (retained messages on connect).
    await engine._mqtt_ingest_loop(_FakeMqtt(messages), cfg, raw_worker=None, device_info_worker=worker)  # type: ignore[arg-type]
    assert worker.dropped == 0
    task = asyncio.create_task(worker.run())
    await worker.drain()
    task.cancel()
    assert len(upserted) == 2501 and worker.dropped == 0
    small = engine.build_device_info_worker("pool", Config({"mqtt": {"device_info_queue_max": 3}}))
    for i in range(5):
        small.submit({"payload": {"hub_id": str(i)}})
    assert small.dropped == 2  # the size is configurable


# --- r3.4: K4 retry safety, toll ramp, verdict hub filter, gates trace JSON --------------------------------


@pytest.mark.asyncio
async def test_a_retry_keeps_other_banks_state_and_does_not_re_step_extras() -> None:
    import opengrid.allocator as alloc
    from opengrid.allocator.models import BankSnapshot, CycleExtras, PiState

    class _FleetGw:
        def __init__(self, fs):
            self._fs = fs

        async def bank_ids(self):
            return ["b1", "b2"]

        async def fleet_state(self, bank_ids, t):
            return self._fs

    class _LedgerGw:
        def __init__(self, view):
            self._view = view
            self.shortfalls: list = []

        async def ledger_view(self, bank_ids, t):
            return LedgerView(tuple(c for c in self._view.calls if c.bank_id in bank_ids))

        async def ledger_version(self):
            return 1

        async def persist_grants(self, cycle_id, grants):
            return None

        async def record_shortfalls(self, cycle_id, shortfalls):
            self.shortfalls = list(shortfalls)

        async def record_substitution_events(self, cycle_id, events):
            return None

    class _Extras:
        def __init__(self):
            self.calls = 0
            self.observed = 0

        async def extras(self, fs, lv, t):
            self.calls += 1
            return CycleExtras()

        async def observe(self, result, lv, t):
            self.observed += 1

    hubs = tuple(
        HubSnapshot(f"h-{b}", b, 10.0, soc_kwh=1e6, reserve_kwh=0.0, e_kwh=1e6) for b in ("b1", "b2")
    )
    banks = tuple(BankSnapshot(b, 10.0, kva_rating=10.0) for b in ("b1", "b2"))
    calls = tuple(
        ObligationCall(f"o-{b}", b, "PARTNER_CAPACITY", "T1", 20.0, (f"h-{b}",)) for b in ("b1", "b2")
    )
    ledger = _LedgerGw(LedgerView(calls))
    extras = _Extras()
    alloc._pi_states["b2"] = PiState(integral=5.0)
    alloc._pi_states["b1"] = PiState(integral=1.0)
    first = await alloc.run_cycle(
        "c1", fleet=_FleetGw(FleetState(hubs, banks)), ledger=ledger, extras_gateway=extras, now=NOW
    )
    assert {str(g.bank_id) for g in first} == {"b1", "b2"}
    assert {s.bank_id for s in ledger.shortfalls} == {"b1", "b2"}
    alloc._pi_states["b1"] = PiState(integral=99.0)  # as if the first run stepped it
    retry = await alloc.run_cycle(
        "c1-r1",
        fleet=_FleetGw(FleetState(hubs, banks)),
        ledger=ledger,
        extras_gateway=extras,
        now=NOW,
        only_bank_ids=["b1"],
        retry_excluded_hub_ids=frozenset({"h-b1"}),
    )
    assert {str(g.bank_id) for g in retry} <= {"b1"}
    assert extras.calls == 1 and extras.observed == 1  # extras reused, not re-stepped; no second observe
    assert {s.bank_id for s in ledger.shortfalls} == {"b1", "b2"}  # b2's shortfall kept (AT_RISK unaffected)
    assert {str(g.bank_id) for g in alloc._last_grants} == {"b1", "b2"} or not retry
    assert alloc._pi_states["b1"].integral == 1.0  # restored to its pre-cycle state (K9)
    assert alloc._pi_states["b2"].integral == 5.0


def test_a_retried_batch_keeps_the_original_cycle_id_but_its_own_submission() -> None:
    grants = [_g("5", obligation=uuid4())]
    first = engine.build_command_batch_row(
        command_batch_id=uuid4(),
        trace_pre_image_id=uuid4(),
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=1,
    )
    retry = engine.build_command_batch_row(
        command_batch_id=uuid4(),
        trace_pre_image_id=uuid4(),
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=1,
        attempt=1,
    )
    assert first.cycle_id == retry.cycle_id == "c1"
    assert first.submission_id == "c1:b1" and retry.submission_id == "c1:b1:r1"


def test_the_retry_stays_off_unless_the_owner_enables_it() -> None:
    assert dispatch_settings(Config({})).veto_retry_enabled is False


def test_utility_scale_hubs_step_from_the_last_commanded_setpoint() -> None:
    @dataclass
    class _Big:
        hub_id: str = "sub-1"
        bank_id: str = "b-sub"
        p_kw: float = 0.0  # stale telemetry
        ramp_kw_per_s: float = 100.0
        utility_scale: bool = True

    engine._last_commanded_kw.pop("sub-1", None)
    hub = _Big()
    step1 = engine._ramped_setpoint_kw(hub, -20000.0, 2.0)
    step2 = engine._ramped_setpoint_kw(hub, -20000.0, 2.0)
    assert step1 == pytest.approx(-180.0) and step2 == pytest.approx(-360.0)  # keeps climbing
    home = _HubCap("h1", "b1", 10.0)
    home_hub = SimpleNamespaceHub(p_kw=0.0, ramp_kw_per_s=0.1)
    assert engine._ramped_setpoint_kw(home_hub, -5.0, 2.0) == engine._ramped_setpoint_kw(home_hub, -5.0, 2.0)
    assert home.hub_id == "h1"


@dataclass
class SimpleNamespaceHub:
    p_kw: float
    ramp_kw_per_s: float
    hub_id: str = "h-home"
    bank_id: str = "b1"


@pytest.mark.asyncio
async def test_only_hub_ids_are_taken_from_a_verdict() -> None:
    from opengrid.engine.veto import vetoed_banks

    b1 = uuid4()
    reader = _FakeVerdicts({b1: "VETOED"}, {b1: {"hub-1", "bank-1", "feeder-7"}})
    got = await vetoed_banks(
        reader, {b1: "VETOED"}, {b1: "bank-1"}, known_hub_ids=frozenset({"hub-1", "hub-2"})
    )
    assert got == {"bank-1": {"hub-1"}}
    none_named = await vetoed_banks(
        reader, {b1: "VETOED"}, {b1: "bank-1"}, known_hub_ids=frozenset({"hub-9"})
    )
    assert none_named == {}


@pytest.mark.asyncio
async def test_intake_skipped_trace_is_json_safe_with_a_uuid_scope() -> None:
    import json

    from opengrid.engine.gates import run_due_gates

    scope = uuid4()

    @dataclass
    class _Trig:
        gate_kind: str = "ADMISSION"
        contract_scope: Any = scope

    captured: list[dict] = []

    class _T:
        async def append(self, stream, dt, ec, payload, /):
            captured.append(payload)

    async def run_gate(kind, s):
        return None

    async def no_new():
        return True

    async def raise_alert(f):
        return None

    async def run_intake(*a, **k):
        return None

    await run_due_gates(
        [_Trig()],
        now=NOW,
        run_intake=run_intake,
        run_gate=run_gate,
        trace=_T(),
        raise_alert=raise_alert,
        no_new_commitments=no_new,
    )
    (payload,) = captured
    json.dumps(payload)  # no UUID objects left
    assert payload["contract_scope"] == str(scope)
