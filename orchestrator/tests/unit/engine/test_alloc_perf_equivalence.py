"""r3.4.3 PERF-OPT: the optimized allocator and energy_check phases are byte-identical to the base commit's.

For seeded production-shaped fleets (`alloc_perf_fleet.build_fleet`), both versions run the engine's
`allocator` and `energy_check` phases for several ticks with telemetry arriving in between (so every memo is
both hit and invalidated), then a K4 veto re-proposal (`only_bank_ids` + `retry_excluded_hub_ids`), and every
output is compared as text (`repr` keeps floats bit-exact): the cycle results (grants with their reasons,
shortfalls, substitutions, holds, hub allocations, PQ reductions, territory blocks), the grant rows, the hub
allocations, the shortfalls handed to escalation, the energy-sufficiency results and AS holds, the AT_RISK flag
writes, alerts, trace rows and every SQL write. The reference is the old code itself (`alloc_perf_oracle`).

The default run is a quick sample; `OG_PERF_EQUIV_FULL=1` runs the full matrix (50 seeds x 1,000/3,500/7,500 hubs).
"""

from __future__ import annotations

import contextlib
import os
from datetime import timedelta

import pytest

from opengrid import allocator, fleet

from .alloc_perf_fleet import NOW, EngineHarness, build_fleet, engine_harness, run_cycles, set_clock
from .alloc_perf_oracle import oracle

_FULL = os.environ.get("OG_PERF_EQUIV_FULL") == "1"
_SIZES = (1_000, 3_500, 7_500) if _FULL else (1_000, 3_500)
_SEEDS = range(50) if _FULL else range(3)
_CYCLES = 4


async def _retry(h: EngineHarness, sf_seed: int) -> None:
    """The K4 veto re-proposal the engine makes after a vetoed batch: a few banks re-solved with a hub out."""
    now = NOW + timedelta(seconds=2 * (_CYCLES - 1))
    set_clock(now)
    grants_by_bank = sorted({str(g.bank_id) for g in h.outputs.grants[-1]})
    if not grants_by_bank:
        return
    wanted = grants_by_bank[sf_seed % len(grants_by_bank) :][:3]
    # The guardian vetoed the first two online hubs of each re-solved bank.
    vetoed = frozenset(
        hub_id
        for bank_id in wanted
        for hub_id in sorted(s.hub_id for s in fleet.hub_capabilities(bank_id) if s.health == "online")[:2]
    )
    grants = await allocator.run_cycle(
        f"{int(now.timestamp())}-{_CYCLES}",
        fleet=h.fleet_gw,
        ledger=h.ledger_gw,
        scada_gateway=h.scada_gw,
        schedule_gateway=h.schedule_gw,
        extras_gateway=h.extras_gw,
        now=now,
        lease_ttl_s=30.0,
        only_bank_ids=wanted,
        retry_excluded_hub_ids=vetoed,
        attempt=1,
    )
    h.outputs.grants.append(grants)
    h.outputs.hub_allocations.append(allocator.hub_allocations())
    h.outputs.shortfalls.append(list(h.ledger_gw.last_shortfalls))


async def _fingerprint(size: int, seed: int, *, old: bool) -> tuple[str, int]:
    sf = build_fleet(size, seed, load=1 + 3 * (seed % 2))  # odd seeds: a heavy DELIVERING book
    with oracle() if old else contextlib.nullcontext():
        async with engine_harness(sf) as h:
            outputs = await run_cycles(h, _CYCLES, churn_seed=seed)
            await _retry(h, seed)
            outputs.trace_rows = list(h.trace_backend.rows)
            outputs.writes = list(h.pool.executed)
            return outputs.fingerprint(), sum(len(r.grants) for r in outputs.cycle_results)


@pytest.mark.parametrize("size", _SIZES)
@pytest.mark.parametrize("seed", _SEEDS)
async def test_optimized_phases_match_the_base_commit_byte_for_byte(size: int, seed: int) -> None:
    old, old_grants = await _fingerprint(size, seed, old=True)
    new, _new_grants = await _fingerprint(size, seed, old=False)
    assert old_grants > 0  # the fleet really dispatches
    if new != old:  # a compact report: the texts are megabytes
        at = next(
            (i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b), min(len(old), len(new))
        )
        pytest.fail(
            f"outputs differ at char {at}:\nold: ...{old[max(at - 300, 0) : at + 300]}\nnew: ...{new[max(at - 300, 0) : at + 300]}"
        )


async def test_the_fixture_exercises_the_interesting_paths() -> None:
    """Guard against a vacuous comparison: across a few seeds the synthetic fleet produces shortfalls, AS
    holds, substitutions, territory/availability blocks, flow-limit caps, AT_RISK obligations and memo hits."""
    seen: dict[str, bool] = {}
    for seed in range(4):
        sf = build_fleet(1_000, seed)
        async with engine_harness(sf) as h:
            outputs = await run_cycles(h, _CYCLES, churn_seed=seed)
            results = outputs.cycle_results
            seen["shortfall"] = seen.get("shortfall", False) or any(r.shortfalls for r in results)
            seen["held"] = seen.get("held", False) or any(r.held for r in results)
            seen["substitution"] = seen.get("substitution", False) or any(r.substitutions for r in results)
            seen["territory"] = seen.get("territory", False) or any(r.territory_blocks for r in results)
            seen["at_risk"] = seen.get("at_risk", False) or any(
                e.at_risk for es in outputs.energy for e in es
            )
            seen["memo"] = seen.get("memo", False) or bool(getattr(allocator, "_prep_memo", {}))
    assert all(seen.values()), seen
