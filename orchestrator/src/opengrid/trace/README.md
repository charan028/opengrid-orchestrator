# opengrid.trace

The only module that writes `og.trace` rows (02a S8, 02b S1.6/S12). Built on
`opengrid.core.tracehash`; I/O is isolated behind the `TraceBackend` protocol in `store.py` so the
hash-chain logic is unit-testable without a database (a real Postgres-backed implementation is added by
whichever agent needs it, in a separate `pg_backend.py`, keeping this module import-free of `psycopg`).

## Interface

- `TraceStore.append(stream_id, decision_type, event_class, payload, reason_codes=None)` -- K10: durably
  write the decision pre-image before anything is signed.
- `TraceStore.exists_preimage(decision_ref)` -- what guardian's G-14 check calls instead of recomputing
  a hash itself.
- `TraceStore.verify(stream_id, from_seq=0)` -- K11: re-derive and confirm the chain.
- `TraceStore.checkpoint()` / `TraceStore.prune()` -- 02a S8.3 checkpointed pruning, run on `og-settle`'s
  cadence via `opengrid.settle.run_trace_pruning_cycle()`.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\trace -q
```
