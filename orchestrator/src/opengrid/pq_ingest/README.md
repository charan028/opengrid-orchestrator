# opengrid.pq_ingest (WP-G)

Waveform ingest for `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md`
§6.4-§6.5. Subscribes (via the caller's MQTT loop), validates, persists and analyzes the
two payload kinds a hub inverter publishes over SCADA: the periodic `PqWaveformSummary`
and the triggered-only `PqWaveformRawCaptureHeader`.

## Purpose

- `ingest_summary(payload)` / `ingest_raw_capture(payload)` -- validate (defense in depth
  over the caller's `opengrid.platform.mqtt.validate_payload` schema check) and persist.
  Raw captures go to a `BlobStore` (object storage) plus an index row in
  `og.pq_waveform_raw_index`; summaries go to `og.pq_waveform_summary`. Both tables
  already exist (`orchestrator/migrations/0011_asset_health.sql`, WP-A) -- this package
  adds no new migration.
- **S9 wave-2 fix**: `ingest_summary` no longer inserts per message. It buffers the
  validated row (capped, drop-oldest, `opengrid.pq_ingest.metrics`) and `flush_summaries()`
  drains the buffer as one batched `executemany` (`PqIngestBackend.insert_summaries_batch`,
  async commit -- mirrors `opengrid.fleet.pg_backend`'s own fix for the same "insert-per-
  message stalls the WAL" failure mode, live 2026-09-26). See "Wiring" below for the two
  call sites `og-engine` must add.
- `capture.build_capture_request` / `track_capture_request` / `expire_stale_capture_requests`
  -- the on-demand capture flow (§6.4b/§6.6): build a `WaveformCaptureRequest`, publish it,
  track it until the matching raw capture arrives (or it expires unanswered).
- `aggregation.bank_measurement` -- §6.5 step 3's measured per-bank aggregation, via
  `opengrid.core.pq`'s pure math (never re-implemented here). Feeds the allocator's
  self-check, the guardian's G-21..G-23, and the §5.4 monitoring loop.
- `audit.audit_current_channel` -- §6.5 step 2's background audit job: independently
  recomputes THD/RMS/frequency from a raw capture's samples (via
  `opengrid.core.pq.analyze_waveform`) and flags a hub whose edge computation disagrees
  with its own concurrent summary (`PQ_EDGE_MISCALIBRATION_SUSPECTED`).
- `raw_codec.encode_raw_samples` / `decode_raw_samples` -- packs/unpacks a capture's
  per-channel `int16` sample arrays into the compact binary blob §6.4a specifies for
  object storage (distinct from the JSON `samples` field the MQTT payload/test-fixture
  representation carries; see `interfaces/mqtt/pq_waveform_raw.schema.json`'s own note).

## Interface

Module-level singleton facade (mirrors `opengrid.fleet`/`opengrid.ledger`): call
`opengrid.pq_ingest.configure(backend, blob_store)` once at process start-up, then call
the free functions. `PqIngestBackend` is a `Protocol`; `pg_backend.PgPqIngestBackend` is
the real Postgres implementation, `blob_store.FileBlobStore` the real (local-disk)
`BlobStore`.

This package holds no MQTT client of its own and never imports `ogsim` (BUILD.md §1).
See the top-level build report for the exact wiring lines into `og-engine`'s existing
MQTT ingest loop (`opengrid.engine._mqtt_ingest_loop`, engine-owned).

## Wiring (for the engine owner -- not applied by this package)

1. At start-up (where `opengrid.fleet.configure`/`opengrid.ledger.configure` are already
   called), call:
   ```python
   opengrid.pq_ingest.configure(
       PgPqIngestBackend(pool),
       FileBlobStore(cfg.get("pq_ingest.blob_store_dir", "/var/lib/opengrid/pq_waveform")),
       summary_buffer_max=cfg.get("pq_ingest.summary_buffer_max", DEFAULT_SUMMARY_BUFFER_MAX),
       flush_batch_size=cfg.get("pq_ingest.flush_batch_size", DEFAULT_FLUSH_BATCH_SIZE),
   )
   ```
2. On the MQTT ingest path (`.../wave/+/+/+/summary` -> `ingest_summary`, `.../raw` ->
   `ingest_raw_capture`), no change needed beyond what already calls these two functions --
   `ingest_summary` now buffers instead of inserting, transparently to the caller.
3. Add a periodic timer (own tick, same shape as `opengrid.fleet.flush`'s
   `[fleet].telemetry_interval_s` timer) calling `await opengrid.pq_ingest.flush_summaries()`
   every `[pq_ingest].flush_interval_s` (default `DEFAULT_FLUSH_INTERVAL_S`, 2.0s). Wrap the
   call in the same per-tick try/except every other tick loop in this codebase already uses
   (`opengrid.fleet.flush`'s caller, `ogsim.fleet.runtime.run_fleet`) so one failed flush is
   logged and retried next tick, never crashes the process or silently stops buffering.
4. Add `[pq_ingest]` to `orchestrator/config/orchestrator.toml` (architect-owned; ask them to
   add the section, or add it yourself if `orchestrator/config/` is in your own owned paths):
   ```toml
   [pq_ingest]
   summary_buffer_max = 20000
   flush_batch_size = 500
   flush_interval_s = 2.0
   ```

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\pq_ingest -q
```

All tests here run against fakes (no DB, no MQTT, no filesystem beyond a pytest `tmp_path`
for `FileBlobStore`), per `BUILD.md` §5's local/unit split. Server-side integration
(real Postgres row round-trip) is exercised via `tools/remote.ps1 -Ws wave`.
