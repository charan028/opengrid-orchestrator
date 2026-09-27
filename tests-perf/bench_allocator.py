"""In-process CPU benchmark of the engine's `allocator` and `energy_check` cycle phases (no DB, no MQTT).

Builds the synthetic production-shaped fleet of `orchestrator/tests/unit/engine/alloc_perf_fleet.py` (50 hubs
per bank, 20 % dual-unit, ERCOT/AEN/LCRA/RAYBN zones, one substation toll, ~50 % of banks with obligations)
and times the two phases exactly as `opengrid.engine._engine_tick` runs them (same phase boundaries as the
`CYCLE_LATENCY` phase timer: `allocator.run_cycle` + `allocator.hub_allocations()`, and
`EnergySufficiencyGateway.run`), over the real engine gateways with a fake pool. DB time is excluded by
construction: what is measured is the Python CPU the event loop spends in those phases.

    python tests-perf/bench_allocator.py --sizes 1000 3500 7500 --cycles 30
    python tests-perf/bench_allocator.py --sizes 7500 --profile allocator    # cProfile top 30

Run it niced on the base server (the disk and CPUs are shared with production).
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import gc
import io
import os
import pstats
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

_ORCH = Path(__file__).resolve().parents[1] / "orchestrator"
# OG_BENCH_SRC: benchmark another source tree (e.g. a checkout of the base commit) with this harness.
for _p in (Path(os.environ.get("OG_BENCH_SRC") or _ORCH / "src"), _ORCH / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from unit.engine.alloc_perf_fleet import (  # noqa: E402
    NOW,
    allocator_phase,
    build_fleet,
    energy_check_phase,
    engine_harness,
    ingest_churn,
    set_clock,
)

gc_pauses: list[float] = []
#: Ticks between two reports of one hub (10 s telemetry / 2 s cycle = 5; 1 = every hub reports every tick).
CHURN_PERIOD = [5]
_gc_t0 = [0.0]


def _gc_probe(phase: str, info: dict[str, int]) -> None:
    if info.get("generation") != 2:
        return
    if phase == "start":
        _gc_t0[0] = time.perf_counter()
    else:
        gc_pauses.append((time.perf_counter() - _gc_t0[0]) * 1000.0)


gc.callbacks.append(_gc_probe)


def _pct(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


async def _bench(
    size: int, seed: int, cycles: int, profile: str | None, churn: bool, load: int
) -> dict[str, object]:
    sf = build_fleet(size, seed, load=load)
    alloc_ms: list[float] = []
    energy_ms: list[float] = []
    prof = cProfile.Profile() if profile else None
    async with engine_harness(sf) as h:
        if os.environ.get("OG_BENCH_GC_FREEZE") == "1":
            gc.collect()
            gc.freeze()  # experiment: the long-lived twin/topology out of the cyclic GC's full passes
        gc_pauses.clear()
        for n in range(cycles + 2):  # two warm-up ticks (first-run alert sweep, AT_RISK entries)
            now = NOW + timedelta(seconds=2 * n)
            set_clock(now, ticking=True)  # every read its own instant, as in production
            if churn and n > 0:
                ingest_churn(seed, n, period=CHURN_PERIOD[0])
            if prof is not None and profile == "allocator" and n >= 2:
                prof.enable()
            t0 = time.perf_counter()
            await allocator_phase(h, now, f"bench-{n}")
            t1 = time.perf_counter()
            if prof is not None:
                prof.disable()
            if prof is not None and profile == "energy_check" and n >= 2:
                prof.enable()
            t2 = time.perf_counter()
            await energy_check_phase(h, now)
            t3 = time.perf_counter()
            if prof is not None:
                prof.disable()
            if n >= 2:
                alloc_ms.append((t1 - t0) * 1000.0)
                energy_ms.append((t3 - t2) * 1000.0)
            h.outputs.grants.clear()
            h.outputs.energy.clear()
            h.outputs.cycle_results.clear()
            h.outputs.hub_allocations.clear()
            h.pool.executed.clear()
    if prof is not None:
        out = io.StringIO()
        pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(30)
        pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(20)
        print(out.getvalue())
    combined = [a + e for a, e in zip(alloc_ms, energy_ms, strict=True)]
    return {
        "hubs": sf.n_hubs,
        "banks": len(sf.banks),
        "obligations": sf.obligations,
        "call_rows": sf.call_rows,
        "alloc_p50": statistics.median(alloc_ms),
        "alloc_p99": _pct(alloc_ms, 0.99),
        "energy_p50": statistics.median(energy_ms),
        "energy_p99": _pct(energy_ms, 0.99),
        "combined_p99": _pct(combined, 0.99),
        "gc2": f"{len(gc_pauses)} x max {max(gc_pauses, default=0.0):.0f} ms",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 3500, 7500])
    parser.add_argument("--cycles", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--load", type=int, default=1, help="obligation book multiplier (4 = heavy DELIVERING)"
    )
    parser.add_argument("--no-churn", dest="churn", action="store_false", help="no telemetry between ticks")
    parser.add_argument("--churn-period", type=int, default=5, help="ticks between a hub's reports (1 = all)")
    parser.add_argument("--profile", choices=("allocator", "energy_check"), default=None)
    args = parser.parse_args()
    CHURN_PERIOD[0] = args.churn_period
    print(
        f"{'hubs':>6} {'banks':>5} {'oblig':>5} {'rows':>5} | {'alloc p50':>9} {'alloc p99':>9} | "
        f"{'energy p50':>10} {'energy p99':>10} | {'comb p99':>8}  (ms)"
    )
    for size in args.sizes:
        r = asyncio.run(_bench(size, args.seed, args.cycles, args.profile, args.churn, args.load))
        print(
            f"{r['hubs']:>6} {r['banks']:>5} {r['obligations']:>5} {r['call_rows']:>5} | "
            f"{r['alloc_p50']:>9.1f} {r['alloc_p99']:>9.1f} | {r['energy_p50']:>10.1f} {r['energy_p99']:>10.1f} | "
            f"{r['combined_p99']:>8.1f}  gen2 GC: {r['gc2']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
