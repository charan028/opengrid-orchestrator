# opengrid.fleet

The digital twin (02b S4): latest state per hub in memory, periodic bulk `COPY` into `og.telemetry`
and upsert of `og.hub_state`, eligibility/quality classification (online/stale/offline/fault),
bank aggregation via `opengrid.core.physics`, SCADA bank-signal and utility-instruction storage.
`fleet` never simulates physics forward -- it only stores and aggregates reported state (02b S12).

I/O is isolated behind the `FleetBackend` protocol (`opengrid.fleet.pg_backend.PgFleetBackend` is the
real Postgres implementation), so the twin's classification/aggregation logic is unit-testable without
a database.

## Interface

Fixed by `orchestrator/INTERFACES.md`:

- `capability(bank_id, interval_start) -> AvailableCapability` -- the only entry point `selector`/
  `allocator` call for a bank's current dischargeable/chargeable power, with stale/offline/fault hubs
  excluded and any active L2 utility instruction (K5) folded in as a hard ceiling.
- `ingest_telemetry(payload)` -- upserts the in-memory twin and buffers a row for the next `flush()`.

Engine-internal extensions (not a wire/API contract): `configure(backend, cfg)`, `load_topology()`
(restart recovery), `flush(now=...)` (periodic maintenance tick), `ingest_scada_signal(payload)`,
`ingest_utility_instruction(payload)`, `bank_scada_signal(bank_id)`, `utility_instruction(bank_id)`,
`hub_health(hub_id)`.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\fleet -q
```

Server-side integration (Mosquitto `ogtest/eng`, Postgres `og_t_eng`, synthetic telemetry) is under
`orchestrator/tests/integration/`.
