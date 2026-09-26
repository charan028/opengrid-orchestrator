# tests-e2e/pq: cross-system power-quality tests (TS-15b)

Owner: qa (`tests-e2e/`). The only suite that imports both `ogsim` and `opengrid`; neither product may import the
other (BUILD.md §1, `orchestrator/tools/dupcheck.py` checks their `src/` trees only). No broker, no database, no
processes: everything runs in process.

| Suite | Scenarios |
|---|---|
| `test_ts15b_pq_cross_system.py` | TS-15b (`06-service-profiles-and-power-quality.md` §8.4): ogsim's published waveform summaries, through the wire contract and `opengrid.pq_ingest`, into `bank_measurement`, compared with the ground truth from ogsim's seeded inverter state (§3.2, §6.5 step 3, §7.4) |

Data path: `FleetEngine.wave_summary_messages()` -> JSON round trip -> `pq_waveform_summary` schema check (both
sides) -> `pq_ingest.ingest_summary` / `flush_summaries` (in-memory backend) -> `bank_measurement`.

## Run

From the repository root, with a Python that has both packages' dependencies:

```bash
python -m pytest tests-e2e/pq -q
```

`conftest.py` puts this checkout's `orchestrator/src` and `integration-sims/src` first on `sys.path`, so no
`PYTHONPATH` is needed.
