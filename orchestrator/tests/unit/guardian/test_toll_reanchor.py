"""r3.4.3 HIGH-A / HIGH-B regressions for the 20 MW toll (the workstation's probe scenarios, r342_g04_sim.py).

The real `GuardianService` (every check wired, sub-LZ_AEN-00's prod-like data from test_toll_ramp_all_checks)
against an engine that follows the DISPATCH contract: it anchors on its last SIGNED setpoint while that lease is
live, else on telemetry (and at 0 kW after a stop release until newer telemetry); it steps at most
0.9 x ramp x one cycle; a veto never drops a live signed anchor (only a lapsed lease or a stop does). The hub follows
its signed setpoint while the lease is live and not stopped, else sits at 0 kW, and reports telemetry every 5 cycles
(or, with `lag_cycles`, every cycle but that many cycles old).

Every scenario must converge to the full 20 MW with a bounded number of vetoes, and the guardian must NEVER sign
a step larger than one cycle's bound from where the hub physically is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opengrid.guardian.ports import ProposedBatch, ProposedItem
from opengrid.guardian.service import GuardianService

from .conftest import make_batch_row
from .test_toll_ramp_all_checks import BANK, CYCLE_S, HUB, STEP_KW, TOLL_KW, TollWorld, _world

LEASE_S = 30.0
TELEMETRY_EVERY = 5
ENGINE_STEP_KW = 0.9 * STEP_KW
EPS = 1e-6


@dataclass
class Sim:
    world: TollWorld
    phys_kw: float = 0.0  # what the hub is doing (discharge negative)
    signed_kw: float | None = None  # the engine's view of its last signed setpoint
    lease_until: datetime | None = None
    stopped: bool = False
    zero_until_telemetry_after: datetime | None = None  # engine: anchor 0 kW after a release
    cycle: int = 0
    vetoes: int = 0
    signed_steps: int = 0
    lag_cycles: int | None = None  # telemetry every cycle, this many cycles old
    engine_drops_on_veto: bool = False  # a misbehaving engine: re-anchors on telemetry after any veto
    hold_item: bool = False  # each batch also carries a 0 kW hold item for the same hub
    history: list[float] = field(default_factory=list)
    hold_obligation: UUID = field(default_factory=uuid4)

    @property
    def now(self) -> datetime:
        return self.world.clock[0]

    def tick(self) -> None:
        self.cycle += 1
        self.world.clock[0] += timedelta(seconds=CYCLE_S)
        if self.stopped or self.lease_until is None or self.lease_until <= self.now:
            self.phys_kw = 0.0  # a stop, or a lapsed lease: the hub ramps itself to 0 kW
        self.history.append(self.phys_kw)
        hubs = self.world.fakes.hubs.hubs
        if self.lag_cycles is not None and len(self.history) > self.lag_cycles:
            lagged_at = self.now - timedelta(seconds=CYCLE_S * self.lag_cycles)
            lagged = self.history[-1 - self.lag_cycles]
            hubs[HUB] = replace(hubs[HUB], prev_p_kw=lagged, telemetry_at=lagged_at)
        elif self.lag_cycles is None and self.cycle % TELEMETRY_EVERY == 0:
            hubs[HUB] = replace(hubs[HUB], prev_p_kw=self.phys_kw, telemetry_at=self.now)

    def engine_anchor(self) -> float:
        hub = self.world.fakes.hubs.hubs[HUB]
        if self.zero_until_telemetry_after is not None:
            if hub.telemetry_at is not None and hub.telemetry_at > self.zero_until_telemetry_after:
                self.zero_until_telemetry_after = None
            else:
                return 0.0
        if self.signed_kw is not None and self.lease_until is not None and self.lease_until > self.now:
            return self.signed_kw
        return hub.prev_p_kw

    async def engine_cycle(self) -> None:
        anchor = self.engine_anchor()
        target = max(-TOLL_KW, anchor - ENGINE_STEP_KW)  # discharge ramps down to -20 MW
        world = self.world
        world.seq += 1
        proposal = ProposedBatch(
            command_batch_id=uuid4(),
            bank_id=BANK,
            cycle_id=f"cycle-{world.seq}",
            epoch=1,
            seq=world.seq,
            issued_at=self.now,
            expires_at=self.now + timedelta(seconds=LEASE_S),
            ledger_version=world.fakes.ledger.version,
            items=[
                *(
                    [ProposedItem(HUB, 0.0, "R-GRANT-AS-HOLD", self.hold_obligation, Decimal(0))]
                    if self.hold_item
                    else []
                ),
                ProposedItem(HUB, target, "R-GRANT-COMMITTED", world.toll, Decimal(str(round(-target, 3)))),
            ],
            is_firm_event=True,
        )
        world.fakes.proposals.add(proposal)
        world.fakes.leases.last[BANK] = (1, world.seq - 1)
        verdict = await world.service.evaluate_and_sign(make_batch_row(proposal))
        if verdict.outcome == "PASS":
            # the guardian never signs more than one cycle's bound from where the hub physically is
            assert abs(target - self.phys_kw) <= STEP_KW + EPS, (self.cycle, target, self.phys_kw)
            self.phys_kw = target
            self.signed_kw, self.lease_until = target, proposal.expires_at
            world.fakes.prior_grants.prior[world.toll] = Decimal(str(round(-target, 3)))
            self.signed_steps += 1
        else:
            self.vetoes += 1
            if self.engine_drops_on_veto:
                self.signed_kw = None  # NOT the contract: the guardian must still hold the one-cycle bound

    async def run(self, cycles: int) -> None:
        for _ in range(cycles):
            self.tick()
            if not self.stopped:
                await self.engine_cycle()

    async def idle(self, seconds: float) -> None:
        """No proposals for `seconds`: the next one is issued `seconds` after the last cycle's."""
        for _ in range(round(seconds / CYCLE_S) - 1):
            self.tick()

    async def converge(self, *, max_cycles: int = 200) -> None:
        for _ in range(max_cycles):
            if self.phys_kw <= -TOLL_KW + EPS:
                return
            self.tick()
            await self.engine_cycle()
        pytest.fail(f"did not converge: at {self.phys_kw} kW after {max_cycles} cycles, {self.vetoes} vetoes")


