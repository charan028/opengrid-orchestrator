# opengrid.allocator

Owner: allocator agent (BUILD.md S4). Implements 02a S5, the 2-second S1-S7 real-time allocation
cycle.

## Purpose

Every 2 s (10 s for idle banks, 02a S5.1), for every bank with an active obligation or a live
`PARTNER_CAPACITY`/`ERCOT_AS` event, the allocator re-derives how much of each already-committed
obligation's frozen `commitment.committed_kw` is physically deliverable right now, substitutes hubs
within an obligation on health loss, folds the `DIST_DEFERRAL` PI loop's output into that obligation's
grant, and schedules any leftover headroom to the price-responsive `ERCOT_ENERGY` market with a 5-min
dwell / $5-per-MWh hysteresis. It **never** selects new opportunities (that is the `selector`'s job)
and **never** reallocates a committed obligation's capacity to a different obligation (K13) -- the
only exceptions are the four K13 override reason codes, and substitution within the same obligation.

## Structure

- `models.py` -- plain dataclasses for the pure logic's inputs/outputs (hub/bank snapshots,
  obligation calls, SCADA samples, L2 instructions, proposed grants, shortfalls).
- `waterfill.py` -- S5.5 water-filling with stickiness, O(n log n), numpy-vectorized.
- `lexicographic.py` -- S3's T1->T2->T3->T4 lexicographic tier allocation.
- `substitution.py` -- S5.3 substitution within an obligation on hub health loss.
- `dist_deferral_pi.py` -- S5.4's `DistDeferralPI` (K9: the one integrating controller for bank kVA).
- `price_response.py` -- S6's dwell/hysteresis free-headroom price response.
- `cycle.py` -- `cycle(...)`, the pure S1-S7 orchestration function. No I/O.
- `gateways.py` -- `Protocol`s the thin adapter (`__init__.py`) uses to reach `opengrid.fleet`/
  `opengrid.ledger`/`opengrid.feeds`; tests inject fakes here instead of a database.
- `__init__.py` -- the fixed public interface (`run_cycle`, `substitute_hub`, per `INTERFACES.md`).

## Interface

```python
async def run_cycle(cycle_id: str, *, fleet=None, ledger=None, ...) -> list[Grant]
async def substitute_hub(obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str) -> None
def cycle(t, fleet_state, ledger_view, schedule, scada, instructions, **kwargs) -> CycleResult
```

`run_cycle`/`substitute_hub` keep the exact signatures fixed by `INTERFACES.md`; the keyword-only
gateway parameters are additive (default `None`, so existing callers are unaffected) and exist so
tests can inject fakes for `opengrid.fleet`/`opengrid.ledger` without a database, per BUILD.md S5.

## How to test

```
D:\Projects\OpenGrid\opengrid-orchestrator\.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\allocator -q
```

Property tests (Hypothesis) cover K1 (reserve never breached via hub capability), K4 (hub/bank
capability and PI ramp respected), K5 (L2 instructions obeyed), K9 (PI stability, no windup, single
integrator), and K13 (committed floor never reduced without an allowed reason; substitution keeps
delivery on hub loss; no flapping via dwell/hysteresis).
