"""r3.4.3 (lead): the 20 MW Austin toll through the FULL guardian check set, cycle after cycle.

sub-LZ_AEN-00 (D-29, og.asset SUBSTATION, utility-scale) on its own bank/feeder, with prod-like premise and
feeder data: export/service premise = its 20,408 kVA transformer rating, feeder ramp ceiling 7,000 kW/min
(above the toll's ~6,667 kW/min at the firm hub rate), feeder/substation/territory flows with room for 20 MW, a
REGULATED Austin Energy obligation served inside its territory, and the hub's telemetry arriving only every ~10 s
(5 cycles) while the engine steps it every 2 s cycle. Every check wired into `GuardianService` runs:
G-01/G-01-ENERGY, G-02, G-03, G-04, G-05 (+ stagger), G-06, G-13, G-19, G-26..G-31, G-32 (non-firm variant),
G-33, G-34. Before the r3.4.0.1 anchor fix G-04 vetoed from cycle 2 and G-05/G-32 saw the same stale step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opengrid.core.physics import BankParams, HubParams, hub_ramp_kw_per_s
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import (
    AggregateFlow,
    BankSnapshot,
    HubSite,
    HubSnapshot,
    ObligationMarket,
    PoiLimit,
    ProposedBatch,
    ProposedItem,
    ServiceTransformer,
)
from opengrid.guardian.service import GuardianService

from .conftest import NOW, Fakes, make_batch_row
from .test_service_flow import FakeAlerts, FakeTerritory, FakeTopology

HUB = "sub-LZ_AEN-00"
BANK = "bank-sub-LZ_AEN-00"
FEEDER = "feeder-sub-LZ_AEN-00"
XFMR = "xfmr-bank-sub-LZ_AEN-00-00"
PREMISE_KW = 20_408.0  # owner decision: the set's export/service premise is its transformer rating
TOLL_KW = 20_000.0
FEEDER_CEILING_KW_PER_MIN = 7_000.0
CYCLE_S = 2.0
TELEMETRY_EVERY = 5  # ~10 s telemetry vs a 2 s engine cycle
PARAMS = HubParams(e_kwh=40_000.0, r_kwh=8_000.0, p_kw=TOLL_KW, utility_scale=True)
STEP_KW = hub_ramp_kw_per_s(PARAMS) * CYCLE_S  # the engine's per-cycle step at the firm hub rate


@dataclass
class TollWorld:
    fakes: Fakes
    service: GuardianService
    toll: UUID
    clock: list[datetime]
    seq: int = 0
    granted: list[float] = field(default_factory=list)


def _config() -> GuardianConfig:
    return GuardianConfig(
        key_path="",
        cycle_interval_s=CYCLE_S,
        feeder_ramp_ceiling_kw_per_min={FEEDER: FEEDER_CEILING_KW_PER_MIN, "feeder-LZ_AEN-00": 7_000.0},
    )


def _world(fakes: Fakes, signing_seed: bytes) -> TollWorld:
    toll = uuid4()
    topology = FakeTopology()
    topology.hub_banks[HUB] = BANK
    topology.sites[HUB] = HubSite(
        export_limit_kw=PREMISE_KW,
        service_kw=PREMISE_KW,
        pv_rated_kw=0.0,
        peak_kw=None,
        tau_peak_s=None,
        transformer_id=XFMR,
    )
    topology.transformers[XFMR] = ServiceTransformer(XFMR, PREMISE_KW, (HUB,))
    # the set's own feeder (seed og.feeder_limit: thermal x 0.95 and reverse both >= 20 MW), a small base import
    topology.feeders[FEEDER] = AggregateFlow(FEEDER, 300.0, 1.0, lower_kw=-21_000.0, upper_kw=23_750.0)
    topology.substations[BANK] = AggregateFlow(
        "sub-LZ_AEN-00", 300.0, 1.0, lower_kw=-40_000.0, upper_kw=40_000.0
    )
    # Austin Energy territory imports ~2.5 GW: a 20 MW discharge never makes it a net exporter (G-30, K15)
    topology.territories[BANK] = AggregateFlow(
        "AUSTIN_ENERGY", 2_500_000.0, 1.0, lower_kw=0.0, upper_kw=math.inf
    )
    topology.pois[BANK] = PoiLimit("sub-LZ_AEN-00", import_kw=PREMISE_KW, export_kw=PREMISE_KW)
    territory = FakeTerritory()
    territory.zones[HUB] = "LZ_AEN"
    territory.markets[toll] = ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY")

    fakes.hubs.hubs[HUB] = HubSnapshot(
        params=PARAMS, soc_kwh=32_000.0, prev_p_kw=0.0, health="online", telemetry_at=NOW
    )
    fakes.banks.banks[BANK] = BankSnapshot(
        params=BankParams(kva_rating=40_000.0, reserve_kva=5.0),
        bank_load_kva=300.0,
        feeder_id=FEEDER,
        feeder_ceiling_kw_per_min=None,  # the configured per-feeder ceiling applies
        bank_load_age_s=1.0,
    )
    fakes.commitments.frozen[toll] = Decimal(TOLL_KW)  # the toll's 20 MW lock
    fakes.commitments.active_by_bank[BANK] = {toll}
    fakes.prior_grants.prior[toll] = Decimal(0)  # nothing granted before the call

    clock = [NOW]
    service = GuardianService(
        ports=replace(fakes.as_ports(), topology=topology, territory=territory, alerts=FakeAlerts()),
        config=_config(),
        signing_seed=signing_seed,
        now_fn=lambda: clock[0],
    )
    return TollWorld(fakes, service, toll, clock)


def _proposal(world: TollWorld, target_kw: float, *, firm: bool) -> ProposedBatch:
    world.seq += 1
    now = world.clock[0]
    proposal = ProposedBatch(
        command_batch_id=uuid4(),
        bank_id=BANK,
        cycle_id=f"cycle-{world.seq}",
        epoch=1,
        seq=world.seq,
        issued_at=now,
        expires_at=now + timedelta(seconds=10),
        ledger_version=world.fakes.ledger.version,
        items=[
            ProposedItem(
                hub_id=HUB,
                p_kw_setpoint=-target_kw,  # discharge
                reason_code="R-GRANT-COMMITTED",
                obligation_id=world.toll,
                obligation_granted_kw=Decimal(str(round(target_kw, 3))),
            )
        ],
        is_firm_event=firm,
    )
    world.fakes.proposals.add(proposal)
    world.fakes.leases.last[BANK] = (1, world.seq - 1)
    return proposal


async def _cycle(world: TollWorld, target_kw: float, *, firm: bool = True) -> tuple[str, list[str]]:
    proposal = _proposal(world, target_kw, firm=firm)
    verdict = await world.service.evaluate_and_sign(make_batch_row(proposal))
    if verdict.outcome == "PASS":
        world.service.confirm_published(proposal.command_batch_id)  # published: the hub follows it
        world.fakes.prior_grants.prior[world.toll] = Decimal(str(round(target_kw, 3)))
        world.granted.append(target_kw)
    return verdict.outcome, list(verdict.vetoed_rule_ids)


def _advance(world: TollWorld, n: int) -> None:
    """One engine cycle later; every TELEMETRY_EVERY cycles the hub reports what it was doing a cycle ago."""
    world.clock[0] += timedelta(seconds=CYCLE_S)
    if n % TELEMETRY_EVERY == 0 and len(world.granted) >= 2:
        hub = world.fakes.hubs.hubs[HUB]
        world.fakes.hubs.hubs[HUB] = replace(
            hub, prev_p_kw=-world.granted[-2], telemetry_at=world.clock[0] - timedelta(seconds=CYCLE_S)
        )


@pytest.mark.parametrize("firm", [True, False], ids=["firm-G06", "non-firm-G32"])
async def test_the_20mw_toll_ramp_passes_every_guardian_check_for_10_cycles(fakes, signing_seed, firm):
    world = _world(fakes, signing_seed)
    for n in range(1, 11):
        outcome, vetoed = await _cycle(world, STEP_KW * n, firm=firm)
        assert (outcome, vetoed) == ("PASS", []), f"cycle {n}: {vetoed} {fakes.trace.appended[-1][1]}"
        _advance(world, n)
    assert world.granted[-1] == pytest.approx(10 * STEP_KW)


async def test_the_toll_at_its_full_20mw_passes_every_check_for_10_cycles(fakes, signing_seed):
    """Holding the full 20 MW (telemetry caught up): every check signs every cycle."""
    world = _world(fakes, signing_seed)
    fakes.prior_grants.prior[world.toll] = Decimal(TOLL_KW)
    fakes.hubs.hubs[HUB] = replace(fakes.hubs.hubs[HUB], prev_p_kw=-TOLL_KW)
    for n in range(1, 11):
        outcome, vetoed = await _cycle(world, TOLL_KW)
        assert (outcome, vetoed) == ("PASS", []), f"cycle {n}: {vetoed} {fakes.trace.appended[-1][1]}"
        _advance(world, n)


async def test_an_overstep_mid_ramp_is_still_vetoed_by_g04(fakes, signing_seed):
    world = _world(fakes, signing_seed)
    for n in range(1, 4):
        assert (await _cycle(world, STEP_KW * n))[0] == "PASS"
        _advance(world, n)
    outcome, vetoed = await _cycle(world, STEP_KW * 3 + 1.5 * STEP_KW)
    assert outcome == "VETOED" and "G-04" in vetoed