def _sim(fakes, signing_seed) -> Sim:
    return Sim(_world(fakes, signing_seed))


@pytest.mark.parametrize("hold_s", [20.0, 28.0])
async def test_a_hold_inside_the_lease_resumes_from_the_signed_setpoint(fakes, signing_seed, hold_s):
    sim = _sim(fakes, signing_seed)
    await sim.run(30)
    await sim.idle(hold_s)  # the engine proposes nothing; the signed lease (30 s) is still live
    await sim.converge()
    assert sim.vetoes == 0


async def test_after_a_20s_hold_a_ten_cycle_jump_is_vetoed_not_signed(fakes, signing_seed):
    """HIGH-B itself: r3.4.2 stretched dt to the 20 s since the signature and signed a +2.2 MW step."""
    sim = _sim(fakes, signing_seed)
    await sim.run(30)
    await sim.idle(20.0)
    anchor = sim.engine_anchor()
    sim.world.seq += 1
    proposal = ProposedBatch(
        command_batch_id=uuid4(),
        bank_id=BANK,
        cycle_id=f"cycle-{sim.world.seq}",
        epoch=1,
        seq=sim.world.seq,
        issued_at=sim.now,
        expires_at=sim.now + timedelta(seconds=LEASE_S),
        ledger_version=sim.world.fakes.ledger.version,
        items=[ProposedItem(HUB, anchor - 10 * STEP_KW, "R-GRANT-COMMITTED", sim.world.toll, None)],
        is_firm_event=True,
    )
    sim.world.fakes.proposals.add(proposal)
    sim.world.fakes.leases.last[BANK] = (1, sim.world.seq - 1)

    verdict = await sim.world.service.evaluate_and_sign(make_batch_row(proposal))

    assert verdict.outcome != "PASS" and "G-04" in verdict.vetoed_rule_ids
    assert "G-06" in verdict.vetoed_rule_ids  # the feeder ramp now counts the full step


async def test_a_40s_stop_then_release_converges_from_zero(fakes, signing_seed):
    sim = _sim(fakes, signing_seed)
    await sim.run(30)
    sim.stopped = True
    sim.world.fakes.safe_stop.stopped.add(("BANK", BANK))
    sim.world.fakes.safe_stop.engaged_at = {("BANK", BANK): sim.now}  # type: ignore[attr-defined]
    await sim.idle(40.0)
    sim.stopped = False
    sim.world.fakes.safe_stop.stopped.discard(("BANK", BANK))
    sim.signed_kw, sim.zero_until_telemetry_after = None, sim.now  # engine: 0 kW until newer telemetry
    sim.world.fakes.prior_grants.prior[sim.world.toll] = Decimal(0)  # the stop ended the delivery
    await sim.converge()
    assert sim.vetoes <= 1


