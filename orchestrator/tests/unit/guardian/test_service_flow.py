"""GuardianService wiring of the 09 S2.6 flow checks (G-02 per-hub sum, G-05 stagger, G-26..G-33)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from opengrid.core.models.mqtt import Telemetry
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import (
    AggregateFlow,
    HubFlowTelemetry,
    HubSite,
    ObligationMarket,
    PoiLimit,
    ProposedItem,
    Reading,
    ServiceTransformer,
)

from .conftest import (
    BANK_ID,
    make_bank_snapshot,
    make_batch_row,
    make_hub_snapshot,
    make_proposal,
    service_with,
)
from .conftest import wire_default_passing_scenario as wire

SITE = HubSite(
    export_limit_kw=20.0, service_kw=48.0, pv_rated_kw=0.0, peak_kw=None, tau_peak_s=None, transformer_id=None
)


class FakeTopology:
    def __init__(self) -> None:
        self.sites: dict[str, HubSite] = {}
        self.transformers: dict[str, ServiceTransformer] = {}
        self.feeders: dict[str, AggregateFlow] = {}
        self.substations: dict[str, AggregateFlow] = {}
        self.territories: dict[str, AggregateFlow] = {}
        self.pois: dict[str, PoiLimit] = {}
        self.hub_banks: dict[str, str | None] = {}  # og.hub bank per hub; unlisted hubs sit on BANK_ID

    async def hub_site(self, hub_id):
        return self.sites.get(hub_id, SITE)

    async def hub_bank(self, hub_id):
        return self.hub_banks.get(hub_id, BANK_ID)

    async def transformer(self, transformer_id):
        return self.transformers.get(transformer_id)

    async def feeder_flow(self, feeder_id):
        return self.feeders.get(feeder_id)

    async def substation_flow(self, bank_id):
        return self.substations.get(bank_id)

    async def territory_flow(self, bank_id):
        return self.territories.get(bank_id)

    async def poi_limit(self, bank_id):
        return self.pois.get(bank_id)


class FakeTerritory:
    def __init__(self) -> None:
        self.zones: dict[str, str] = {}
        self.markets: dict = {}
        self.access: set[str] = set()
        self.availability: dict = {}
        self.grandfathered_pairs: set[tuple[str, str]] = set()

    def zone_territory(self):
        return {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}

    async def hub_zone(self, hub_id):
        return self.zones.get(hub_id, "LZ_NORTH")

    async def obligation_market(self, obligation_id):
        return self.markets.get(obligation_id, ObligationMarket("FREE", None, "ERCOT_ENERGY"))

    async def free_access(self, utility_id):
        return utility_id in self.access

    async def bank_availability(self, bank_id):  # D-37: every bank AVAILABLE unless a test says so
        return self.availability.get(bank_id)

    async def grandfathered(self, obligation_id, bank_id):
        return (str(obligation_id), bank_id) in self.grandfathered_pairs


class FakeAlerts:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str]] = []

    async def raise_alert(self, rule, severity, summary, condition_key, detail):
        self.raised.append((rule, condition_key))

    async def clear_alert(self, rule, condition_key):
        return None


def _service(fakes, config, seed, *, topology=None, territory=None, alerts=None):
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), topology=topology, territory=territory, alerts=alerts)
    return service


def _batch(fakes, items, *, prev: dict[str, float] | None = None, **proposal_kw):
    proposal = replace(make_proposal(**proposal_kw), items=items)
    wire(fakes, proposal)
    for hub_id, p in (prev or {}).items():
        fakes.hubs.hubs[hub_id] = replace(fakes.hubs.hubs[hub_id], prev_p_kw=p)
    return proposal


def _big_hub(**kw):
    snap = make_hub_snapshot(soc_kwh=35.0, p_kw=11.0, **kw)
    return replace(snap, params=replace(snap.params, ramp_kw_per_s=1000.0))


# --- per-hub sum (lead, top priority) -----------------------------------------------------------------------


async def test_two_items_on_one_hub_are_checked_on_their_sum(fakes, guardian_config, signing_seed):
    items = [ProposedItem("hub-0001", -8.0, "R-GRANT-COMMITTED", uuid4()) for _ in range(2)]
    proposal = _batch(fakes, items)
    fakes.hubs.hubs["hub-0001"] = _big_hub(prev_p_kw=-16.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.signature is None and "G-02" in verdict.vetoed_rule_ids


async def test_split_items_within_the_hub_rating_still_sign(fakes, guardian_config, signing_seed):
    items = [ProposedItem("hub-0001", -4.0, "R-GRANT-COMMITTED", uuid4()) for _ in range(2)]
    proposal = _batch(fakes, items)
    fakes.hubs.hubs["hub-0001"] = _big_hub(prev_p_kw=-8.0)

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "PASS"


# --- G-05 stagger --------------------------------------------------------------------------------------------


async def test_an_unstaggered_step_of_many_hubs_is_vetoed_even_when_it_nets_to_zero(fakes, signing_seed):
    """Gross sum(|delta p|) in one tick above cap/30: +x on half the hubs and -x on the other half nets 0."""
    items = [ProposedItem(f"hub-{i:04d}", 5.0 if i % 2 else -5.0, "R") for i in range(40)]
    proposal = _batch(fakes, items)
    for it in items:
        fakes.hubs.hubs[it.hub_id] = _big_hub(prev_p_kw=0.0)
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(kva_rating=10_000.0)
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, discretionary_ramp_cap_kw_per_min=5_000.0)

    verdict = await service_with(fakes, config, signing_seed).evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "VETOED" and "G-05" in verdict.vetoed_rule_ids
    assert {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]} >= {"SYNC_STEP_LIMIT"}


# --- G-26 / G-27 -------------------------------------------------------------------------------------------------


async def test_transformer_violation_vetoes_every_item_under_it_and_only_those(
    fakes, guardian_config, signing_seed
):
    topology = FakeTopology()
    topology.transformers["x1"] = ServiceTransformer("x1", 8.0, ("hub-a", "hub-b"))
    topology.sites["hub-a"] = replace(SITE, transformer_id="x1")
    topology.sites["hub-b"] = replace(SITE, transformer_id="x1")
    topology.sites["hub-c"] = replace(SITE, transformer_id="x2")
    topology.transformers["x2"] = ServiceTransformer("x2", 50.0, ("hub-c",))
    items = [ProposedItem(h, -6.0, "R") for h in ("hub-a", "hub-b", "hub-c")]
    proposal = _batch(fakes, items)
    for h in ("hub-a", "hub-b", "hub-c"):
        fakes.hubs.hubs[h] = replace(
            _big_hub(prev_p_kw=-6.0), flow=HubFlowTelemetry(meter_kw=Reading(-6.0 + 1.0, 1.0))
        )
    fakes.hubs.hubs["hub-a"] = replace(fakes.hubs.hubs["hub-a"], prev_p_kw=-2.0)
    fakes.hubs.hubs["hub-a"] = replace(
        fakes.hubs.hubs["hub-a"], flow=HubFlowTelemetry(meter_kw=Reading(-1.0, 1.0))
    )
    fakes.banks.banks[BANK_ID] = make_bank_snapshot(kva_rating=10_000.0)

    service = _service(fakes, guardian_config, signing_seed, topology=topology)
    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome == "PARTLY_VETOED" and verdict.vetoed_rule_ids == ["G-27"]
    vetoed = {v["hub_id"] for v in fakes.trace.appended[-1][1]["violations"]}
    assert vetoed == {"hub-a"}  # the trace keeps one example per (rule, reason); the group is both a and b
    outcome = service.batch_outcome(verdict)
    assert outcome is not None and outcome.vetoed_commands == 2


async def test_unmapped_hubs_are_a_group_of_one_and_raise_the_alert(fakes, guardian_config, signing_seed):
    topology, alerts = FakeTopology(), FakeAlerts()
    proposal = _batch(fakes, [ProposedItem("hub-0001", -8.0, "R")])
    fakes.hubs.hubs["hub-0001"] = replace(
        _big_hub(prev_p_kw=-2.0), flow=HubFlowTelemetry(meter_kw=Reading(-1.0, 1.0))
    )

    service = _service(fakes, guardian_config, signing_seed, topology=topology, alerts=alerts)
    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-27" in verdict.vetoed_rule_ids  # -1 - 6 = -7 kW through a 5 kVA default
    assert ("ALR-XFMR-UNMAPPED", BANK_ID) in alerts.raised


async def test_home_export_limit_is_enforced_through_the_service(fakes, guardian_config, signing_seed):
    topology = FakeTopology()
    topology.sites["hub-0001"] = replace(SITE, export_limit_kw=2.0)
    topology.transformers["x"] = ServiceTransformer("x", 100.0, ("hub-0001",))
    topology.sites["hub-0001"] = replace(topology.sites["hub-0001"], transformer_id="x")
    proposal = _batch(fakes, [ProposedItem("hub-0001", -6.0, "R")])
    fakes.hubs.hubs["hub-0001"] = replace(
        _big_hub(prev_p_kw=0.0), flow=HubFlowTelemetry(meter_kw=Reading(1.0, 1.0))
    )

    verdict = await _service(fakes, guardian_config, signing_seed, topology=topology).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.vetoed_rule_ids == ["G-26"]


# --- G-28 / G-29 / G-30 / G-32 ---------------------------------------------------------------------------------------


def _feeder_world(fakes, topology, bank_id, delta_kw, *, cycle="cycle-1"):
    proposal = replace(
        _batch(fakes, [ProposedItem(f"{bank_id}-hub", delta_kw, "R")], bank_id=bank_id), cycle_id=cycle
    )
    fakes.proposals.add(proposal)
    fakes.hubs.hubs[f"{bank_id}-hub"] = _big_hub(prev_p_kw=0.0)
    fakes.hubs.hubs[f"{bank_id}-hub"] = replace(
        fakes.hubs.hubs[f"{bank_id}-hub"],
        params=replace(fakes.hubs.hubs[f"{bank_id}-hub"].params, p_kw=20.0, units=2),
    )
    fakes.banks.banks[bank_id] = make_bank_snapshot(kva_rating=10_000.0, feeder_id="f1", bank_load_kva=5.0)
    xfmr = f"x-{bank_id}"
    topology.hub_banks[f"{bank_id}-hub"] = bank_id
    topology.sites[f"{bank_id}-hub"] = replace(SITE, transformer_id=xfmr)
    topology.transformers[xfmr] = ServiceTransformer(xfmr, 1_000.0, (f"{bank_id}-hub",))
    return proposal


async def test_two_banks_jointly_reversing_the_feeder_head_are_vetoed(fakes, signing_seed):
    topology = FakeTopology()
    topology.feeders["f1"] = AggregateFlow("f1", 10.0, 1.0, lower_kw=-20.0, upper_kw=1000.0)
    config = GuardianConfig(
        key_path="",
        cycle_interval_s=2.0,
        default_feeder_ramp_ceiling_kw_per_min=1e9,
        discretionary_ramp_cap_kw_per_min=1e9,
        non_firm_ramp_cap_kw_per_min=1e9,
    )
    service = _service(fakes, config, signing_seed, topology=topology)
    first = _feeder_world(fakes, topology, "bank-a", -18.0)
    assert (await service.evaluate_and_sign(make_batch_row(first))).outcome == "PASS"
    second = _feeder_world(fakes, topology, "bank-b", -18.0)
    verdict = await service.evaluate_and_sign(make_batch_row(second))

    assert verdict.outcome == "VETOED" and "G-28" in verdict.vetoed_rule_ids


async def test_stale_feeder_scada_vetoes_increases(fakes, signing_seed):
    topology = FakeTopology()
    topology.feeders["f1"] = AggregateFlow("f1", 10.0, math.inf, lower_kw=-20.0, upper_kw=1000.0)
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    service = _service(fakes, config, signing_seed, topology=topology)
    proposal = _feeder_world(fakes, topology, "bank-a", -1.0)

    assert "G-28" in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


async def test_territory_export_and_substation_limits(fakes, signing_seed):
    topology = FakeTopology()
    topology.territories["bank-a"] = AggregateFlow("AUSTIN_ENERGY", 5.0, 1.0, 0.0, math.inf)
    topology.substations["bank-a"] = AggregateFlow("sub-1", 5.0, 1.0, None, 100.0)
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    service = _service(fakes, config, signing_seed, topology=topology)
    proposal = _feeder_world(fakes, topology, "bank-a", -10.0)

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert {"G-29", "G-30"} <= set(verdict.vetoed_rule_ids)


async def test_substation_asset_poi(fakes, signing_seed):
    topology = FakeTopology()
    topology.pois["bank-a"] = PoiLimit("sub-asset", import_kw=5.0, export_kw=5.0)
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    proposal = _feeder_world(fakes, topology, "bank-a", -10.0)

    verdict = await _service(fakes, config, signing_seed, topology=topology).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-29" in verdict.vetoed_rule_ids


async def test_non_firm_feeder_ramp_is_g32(fakes, signing_seed):
    topology = FakeTopology()
    topology.feeders["f1"] = AggregateFlow("f1", 10.0, 1.0, -1000.0, 1000.0)
    config = GuardianConfig(
        key_path="",
        cycle_interval_s=2.0,
        default_feeder_ramp_ceiling_kw_per_min=60.0,  # 2 kW per 2-s tick
        discretionary_ramp_cap_kw_per_min=1e9,
        non_firm_ramp_cap_kw_per_min=1e9,
    )
    proposal = _feeder_world(fakes, topology, "bank-a", -3.0)

    verdict = await _service(fakes, config, signing_seed, topology=topology).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert "G-32" in verdict.vetoed_rule_ids and "G-06" not in verdict.vetoed_rule_ids


# --- G-33 -------------------------------------------------------------------------------------------------------------


async def test_regulated_obligation_served_outside_its_territory_is_vetoed(
    fakes, guardian_config, signing_seed
):
    territory = FakeTerritory()
    reg = uuid4()
    territory.markets[reg] = ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY")
    territory.zones = {"hub-in": "LZ_AEN", "hub-out": "LZ_NORTH"}
    items = [
        ProposedItem("hub-out", -2.0, "R-GRANT-COMMITTED", reg),
        ProposedItem("hub-in", -2.0, "R-GRANT-COMMITTED", reg),
    ]
    proposal = _batch(fakes, items)
    fakes.commitments.active_by_bank.pop(BANK_ID, None)

    verdict = await _service(fakes, guardian_config, signing_seed, territory=territory).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "PARTLY_VETOED" and verdict.vetoed_rule_ids == ["G-33"]
    violations = fakes.trace.appended[-1][1]["violations"]
    assert [(v["hub_id"], v["reason"]) for v in violations] == [("hub-out", "R-TERRITORY-OUTSIDE")]


async def test_free_headroom_inside_a_territory_needs_wholesale_access(fakes, guardian_config, signing_seed):
    territory = FakeTerritory()
    territory.zones = {"hub-0001": "LZ_AEN"}
    proposal = _batch(fakes, [ProposedItem("hub-0001", -2.0, "R-GRANT-HEADROOM")])
    service = _service(fakes, guardian_config, signing_seed, territory=territory)

    assert (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids == ["G-33"]
    territory.access.add("AUSTIN_ENERGY")
    proposal2 = _batch(fakes, [ProposedItem("hub-0001", -2.0, "R-GRANT-HEADROOM")], seq=2)
    assert (await service.evaluate_and_sign(make_batch_row(proposal2))).outcome == "PASS"


# --- the guardian's own telemetry cache carries the flow fields as they land ------------------------------------


def _wire_telemetry(**flow: float) -> Telemetry:
    """A real `core.models.mqtt.Telemetry` (the wire model), never a stand-in with guessed field names."""
    return Telemetry(
        hub_id="hub-1",
        bank_id=BANK_ID,
        zone="LZ_NORTH",
        ts=datetime(2026, 9, 26, 18, 0, tzinfo=UTC),
        soc_kwh=20.0,
        p_kw=-3.0,
        health="online",
        seq=1,
        epoch=1,
        **flow,
    )


async def test_mqtt_cache_ages_flow_fields_and_keeps_never_reported_ones_absent():
    from opengrid.guardian.mqtt_io import MqttHubStatePort

    clock = [100.0]
    seed = replace(make_hub_snapshot(), health="stale")
    cache = MqttHubStatePort({"hub-1": seed}, max_age_s=60.0, monotonic_fn=lambda: clock[0])
    cache.ingest(_wire_telemetry(meter_kw=-1.5, cell_temp_c=31.0))
    clock[0] = 105.0

    snap = await cache.snapshot("hub-1")

    assert snap is not None
    assert snap.flow.meter_kw == Reading(-1.5, 5.0)
    assert snap.flow.cell_temp_c == Reading(31.0, 5.0)
    assert snap.flow.pv_kw is None and snap.flow.peak_budget_kws is None  # never reported by this hub


async def test_the_wire_peak_power_budget_reaches_g31(fakes, guardian_config, signing_seed):
    """R2 review fix: the wire field is `peak_power_budget_kws`; the cache read `peak_budget_kws`, which the
    Telemetry model never has, so G-31's budget was always None and every above-continuous setpoint was
    vetoed. With the budget on the wire, a short peak within it is signed."""
    from opengrid.guardian import flow_checks
    from opengrid.guardian.mqtt_io import MqttHubStatePort

    cache = MqttHubStatePort(
        {"hub-1": make_hub_snapshot(p_kw=11.0)}, max_age_s=60.0, monotonic_fn=lambda: 0.0
    )
    cache.ingest(_wire_telemetry(peak_power_budget_kws=500.0))

    snap = await cache.snapshot("hub-1")

    assert snap is not None and snap.flow.peak_budget_kws == Reading(500.0, 0.0)
    site = replace(SITE, peak_kw=15.0, tau_peak_s=60.0)
    policy = service_with(fakes, guardian_config, signing_seed)._flow_policy()
    ok = flow_checks.check_g31_peak(ProposedItem("hub-1", -14.0, "R"), snap, site, 10.0, policy)
    assert ok.ok  # 3 kW over continuous x 10 s = 30 kW*s, inside the 500 kW*s budget
    over = flow_checks.check_g31_peak(ProposedItem("hub-1", -14.0, "R"), snap, site, 60.0, policy)
    assert over.ok  # 180 kW*s still inside
    none = replace(snap, flow=replace(snap.flow, peak_budget_kws=None))
    assert not flow_checks.check_g31_peak(ProposedItem("hub-1", -14.0, "R"), none, site, 10.0, policy).ok


# --- D-29c: manual ramps in a regulated/toll territory ------------------------------------------------------


async def _manual_in_aen(fakes, config, seed, p_kw: float, *, reason: str = "R-MANUAL-RAMP"):
    territory = FakeTerritory()
    territory.zones = {"hub-0001": "LZ_AEN"}  # Austin Energy territory, no wholesale access
    proposal = _batch(fakes, [ProposedItem("hub-0001", p_kw, reason)])
    return await _service(fakes, config, seed, territory=territory).evaluate_and_sign(
        make_batch_row(proposal)
    )


async def test_a_manual_charge_or_hold_in_a_regulated_territory_is_allowed(fakes, signing_seed):
    """The utility makes the discharge calls there (D-29c); an operator's charge (owner schedule) or 0 kW hold
    (maintenance) is not a market service and passes G-33."""
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    assert "G-33" not in (await _manual_in_aen(fakes, config, signing_seed, 2.0)).vetoed_rule_ids
    assert "G-33" not in (await _manual_in_aen(fakes, config, signing_seed, 0.0)).vetoed_rule_ids


async def test_a_manual_discharge_in_a_regulated_territory_stays_vetoed(fakes, signing_seed):
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    assert "G-33" in (await _manual_in_aen(fakes, config, signing_seed, -2.0)).vetoed_rule_ids


async def test_headroom_charging_in_a_regulated_territory_is_not_exempted(fakes, signing_seed):
    """Only R-MANUAL-RAMP is exempt: a market (headroom) item there is still judged by G-33."""
    config = GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)
    verdict = await _manual_in_aen(fakes, config, signing_seed, 2.0, reason="R-GRANT-HEADROOM")
    assert "G-33" in verdict.vetoed_rule_ids


def test_a_hub_with_a_manual_charge_and_a_discharging_headroom_item_is_not_exempt():
    from opengrid.guardian.checks import CheckOutcome
    from opengrid.guardian.service import manual_charge_territory_exempt

    proposal = replace(
        make_proposal(),
        items=[ProposedItem("hub-1", 2.0, "R-MANUAL-RAMP"), ProposedItem("hub-1", -3.0, "R-GRANT-HEADROOM")],
    )
    g33 = CheckOutcome("G-33", False, "R-TERRITORY-NO-FREE-ACCESS", "hub-1")
    assert manual_charge_territory_exempt(proposal, [g33]) == [g33]
