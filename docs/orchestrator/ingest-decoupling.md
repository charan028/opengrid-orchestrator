# Telemetry ingest decoupling

Owner: DISPATCH. Status: step 1 is implemented behind `[ingest].telemetry_decoupled`, default OFF (deferred to r3.4.5: it is enabled after the workstation r3.4.3 perf before/after and a dev-stack test). Step 2 is a design only, not scheduled.

## Why

og-engine used to run everything that touches telemetry on its single asyncio loop:

- the MQTT receive;
- JSON decode;
- jsonschema validation;
- pydantic parse;
- the write into the twin.

The 2 s dispatch tick runs on that same loop. At fleet scale (7,500 hubs, 2–10 s telemetry each), decode and validation were the largest share of loop time outside the tick. A telemetry burst delayed the tick, and a slow tick delayed telemetry. That is the loop-lag coupling behind r3.4.1's 4–5 s event-loop stalls.

## Step 1: parser thread and a latest-value buffer (`engine/telemetry_ingest.py`, default OFF)

```
MQTT loop ──submit(raw bytes)──► bounded raw deque ──► parser thread
                                  (raw_max, drops oldest)   json + schema + pydantic
                                                              │
                                                              ▼
                                             latest-value-per-hub buffer
                                             (newest ts wins, ≤ max_hubs)
                                                              │  swap every ~100 ms
                                                              ▼
                                   applier task (on the loop) ──► fleet.apply_telemetry
```

- **Loop side.** For `<root>/tel/#` the MQTT loop only appends the raw bytes and wakes the parser. Every other topic keeps its inline or background-worker path. No other topic sits under `tel/`.
- **Parser thread.** One daemon thread (`og-telemetry-parse`). It never touches the twin. It calls `fleet.parse_telemetry`, which validates against the schema and parses, and is thread-safe.
- **Newest-wins.**
  - A sample older than the one buffered for its hub is dropped (`stale`), as is one older than the sample last applied.
  - A newer sample replaces an unapplied one (`superseded`).
  - The twin therefore never rolls back to an older sample.
- **Bounded memory.**
  - The raw deque is capped at `raw_max` (default 20,000). When it is full, the oldest raw message is dropped (`raw_dropped`), because a newer one is already behind it.
  - The parsed buffer holds at most one entry per hub, capped at `max_hubs` (default 100,000).
- **Applier.** Every `apply_interval_s` (default 0.1 s) it swaps the buffer out and applies each hub's sample through `fleet.apply_telemetry`. It feeds the ingest-lag metric and yields every 500 samples.
- **CPU.** Total work is unchanged: the same decode, validation and parse, now on another thread. Coalescing only removes duplicates within 100 ms. The GIL still serialises Python bytecode, but the loop no longer waits behind a burst.
- **Latency.** At most one apply interval (100 ms) is added to a sample. Telemetry is 2–10 s, and G-04 anchors, K13 and eligibility all work at second scale.
- **Switch.** `[ingest].telemetry_decoupled` defaults to false, which keeps the inline path; true enables the decoupled path. The tunables are `[ingest].telemetry_raw_max`, `telemetry_max_hubs` and `telemetry_apply_interval_s`.
- **Equivalence.** `tests/unit/fleet/test_telemetry_paths_equivalence.py` runs one in-order stream through both paths. The twin runtime state, the hub capability snapshots and the persisted telemetry rows are identical. For a burst of samples inside one apply interval, the twin is still identical; only the superseded intermediate rows are not persisted.
- **Tests.** `tests/unit/engine/test_telemetry_ingest.py` covers:
  - newest-wins regardless of arrival order;
  - no rollback after apply;
  - the backlog stays bounded at 50k messages with a stalled consumer;
  - the oldest raw message is the one dropped;
  - parsing runs on the thread;
  - invalid payloads are counted;
  - an apply failure is isolated;
  - the applier cadence;
  - the switch.

## Step 2: a separate og-ingest process (design only)

**Goal.** Take MQTT receive, decode and validation out of the og-engine process entirely, so that engine loop lag and ingest can never block each other, and ingest can scale on its own core.

**Shape.**
- A new `og-ingest` systemd unit (the same venv) subscribes to `tel/#` and does step 1's parse and newest-wins coalescing.
- Every ~100 ms it publishes the per-hub latest snapshot to og-engine over a local channel.
- og-engine's applier reads that channel instead of the in-process buffer. `fleet.apply_telemetry` is unchanged.

**Channel options, in order of preference.**
1. **A Unix domain socket carrying msgpack frames.** Each frame is a batch of `(hub_id, ts, fields)`, sequence-numbered.
   - og-engine reconnects on loss.
   - On a gap it asks og-ingest for a full snapshot. This is cheap: at most one row per hub.
   - No broker hop and no disk.
2. **A shared-memory ring of fixed-size records per hub** (hub-index to slot, with a seqlock).
   - Lowest latency.
   - More code, and needs careful versioning of the record layout.
3. **Republishing coalesced telemetry on an internal MQTT topic.**
   - The simplest to build.
   - Adds a broker hop and keeps JSON decode in og-engine, so it only helps with validation. Rejected.

**Contracts to keep.**
- Newest-wins by `ts` per hub. Never apply an older sample.
- og-engine must see an ingest outage. Today `IngestHealth` and the heartbeat stop after `mqtt.ingest_give_up_s`; with step 2, a snapshot channel silent for more than N seconds counts as ingest down, so ALR-PROCESS-DOWN still fires and the twin still ages to stale.
- Telemetry persistence (the COPY into `og.telemetry`, via `fleet._pending_telemetry`) stays with og-engine's applier. Rows then cover the applied samples only, as in step 1. If every raw sample must be stored, og-ingest takes over the COPY instead.
- Deploy order: og-ingest first, then an og-engine that reads the channel. Behind `[ingest].mode = "inline" | "thread" | "process"`.

**Open questions.**
- Whether `ack/+` (hub command acks, today inline) should move with `tel/` as well. They are rarer, but they drive lease tracking.
- Resource budget on base: one more Python process, about 150 MB RSS.
- Whether K13 / DELIVERY-VERIFY ever need raw-sample-level telemetry rather than the coalesced stream. Today they read `og.telemetry` at 2 s granularity, which coalescing at 100 ms does not change.
