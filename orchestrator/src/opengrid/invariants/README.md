# opengrid.invariants

Independent, read-only measurement of `docs/orchestrator/07-delivery/00-invariants.md`'s K1 (reserve
breach), K2 (double-sold kWh), K13 (commitment-lock violation), orphan reservation/commitment
bookkeeping, and K11 (scheduled trace-chain verification).

## Purpose

`og_reserve_breaches_total` / `og_double_sold_kwh_total` (`opengrid.platform.metrics`) were declared but
never incremented, and the API hard-coded its three invariant counters to 0. This package re-derives
each invariant from what actually landed in the database, on its own schedule, independently of the
write paths (`opengrid.core.limits`, `opengrid.ledger`) that already try to prevent the violation — so a
bug in, or a write that bypassed, the preventive path is still caught.

## Interface

- `configure(pool, cfg)` — called once, from `opengrid.health.configure()` (this package has no process
  entry point of its own; it piggybacks on `og-settle`'s existing health cadence).
- `run_due()` — the hook called every `og-settle` health cycle (`opengrid.health.evaluate_once()`). Gates
  its own K1/K2/K13/orphan checks and its K11 trace-verify check on independent `Cadence`s
  (`[invariants].interval_s`, default 60s; `[invariants].trace_verify_interval_s`, default 300s).
- `run_once()` / `run_trace_verify_once()` — the individual check passes, callable directly (tests,
  manual runs).
- `read_summary(pool)` — the API's read path (`api/routers/health.py`, `api/routers/dispatch.py`): a
  plain read of `og.invariant_check`'s measured totals, never a constant.

## How to test

```powershell
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\invariants -q
```

Integration (real Postgres, seeded fleet): `tools/remote.ps1 -Ws inv -Cmd "<pytest invocation>"`.

## Config

`[invariants]` in `orchestrator.toml`: `interval_s` (default 60), `trace_verify_interval_s` (default
300), `k2_lookback_s` (default 3600, K2's rolling aggregation window), `k13_max_grant_gap_s` (default 30,
K13's implicit-zero gap threshold -- see `checks.find_dip`).

## K13 notes (2026-09-26 fixes)

Three defects found from the first live numbers, all in this package (never in the engine/allocator/
ledger, which enforce K13 correctly):

1. **Reason lookup used `og.trace.scope`, which nothing writes.** The engine/allocator trace a K13
   exception's `obligation_id` in the trace row's `payload` (`opengrid.engine.gateways.
   EngineLedgerGateway.record_shortfalls`/`record_substitution_events`); `scope` is never populated by
   any writer. Fixed in `queries.fetch_earliest_covering_trace_at` (`payload ->> 'obligation_id'`).
2. **Per-bank grant compared to the whole obligation's commitment.** K13 is defined on the obligation's
   TOTAL delivery, not one bank's share of it. Fixed by `queries.fetch_grant_cycle_series` summing every
   bank's grant across each allocator cycle before comparing.
3. **A silent gap in grant activity was invisible.** A stretch with zero grant rows at all (not just a
   low one) is the most severe possible dip; `checks.find_dip` now treats a gap longer than
   `k13_max_grant_gap_s` as an implicit zero-kW sample.

The "rest-of-window zero after a mid-window SHORTFALL is allowed" policy lives in exactly one place,
`checks._dip_is_covered`, so it can change in one edit if the owner's coverage rule changes.
