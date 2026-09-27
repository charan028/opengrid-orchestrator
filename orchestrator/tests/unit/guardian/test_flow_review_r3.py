"""R3 review fixes on the guardian's flow and territory checks (GUARDIAN-FLOW lane):

1. G-33 passes the engine's own 0 kW territory-block items;
2. an unsigned SCADA kVA reading is an interval, and an unknown direction is treated as export;
3. G-34: every item's hub is on the proposal's bank;
4. per-cycle accumulators count only signed batches;
5. SCADA reads: GOOD quality only, no future stamps, stale = missing;
6. the hub -> zone cache refreshes on the topology cadence;
7. `fail_closed_missing_topology`.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from opengrid.core import reasons
from opengrid.guardian import flow_checks, flow_repo
from opengrid.guardian.config import GuardianConfig, load_guardian_config
from opengrid.guardian.ports import AggregateFlow, ObligationMarket, ProposedItem
from opengrid.platform.config import Config

from .conftest import (
    BANK_ID,
    HUB_ID,
    FakeBankMembers,
    make_batch_row,
    make_hub_snapshot,
    make_proposal,
    service_with,
)
from .conftest import wire_default_passing_scenario as wire
from .test_service_flow import FakeTerritory, FakeTopology, _batch, _feeder_world, _service

# --- a fake psycopg pool: SQL constant -> rows ------------------------------------------------------------------


class _Cursor:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._pool.executed.append((sql, params))
        response = self._pool.responses.get(sql, [])
        self._rows = list(response(params) if callable(response) else response)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._pool)


class FakePool:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    def connection(self) -> _Conn:
        return _Conn(self)

    def count(self, sql: str) -> int:
        return sum(1 for s, _ in self.executed if s == sql)


def _hub_row(hub_id: str, bank_id: str, pv_kw: float | None = None) -> tuple[Any, ...]:
    return (hub_id, bank_id, None, None, pv_kw, None, None, None)


def _topology_pool(
    readings: dict[str, tuple[Any, Any, Any, Any]] | Callable[[Any], list[tuple[Any, ...]]],
    *,
    feeder_limits: list[tuple[Any, ...]] | None = None,
    hubs: list[tuple[Any, ...]] | None = None,
) -> FakePool:
    """Banks b1, b2 on feeder f1; `readings` per bank: (real kW, age, kVA, age)."""
    aggregate = (
        readings
        if callable(readings)
        else lambda params: [(b, *readings[b]) for b in params["banks"] if b in readings]
    )
    return FakePool(
        {
            flow_repo._HUB_SITES_SQL: hubs
            if hubs is not None
            else [_hub_row("h1", "b1"), _hub_row("h2", "b2")],
            flow_repo._BANKS_SQL: [("b1", "LZ_NORTH", "f1"), ("b2", "LZ_NORTH", "f1")],
            flow_repo._FEEDER_LIMITS_SQL: feeder_limits if feeder_limits is not None else [],
            flow_repo._AGGREGATE_FLOW_SQL: aggregate,
        }
    )


def _port(
    pool: FakePool, *, members: FakeBankMembers | None = None, **config: Any
) -> flow_repo.PgGridTopologyPort:
    return flow_repo.PgGridTopologyPort(
        pool,  # type: ignore[arg-type]
        GuardianConfig(key_path="", **config),
        {},
        members=members,
    )


# --- 1. G-33 and the engine's 0 kW territory-block items --------------------------------------------------------


REG = ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY")


def test_g33_passes_a_zero_kw_item_whatever_its_market():
    from opengrid.market.territory import market_of

    ref = market_of(market=REG.market, utility_id=REG.utility_id, service_type=REG.service_type)
    zones = {"LZ_AEN": "AUSTIN_ENERGY"}
    idle = ProposedItem("hub-out", 0.0, "R-TERRITORY-OUTSIDE", uuid4())
    moving = ProposedItem("hub-out", -2.0, "R-TERRITORY-OUTSIDE", uuid4())

    assert flow_checks.check_g33_territory(
        idle, ref=ref, zone="LZ_NORTH", zone_territory=zones, free_access=False
    ).ok
    vetoed = flow_checks.check_g33_territory(
        moving, ref=ref, zone="LZ_NORTH", zone_territory=zones, free_access=False
    )
    assert not vetoed.ok  # the reason code never exempts a real setpoint


async def test_the_engines_territory_block_grant_is_signed(fakes, guardian_config, signing_seed):
    territory = FakeTerritory()
    reg = uuid4()
    territory.markets[reg] = REG
    territory.zones = {"hub-out": "LZ_NORTH"}
    proposal = _batch(fakes, [ProposedItem("hub-out", 0.0, "R-TERRITORY-OUTSIDE", reg)])
    fakes.commitments.active_by_bank.pop(BANK_ID, None)

    verdict = await _service(fakes, guardian_config, signing_seed, territory=territory).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-33" not in verdict.vetoed_rule_ids


async def test_a_non_zero_item_labelled_as_a_territory_block_is_still_checked(
    fakes, guardian_config, signing_seed
):
    territory = FakeTerritory()
    reg = uuid4()
    territory.markets[reg] = REG
    territory.zones = {"hub-out": "LZ_NORTH"}
    proposal = _batch(fakes, [ProposedItem("hub-out", -2.0, "R-TERRITORY-OUTSIDE", reg)])
    fakes.commitments.active_by_bank.pop(BANK_ID, None)

    verdict = await _service(fakes, guardian_config, signing_seed, territory=territory).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-33" in verdict.vetoed_rule_ids


# --- 2. reverse flow from an unsigned kVA reading -------------------------------------------------------------------


def test_bank_flow_interval_from_kva():
    assert flow_repo.bank_flow_interval_kw(10.0, None, 0.0) == (-10.0, 10.0)  # direction unknown: export
    assert flow_repo.bank_flow_interval_kw(10.0, -3.0, 0.0) == (-3.0, 10.0)  # home load >= 0: F >= sum p
    assert flow_repo.bank_flow_interval_kw(10.0, -30.0, 0.0) == (-10.0, 10.0)  # |F| <= kVA
    assert flow_repo.bank_flow_interval_kw(10.0, 2.0, 0.0) == (2.0, 10.0)
    assert flow_repo.bank_flow_interval_kw(10.0, 0.0, 0.9) == (9.0, 10.0)  # no export: |F| >= pf * kVA
    assert flow_repo.bank_flow_interval_kw(10.0, -3.0, 0.9) == (9.0, 10.0)  # F >= -3 and |F| >= 9: F >= 9
    assert flow_repo.bank_flow_interval_kw(10.0, -9.5, 0.9) == (-9.5, 10.0)  # export still possible
    assert flow_repo.bank_flow_interval_kw(0.0, None, 0.9) == (0.0, 0.0)


def test_an_interval_flow_is_checked_at_its_export_end():
    exact = AggregateFlow("f1", 10.0, 1.0, lower_kw=-5.0, upper_kw=1000.0)
    unsigned = replace(exact, flow_low_kw=-4.0)
    kw = {"max_age_s": 30.0, "reverse_reason": "REV", "forward_reason": "FWD", "ref": "f1"}

    assert flow_checks.check_aggregate_flow("G-28", exact, 0.0, -2.0, **kw).ok
    vetoed = flow_checks.check_aggregate_flow("G-28", unsigned, 0.0, -2.0, **kw)
    assert not vetoed.ok and vetoed.reason == "REV"
    assert flow_checks.check_aggregate_flow("G-28", unsigned, 0.0, 2.0, **kw).ok  # relief (charging) passes
    over = replace(unsigned, flow_kw=999.0)
    assert not flow_checks.check_aggregate_flow("G-28", over, 0.0, 2.0, **kw).ok  # import end binds too


async def test_measured_export_seen_through_the_service_vetoes_g28(fakes, signing_seed):
    topology = FakeTopology()
    topology.feeders["f1"] = AggregateFlow("f1", 10.0, 1.0, lower_kw=-5.0, upper_kw=1000.0, flow_low_kw=-4.0)
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    proposal = _feeder_world(fakes, topology, "bank-a", -2.0)

    verdict = await _service(fakes, config, signing_seed, topology=topology).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-28" in verdict.vetoed_rule_ids
    assert reasons.R_FEEDER_REVERSE_FLOW in {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}


async def test_signed_real_power_is_used_as_is():
    pool = _topology_pool({"b1": (-5.0, 1.0, 5.1, 1.0), "b2": (2.0, 2.0, None, None)})

    flow = await _port(pool).feeder_flow("f1")

    assert flow is not None and flow.flow_kw == -3.0 and flow.flow_low_kw == -3.0 and flow.age_s == 2.0


async def test_kva_only_without_hub_telemetry_is_treated_as_export():
    pool = _topology_pool({"b1": (None, None, 10.0, 1.0), "b2": (None, None, 4.0, 3.0)})

    flow = await _port(pool).feeder_flow("f1")

    assert flow is not None and flow.flow_kw == 14.0 and flow.flow_low_kw == -14.0 and flow.age_s == 3.0


async def test_kva_only_export_side_is_bounded_by_the_guardians_own_hub_telemetry():
    members = FakeBankMembers()
    members.hub_ids = {"b1": ["h1"], "b2": ["h2"]}
    members.members = {
        "b1": [make_hub_snapshot(prev_p_kw=-3.0)],
        "b2": [replace(make_hub_snapshot(prev_p_kw=0.0, p_kw=11.0), health="stale")],  # full discharge
    }
    pool = _topology_pool(
        {"b1": (None, None, 10.0, 1.0), "b2": (None, None, 20.0, 1.0)},
        hubs=[_hub_row("h1", "b1", pv_kw=1.0), _hub_row("h2", "b2", pv_kw=0.0)],
    )

    flow = await _port(pool, members=members).feeder_flow("f1")

    assert flow is not None and flow.flow_kw == 30.0
    assert flow.flow_low_kw == pytest.approx((-3.0 - 1.0) + -11.0)  # sum p - PV per bank


async def test_a_charging_bank_with_a_power_factor_floor_is_known_to_import():
    members = FakeBankMembers()
    members.hub_ids = {"b1": ["h1"], "b2": ["h2"]}
    members.members = {"b1": [make_hub_snapshot(prev_p_kw=2.0)], "b2": [make_hub_snapshot(prev_p_kw=0.0)]}
    pool = _topology_pool(
        {"b1": (None, None, 10.0, 1.0), "b2": (None, None, 10.0, 1.0)},
        hubs=[_hub_row("h1", "b1", pv_kw=0.0), _hub_row("h2", "b2", pv_kw=0.0)],  # known: no PV
    )

    flow = await _port(pool, members=members, scada_min_power_factor=0.98).feeder_flow("f1")

    assert flow is not None and flow.flow_low_kw == pytest.approx(19.6)


# --- H3: an unknown PV rating is never "no export possible" -------------------------------------------------------------


def _idle_members() -> FakeBankMembers:
    members = FakeBankMembers()
    members.hub_ids = {"b1": ["h1"], "b2": ["h2"]}
    members.members = {"b1": [make_hub_snapshot(prev_p_kw=0.0)], "b2": [make_hub_snapshot(prev_p_kw=0.0)]}
    return members


def test_the_unknown_pv_rating_defaults_to_a_conservative_value():
    assert GuardianConfig(key_path="").default_pv_rated_kw == 10.0
    assert load_guardian_config(Config({})).default_pv_rated_kw == 10.0
    cfg = Config({"guardian": {"flow": {"default_pv_rated_kw": 7.5}}})
    assert load_guardian_config(cfg).default_pv_rated_kw == 7.5


async def test_idle_hubs_with_unknown_pv_may_still_be_exporting():
    """H3 (lead, verified): nothing writes og.hub.pv_rated_kw, so NULL read as 0 made an idle bank look unable
    to export, and G-28 reverse / G-30 passed real rooftop-PV export on kVA-only data."""
    pool = _topology_pool({"b1": (None, None, 30.0, 1.0), "b2": (None, None, 4.0, 1.0)})  # pv_rated_kw NULL

    flow = await _port(pool, members=_idle_members()).feeder_flow("f1")

    assert flow is not None and flow.flow_low_kw == pytest.approx(-10.0 + -4.0)  # max(0 - 10, -m) per bank


async def test_an_explicit_zero_pv_rating_is_still_honoured():
    pool = _topology_pool(
        {"b1": (None, None, 30.0, 1.0), "b2": (None, None, 4.0, 1.0)},
        hubs=[_hub_row("h1", "b1", pv_kw=0.0), _hub_row("h2", "b2", pv_kw=0.0)],
    )

    flow = await _port(pool, members=_idle_members()).feeder_flow("f1")

    assert flow is not None and flow.flow_low_kw == 0.0


async def test_g30_vetoes_discharge_on_kva_only_data_with_unknown_pv_but_trusts_real_power():
    def territory_port(readings: dict[str, tuple[Any, Any, Any, Any]]) -> flow_repo.PgGridTopologyPort:
        return flow_repo.PgGridTopologyPort(
            _topology_pool(readings),  # type: ignore[arg-type]
            GuardianConfig(key_path=""),
            {"LZ_NORTH": "AUSTIN_ENERGY"},
            members=_idle_members(),
        )

    def g30(flow: AggregateFlow | None) -> flow_checks.CheckOutcome:
        assert flow is not None
        return flow_checks.check_aggregate_flow(
            "G-30",
            flow,
            0.0,
            -1.0,
            max_age_s=30.0,
            reverse_reason=reasons.R_TERRITORY_EXPORT,
            forward_reason=reasons.R_TERRITORY_EXPORT,
            ref="AUSTIN_ENERGY",
        )

    kva_only = await territory_port(
        {"b1": (None, None, 30.0, 1.0), "b2": (None, None, 30.0, 1.0)}
    ).territory_flow("b1")
    vetoed = g30(kva_only)
    assert not vetoed.ok and vetoed.reason == reasons.R_TERRITORY_EXPORT

    # REAL_POWER_KW GOOD and fresh (production stores it for every bank) is preferred over kVA: 60 kW import.
    signed = await territory_port(
        {"b1": (30.0, 1.0, 30.6, 1.0), "b2": (30.0, 1.0, 30.6, 1.0)}
    ).territory_flow("b1")
    assert signed is not None and signed.flow_kw == signed.flow_low_kw == 60.0
    assert g30(signed).ok
    # ...and real power that shows export is seen as export.
    exporting = await territory_port(
        {"b1": (-5.0, 1.0, 5.1, 1.0), "b2": (2.0, 1.0, 2.1, 1.0)}
    ).territory_flow("b1")
    assert not g30(exporting).ok


# --- 5. SCADA read hygiene ---------------------------------------------------------------------------------------------


async def test_the_scada_read_takes_good_rows_only_and_no_future_stamps():
    pool = _topology_pool({"b1": (None, None, 1.0, 1.0), "b2": (None, None, 1.0, 1.0)})

    await _port(pool).feeder_flow("f1")

    sql, params = next((s, p) for s, p in pool.executed if s == flow_repo._AGGREGATE_FLOW_SQL)
    assert sql.count("quality = 'GOOD'") == 2 and sql.count("ts <= now() + make_interval") == 2
    assert params is not None and params["future_s"] == flow_repo.FUTURE_TOLERANCE_S == 5.0


@pytest.mark.parametrize(
    "readings",
    [
        {"b1": (None, None, 10.0, 31.0), "b2": (None, None, 1.0, 1.0)},  # stale kVA = missing
        {"b1": (-5.0, 31.0, None, None), "b2": (None, None, 1.0, 1.0)},  # stale real power, no kVA
        {"b2": (None, None, 1.0, 1.0)},  # no row at all for b1
        {"b1": (None, None, None, None), "b2": (None, None, 1.0, 1.0)},  # no GOOD row
    ],
)
async def test_stale_or_missing_bank_data_makes_the_aggregate_unknown(readings):
    flow = await _port(_topology_pool(readings)).feeder_flow("f1")

    assert flow is not None and flow.flow_kw is None and flow.flow_low_kw is None and math.isinf(flow.age_s)
    outcome = flow_checks.check_aggregate_flow(
        "G-28", flow, 0.0, -1.0, max_age_s=30.0, reverse_reason="R", forward_reason="F", ref="f1"
    )
    assert not outcome.ok and outcome.reason == "FLOW_UNKNOWN"  # increases fail closed


async def test_stale_real_power_falls_back_to_kva_and_a_slightly_future_stamp_counts_as_fresh():
    pool = _topology_pool({"b1": (-5.0, 31.0, 6.0, -2.0), "b2": (1.0, 0.5, None, None)})

    flow = await _port(pool).feeder_flow("f1")

    assert flow is not None and flow.flow_kw == 7.0 and flow.flow_low_kw == -5.0 and flow.age_s == 0.5


# --- 3. G-34 hub on the proposal's bank --------------------------------------------------------------------------------


async def test_hub_bank_comes_from_the_refreshed_topology():
    port = _port(_topology_pool({}))

    assert await port.hub_bank("h1") == "b1" and await port.hub_bank("nope") is None


@pytest.mark.parametrize("hub_bank", ["bank-elsewhere", None])
async def test_an_item_for_a_hub_outside_the_proposals_bank_vetoes_the_batch(
    fakes, guardian_config, signing_seed, hub_bank
):
    topology = FakeTopology()
    topology.hub_banks["hub-0001"] = hub_bank
    proposal = _batch(fakes, [ProposedItem("hub-0001", 3.0, "SELECTOR")])

    verdict = await _service(fakes, guardian_config, signing_seed, topology=topology).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "VETOED" and "G-34" in verdict.vetoed_rule_ids
    violations = fakes.trace.appended[-1][1]["violations"]
    assert ("hub-0001", reasons.R_HUB_NOT_IN_BANK) in {(v["hub_id"], v["reason"]) for v in violations}


async def test_items_on_the_proposals_bank_pass_g34(fakes, guardian_config, signing_seed):
    proposal = _batch(fakes, [ProposedItem("hub-0001", 3.0, "SELECTOR")])

    verdict = await _service(fakes, guardian_config, signing_seed, topology=FakeTopology()).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-34" not in verdict.vetoed_rule_ids


# --- 4. accumulators count signed batches only -------------------------------------------------------------------------


def _feeder_config(**kw: Any) -> GuardianConfig:
    return GuardianConfig(
        key_path="",
        cycle_interval_s=2.0,
        default_feeder_ramp_ceiling_kw_per_min=1e9,
        discretionary_ramp_cap_kw_per_min=1e9,
        non_firm_ramp_cap_kw_per_min=1e9,
        **kw,
    )


async def test_a_vetoed_batch_does_not_consume_the_cycles_feeder_headroom(fakes, signing_seed):
    topology = FakeTopology()
    topology.feeders["f1"] = AggregateFlow("f1", 10.0, 1.0, lower_kw=-20.0, upper_kw=1000.0)
    service = _service(fakes, _feeder_config(), signing_seed, topology=topology)
    first = _feeder_world(fakes, topology, "bank-a", -18.0)
    topology.hub_banks["bank-a-hub"] = "bank-elsewhere"  # vetoed on G-34, never signed
    assert (await service.evaluate_and_sign(make_batch_row(first))).outcome == "VETOED"
    assert not service._flow_delta_by_cycle and not service._fleet_delta_by_cycle

    second = _feeder_world(fakes, topology, "bank-b", -18.0)
    verdict = await service.evaluate_and_sign(make_batch_row(second))

    assert verdict.outcome == "PASS"  # 10 - 18 = -8 >= -20: only the signed batch counts
    assert service._flow_delta_by_cycle[("cycle-1", "G-28:f1")] == -18.0
    assert service._fleet_delta_by_cycle["cycle-1"] == -18.0


async def test_a_timed_out_batch_leaves_no_staged_delta(fakes, signing_seed):
    class SlowTopology(FakeTopology):
        async def feeder_flow(self, feeder_id):
            await asyncio.sleep(0.5)
            return await super().feeder_flow(feeder_id)

    topology = SlowTopology()
    service = _service(fakes, _feeder_config(verdict_timeout_ms=20.0), signing_seed, topology=topology)
    proposal = _feeder_world(fakes, topology, "bank-a", -2.0)

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "TIMEOUT"
    assert not service._fleet_delta_by_cycle and not service._gross_step_by_cycle
    assert not service._feeder_delta_by_cycle


async def test_signed_batches_accumulate_across_the_cycle(fakes, signing_seed):
    topology = FakeTopology()
    service = _service(fakes, _feeder_config(), signing_seed, topology=topology)
    for bank in ("bank-a", "bank-b"):
        proposal = _feeder_world(fakes, topology, bank, -2.0)
        assert (await service.evaluate_and_sign(make_batch_row(proposal))).outcome == "PASS"

    assert service._fleet_delta_by_cycle["cycle-1"] == -4.0
    assert service._feeder_delta_by_cycle[("cycle-1", "f1")] == -4.0
    assert service._gross_step_by_cycle["cycle-1"] == 4.0


def test_accumulate_outside_an_evaluation_commits_at_once(fakes, guardian_config, signing_seed):
    service = service_with(fakes, guardian_config, signing_seed)

    assert service._accumulate(service._fleet_delta_by_cycle, "c", 1.5) == 1.5
    assert service._accumulate(service._fleet_delta_by_cycle, "c", 1.0) == 2.5


async def test_vetoed_manual_commands_never_poison_g05_for_the_shared_cycle_id(fakes, signing_seed):
    """Dev-stack repro (lead, 2026-09-26): 20 manual 60 kW commands under cycle_id "MANUAL" were vetoed, yet
    their 1,200 kW stayed in G-05's fleet net-delta and gross-step accumulators (the key kept being refreshed
    in the LRU), so even a 0.1 kW manual command was then vetoed by G-05."""
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, discretionary_ramp_cap_kw_per_min=600.0)
    service = service_with(fakes, config, signing_seed)

    def manual(p_kw: float):
        proposal = replace(make_proposal(p_kw_setpoint=p_kw), cycle_id="MANUAL")
        wire(fakes, proposal)
        fakes.hubs.hubs[HUB_ID] = make_hub_snapshot(prev_p_kw=0.0, p_kw=11.0)
        return proposal

    for _ in range(20):
        verdict = await service.evaluate_and_sign(make_batch_row(manual(60.0)))
        assert verdict.outcome != "PASS" and "G-05" in verdict.vetoed_rule_ids  # 60 kW > 20 kW per tick
    assert "MANUAL" not in service._fleet_delta_by_cycle
    assert "MANUAL" not in service._gross_step_by_cycle

    verdict = await service.evaluate_and_sign(make_batch_row(manual(0.1)))

    assert verdict.outcome == "PASS", verdict.vetoed_rule_ids
    assert service._fleet_delta_by_cycle["MANUAL"] == pytest.approx(0.1)
    assert service._gross_step_by_cycle["MANUAL"] == pytest.approx(0.1)


# --- 6. hub -> zone cache refresh ---------------------------------------------------------------------------------------


async def test_hub_zone_cache_refreshes_on_the_topology_cadence():
    zones = [[("h1", "LZ_NORTH")]]
    pool = FakePool({flow_repo._HUB_ZONES_SQL: lambda params: zones[0]})
    clock = [0.0]
    port = flow_repo.PgTerritoryPort(pool, {}, monotonic_fn=lambda: clock[0])  # type: ignore[arg-type]

    assert await port.hub_zone("h1") == "LZ_NORTH"
    zones[0] = [("h1", "LZ_AEN")]  # the hub's bank moved zone
    clock[0] = 30.0
    assert await port.hub_zone("h1") == "LZ_NORTH"  # cached within the refresh
    clock[0] = 61.0
    assert await port.hub_zone("h1") == "LZ_AEN"
    assert pool.count(flow_repo._HUB_ZONES_SQL) == 2


async def test_an_unknown_hub_reloads_the_zone_cache_at_most_every_few_seconds():
    pool = FakePool({flow_repo._HUB_ZONES_SQL: [("h1", "LZ_NORTH")]})
    clock = [0.0]
    port = flow_repo.PgTerritoryPort(pool, {}, monotonic_fn=lambda: clock[0])  # type: ignore[arg-type]
    await port.hub_zone("h1")

    clock[0] = 1.0
    assert await port.hub_zone("ghost") is None
    assert pool.count(flow_repo._HUB_ZONES_SQL) == 1
    clock[0] = 7.0
    assert await port.hub_zone("ghost") is None
    assert pool.count(flow_repo._HUB_ZONES_SQL) == 2


# --- 7. fail closed on missing topology ----------------------------------------------------------------------------------


async def test_a_feeder_without_limit_rows_takes_the_defaults_unless_fail_closed():
    readings = {"b1": (1.0, 1.0, None, None), "b2": (1.0, 1.0, None, None)}

    permissive = await _port(_topology_pool(readings)).feeder_flow("f1")
    strict = await _port(_topology_pool(readings), flow_fail_closed_missing_topology=True).feeder_flow("f1")
    with_row = await _port(
        _topology_pool(readings, feeder_limits=[("f1", 100.0, 10.0)]), flow_fail_closed_missing_topology=True
    ).feeder_flow("f1")

    assert permissive is not None and permissive.lower_kw == -3_000.0 and permissive.upper_kw == 9_500.0
    assert strict is not None and strict.lower_kw is None and strict.upper_kw is None
    assert with_row is not None and with_row.lower_kw == -10.0 and with_row.upper_kw == pytest.approx(95.0)
    vetoed = flow_checks.check_aggregate_flow(
        "G-28", strict, 0.0, -1.0, max_age_s=30.0, reverse_reason="R", forward_reason="F", ref="f1"
    )
    assert not vetoed.ok and vetoed.reason == "FLOW_UNKNOWN"


def test_the_flow_flags_are_read_from_config():
    cfg = load_guardian_config(
        Config({"guardian": {"flow": {"fail_closed_missing_topology": True, "scada_min_power_factor": 0.95}}})
    )
    assert cfg.flow_fail_closed_missing_topology and cfg.scada_min_power_factor == 0.95
    defaults = load_guardian_config(Config({}))
    assert not defaults.flow_fail_closed_missing_topology and defaults.scada_min_power_factor == 0.0
    with pytest.raises(ValueError, match="scada_min_power_factor"):
        load_guardian_config(Config({"guardian": {"flow": {"scada_min_power_factor": 1.5}}}))


# --- a bank with no feeder mapping ----------------------------------------------------------------------------------------


def _unmapped_world(fakes, p_kw: float, prev_kw: float):
    from .test_service_flow import FakeAlerts

    proposal = _batch(fakes, [ProposedItem(HUB_ID, p_kw, "SELECTOR")], prev={HUB_ID: prev_kw})
    fakes.hubs.hubs[HUB_ID] = replace(
        fakes.hubs.hubs[HUB_ID], params=replace(fakes.hubs.hubs[HUB_ID].params, p_kw=11.0)
    )
    assert fakes.banks.banks[BANK_ID].feeder_id is None
    return proposal, FakeAlerts()


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_a_bank_without_a_feeder_raises_the_unmapped_alert(fakes, signing_seed, fail_closed):
    proposal, alerts = _unmapped_world(fakes, 3.0, 3.0)  # no change: never vetoed
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, flow_fail_closed_missing_topology=fail_closed)

    verdict = await _service(
        fakes, config, signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "PASS"
    assert ("ALR-BANK-UNMAPPED-TOPOLOGY", BANK_ID) in alerts.raised


async def test_fail_closed_vetoes_increases_on_an_unmapped_bank_and_passes_relief(fakes, signing_seed):
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, flow_fail_closed_missing_topology=True)
    proposal, alerts = _unmapped_world(fakes, -4.0, -2.0)

    verdict = await _service(
        fakes, config, signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "VETOED" and "G-28" in verdict.vetoed_rule_ids
    violations = fakes.trace.appended[-1][1]["violations"]
    assert (BANK_ID, flow_checks.BANK_TOPOLOGY_UNMAPPED) in {(v["hub_id"], v["reason"]) for v in violations}

    relief, _ = _unmapped_world(fakes, -1.0, -2.0)
    relief = replace(relief, seq=2)
    fakes.proposals.add(relief)
    relief_verdict = await _service(
        fakes, config, signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(relief))
    assert "G-28" not in relief_verdict.vetoed_rule_ids, relief_verdict.vetoed_rule_ids


async def test_without_fail_closed_an_unmapped_bank_is_only_alerted(fakes, signing_seed):
    config = GuardianConfig(key_path="", cycle_interval_s=2.0)
    proposal, alerts = _unmapped_world(fakes, -4.0, -2.0)

    verdict = await _service(
        fakes, config, signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert "G-28" not in verdict.vetoed_rule_ids
    assert ("ALR-BANK-UNMAPPED-TOPOLOGY", BANK_ID) in alerts.raised


def test_check_unmapped_bank_relief_semantics():
    assert not flow_checks.check_unmapped_bank("b", 0.0, -1.0).ok
    assert not flow_checks.check_unmapped_bank("b", 2.0, 3.0).ok
    assert flow_checks.check_unmapped_bank("b", -5.0, -1.0).ok
    assert flow_checks.check_unmapped_bank("b", 5.0, 1.0).ok
    assert flow_checks.check_unmapped_bank("b", -5.0, -5.0).ok  # unchanged
    assert flow_checks.check_unmapped_bank("b", -5.0, 0.0).ok  # to zero from either side
    assert flow_checks.check_unmapped_bank("b", 5.0, 0.0).ok
    assert flow_checks.check_unmapped_bank("b", 0.0, 0.0).ok


# --- r3.4.1 (workstation review LOW): a sign flip is never relief -------------------------------------------------------


@pytest.mark.parametrize(
    ("prev_kw", "new_kw"),
    [
        (200.0, -250.0),  # the review's case: import to a larger export
        (200.0, -150.0),  # a flip is never relief, even at a smaller magnitude
        (-200.0, 150.0),  # export to import
        (-5.0, 5.0),  # same magnitude, other side
    ],
)
def test_a_sign_flip_on_an_unmapped_bank_is_never_relief(prev_kw, new_kw):
    outcome = flow_checks.check_unmapped_bank("b", prev_kw, new_kw)

    assert not outcome.ok and outcome.reason == flow_checks.BANK_TOPOLOGY_UNMAPPED


async def test_a_sign_flip_batch_on_an_unmapped_bank_is_vetoed_through_the_service(fakes, signing_seed):
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, flow_fail_closed_missing_topology=True)
    proposal, alerts = _unmapped_world(fakes, -2.5, 2.0)  # charging 2 kW -> discharging 2.5 kW

    verdict = await _service(
        fakes, config, signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "VETOED" and "G-28" in verdict.vetoed_rule_ids
    violations = fakes.trace.appended[-1][1]["violations"]
    assert (BANK_ID, flow_checks.BANK_TOPOLOGY_UNMAPPED) in {(v["hub_id"], v["reason"]) for v in violations}


# --- r3.4.1 (M11): missing substation topology is alerted, and fail-closed vetoes increases ------------------------------


def _fed_bank_world(fakes, p_kw: float, prev_kw: float):
    """A bank WITH a feeder mapping, so only the substation topology is missing."""
    proposal, alerts = _unmapped_world(fakes, p_kw, prev_kw)
    fakes.banks.banks[BANK_ID] = replace(fakes.banks.banks[BANK_ID], feeder_id="f1")
    return proposal, alerts


def _flow_config(fail_closed: bool) -> GuardianConfig:
    return GuardianConfig(
        key_path="",
        cycle_interval_s=2.0,
        default_feeder_ramp_ceiling_kw_per_min=1e9,
        flow_fail_closed_missing_topology=fail_closed,
    )


async def test_a_bank_without_substation_topology_is_alerted_and_g29_skipped(fakes, signing_seed):
    proposal, alerts = _fed_bank_world(fakes, -4.0, -2.0)

    verdict = await _service(
        fakes, _flow_config(False), signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert "G-29" not in verdict.vetoed_rule_ids
    assert ("ALR-SUBSTATION-UNMAPPED-TOPOLOGY", BANK_ID) in alerts.raised
    assert ("ALR-BANK-UNMAPPED-TOPOLOGY", BANK_ID) not in alerts.raised  # the feeder IS mapped


@pytest.mark.parametrize(
    ("p_kw", "prev_kw", "vetoed"),
    [(-4.0, -2.0, True), (2.5, -2.0, True), (-1.0, -2.0, False), (-2.0, -2.0, False)],
)
async def test_fail_closed_vetoes_increases_and_flips_on_a_bank_without_substation_topology(
    fakes, signing_seed, p_kw, prev_kw, vetoed
):
    proposal, alerts = _fed_bank_world(fakes, p_kw, prev_kw)

    verdict = await _service(
        fakes, _flow_config(True), signing_seed, topology=FakeTopology(), alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert ("G-29" in verdict.vetoed_rule_ids) is vetoed
    if vetoed:
        violations = fakes.trace.appended[-1][1]["violations"]
        assert (BANK_ID, flow_checks.SUBSTATION_TOPOLOGY_UNMAPPED) in {
            (v["hub_id"], v["reason"]) for v in violations
        }
    assert ("ALR-SUBSTATION-UNMAPPED-TOPOLOGY", BANK_ID) in alerts.raised


async def test_a_mapped_substation_raises_no_substation_alert(fakes, signing_seed):
    topology = FakeTopology()
    topology.substations[BANK_ID] = AggregateFlow("sub-1", 5.0, 1.0, -100.0, 1000.0)
    proposal, alerts = _fed_bank_world(fakes, -4.0, -2.0)

    verdict = await _service(
        fakes, _flow_config(True), signing_seed, topology=topology, alerts=alerts
    ).evaluate_and_sign(make_batch_row(proposal))

    assert "G-29" not in verdict.vetoed_rule_ids
    assert ("ALR-SUBSTATION-UNMAPPED-TOPOLOGY", BANK_ID) not in alerts.raised


def _home_bank_pool(*, with_limit_row: bool) -> FakePool:
    """TOPOLOGY-FIX's r3.4.1 data: HOME_BANK og.asset rows map the home banks to their substation."""
    pool = _topology_pool({"b1": (-5.0, 1.0, None, None), "b2": (2.0, 1.0, None, None)})
    pool.responses[flow_repo._ASSETS_SQL] = [
        ("home-b1", "HOME_BANK", "b1", "sub-1", None, None),
        ("home-b2", "HOME_BANK", "b2", "sub-1", None, None),
    ]
    pool.responses[flow_repo._SUBSTATION_LIMITS_SQL] = [("sub-1", 1000.0, 200.0)] if with_limit_row else []
    return pool


async def test_home_bank_asset_rows_make_g29_evaluate_for_home_banks():
    port = _port(_home_bank_pool(with_limit_row=True))

    flow = await port.substation_flow("b1")

    assert flow is not None and flow.ref == "sub-1" and flow.banks == ("b1", "b2")
    assert flow.flow_kw == -3.0 and flow.lower_kw == -200.0 and flow.upper_kw == pytest.approx(950.0)
    assert await port.poi_limit("b1") is None  # HOME_BANK rows are not a POI


async def test_a_mapped_substation_without_limits_is_missing_topology_unless_fail_closed():
    permissive = await _port(_home_bank_pool(with_limit_row=False)).substation_flow("b1")
    strict = await _port(
        _home_bank_pool(with_limit_row=False), flow_fail_closed_missing_topology=True
    ).substation_flow("b1")

    assert permissive is None  # skipped (and alerted by the service), not a veto of every increase
    assert (
        strict is not None and strict.lower_kw is None and strict.upper_kw is None
    )  # unknown: increases vetoed