async def test_a_release_inside_the_lease_does_not_anchor_on_the_pre_stop_setpoint(fakes, signing_seed):
    """DISPATCH's ask: a stop engaged after the signature drops the guardian's signed anchor, so the engine's
    steps from 0 kW after a 10 s stop are signed (and a step from the pre-stop setpoint is not)."""
    sim = _sim(fakes, signing_seed)
    await sim.run(30)
    sim.stopped = True
    sim.world.fakes.safe_stop.stopped.add(("BANK", BANK))
    sim.world.fakes.safe_stop.engaged_at = {("BANK", BANK): sim.now}  # type: ignore[attr-defined]
    await sim.idle(10.0)
    sim.stopped = False
    sim.world.fakes.safe_stop.stopped.discard(("BANK", BANK))
    sim.signed_kw, sim.zero_until_telemetry_after = None, sim.now
    sim.world.fakes.prior_grants.prior[sim.world.toll] = Decimal(0)
    await sim.converge()
    assert sim.vetoes <= 1


async def test_a_guardian_restart_mid_ramp_with_a_10s_gap_keeps_the_signed_anchor(fakes, signing_seed):
    sim = _sim(fakes, signing_seed)
    await sim.run(30)
    old: GuardianService = sim.world.service
    live = {h: e for h, e in old._last_signed.items() if e[2] > sim.now + timedelta(seconds=10.0)}
    await sim.idle(10.0)  # the guardian is down: nothing is signed
    fresh = GuardianService(
        ports=old.ports, config=old.config, signing_seed=signing_seed, now_fn=lambda: sim.world.clock[0]
    )
    fresh.seed_signed_anchors(live)  # what repo.load_signed_anchors reads back from og.verdict + the trace
    sim.world.service = fresh
    await sim.converge()
    assert sim.vetoes == 0


def test_seeding_never_replaces_a_newer_signature(fakes, signing_seed):
    sim = _sim(fakes, signing_seed)
    service = sim.world.service
    t = sim.now
    service._last_signed[HUB] = (-400.0, t, t + timedelta(seconds=30))
    service.seed_signed_anchors({HUB: (-200.0, t - timedelta(seconds=2), t + timedelta(seconds=28))})
    assert service._last_signed[HUB][0] == -400.0


# --- r3.4.3 HIGH: an item-level veto never drops a live signed anchor --------------------------------------------


@pytest.mark.parametrize("lag_cycles", [2, 3])
@pytest.mark.parametrize(
    "engine_drops", [False, True], ids=["contract-engine", "engine-reanchors-on-telemetry"]
)
async def test_an_item_level_veto_mid_ramp_keeps_the_signed_anchor(
    fakes, signing_seed, lag_cycles, engine_drops
):
    """A 20 MW ramp; one G-01 veto mid-ramp (the hub's SoC briefly not live); telemetry lagging 2-3 cycles. The
    hub keeps following its last signed setpoint on its live lease, so G-04 keeps measuring from there: no
    signed step ever exceeds one cycle's bound (the Sim asserts it on every PASS), even when the engine wrongly
    re-anchors on the stale telemetry (then the guardian vetoes until telemetry catches up), and it converges."""
    sim = _sim(fakes, signing_seed)
    sim.lag_cycles, sim.engine_drops_on_veto = lag_cycles, engine_drops
    await sim.run(30)
    hubs = sim.world.fakes.hubs.hubs
    hubs[HUB] = replace(hubs[HUB], health="stale")
    sim.tick()
    await sim.engine_cycle()
    assert sim.vetoes == 1  # G-01: no discharge on a SoC that is not live
    hubs[HUB] = replace(hubs[HUB], health="online")
    await sim.converge()
    assert sim.vetoes <= (1 if not engine_drops else 2 + lag_cycles)


async def test_a_veto_does_not_forget_the_guardians_signed_anchor(fakes, signing_seed):
    sim = _sim(fakes, signing_seed)
    await sim.run(10)
    signed = sim.world.service._last_signed[HUB]
    hubs = sim.world.fakes.hubs.hubs
    hubs[HUB] = replace(hubs[HUB], health="stale")
    sim.tick()
    await sim.engine_cycle()
    assert sim.vetoes == 1 and sim.world.service._last_signed[HUB] == signed


# --- MEDIUM: a single-hub utility-scale bank with a 0 kW hold item and the discharge item in one batch -------


async def test_a_hold_item_and_a_discharge_item_on_one_hub_are_checked_on_their_sum(fakes, signing_seed):
    """The anchor, G-04 and the rate checks use the hub's NET setpoint per batch (the sum of its items): a 0 kW
    hold item listed first never becomes the hub's anchor or its step."""
    sim = _sim(fakes, signing_seed)
    sim.hold_item = True
    await sim.converge()
    assert sim.vetoes == 0
    assert sim.world.service._last_signed[HUB][0] == pytest.approx(-TOLL_KW)
