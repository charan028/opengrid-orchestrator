# opengrid.engine

`og-engine` process wiring (02b S1.2): composes `fleet` (read side), `contracts`, `selector`, `ledger`,
`allocator` and `trace` through their public interfaces only. No independent business logic beyond
scheduling and the engine -> guardian handoff.

## What `main(cfg)` drives

A single 2 s ticking loop (`[allocator].cycle_interval_s`) that, each tick:

1. writes this process's heartbeat (`og.heartbeat`);
2. flushes the fleet twin (`fleet.flush`) -- bulk `COPY`/upsert plus health reclassification;
3. checks the 15-min wall-clock gate, plus any due `ADMISSION`/`RENOMINATION` triggers, and calls
   `selector.run_gate` for each;
4. calls `allocator.run_cycle` for this cycle's grants;
5. if the guardian's heartbeat is current, builds and persists one `og.command_batch` summary row per
   bank with a non-empty grant set and issues `NOTIFY og_command_batch` (the engine -> guardian handoff;
   see the module docstring for why this is Postgres-as-queue and not a direct function call: K3/K8
   process separation). If the guardian is not heartbeating, the cycle holds -- no new batches are
   proposed (02b S6.5 degraded mode).

A second task subscribes to `<root>/tel/#`, `<root>/scada/#`, `<root>/scada/instruction/#`, validates
each payload against `interfaces/mqtt/*.schema.json`, and routes it into `fleet`.

Only one `run_forever` loop is used (not one per cadence) because
`opengrid.platform.process.run_forever` installs a SIGTERM/SIGINT handler per call; running several
concurrently would leave only the last-registered one able to see the signal.

I/O for scheduling triggers and the command-batch queue is isolated behind the `EngineBackend`
protocol (`opengrid.engine.pg_backend.PgEngineBackend` is the real Postgres implementation) so the
scheduling logic (`GateScheduler`, `build_command_batch_row`, `guardian_is_available`) is unit-testable
with fakes.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\engine -q
```
