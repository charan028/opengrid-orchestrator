# tests-e2e/pq: cross-system power-quality tests (TS-15b)

Owner: qa (`tests-e2e/`). The only suite that imports both `ogsim` and `opengrid`; neither product may import the
other (BUILD.md §1, `orchestrator/tools/dupcheck.py` checks their `src/` trees only). No broker, no database, no
processes: everything runs in process.

| Suite | Scenarios |
|---|---|
| `test_ts15b_pq_cross_system.py` | TS-15b (`06-service-profiles-and-power-quality.md` §8.4): ogsim's published waveform summaries, through the wire contract and `opengrid.pq_ingest`, into `bank_measurement`, compared with the ground truth from ogsim's seeded inverter state (§3.2, §6.5 step 3, §7.4) |

Every scenario runs on single-unit homes (`dual_unit_share=0`) and on Base's confirmed mix (`dual_unit_share=0.2`);
two-unit homes are checked for per-leg phasor-summed current and a vector-summed hub harmonic block (WP-K).

Data path: `FleetEngine.wave_summary_messages()` -> JSON round trip -> `pq_waveform_summary` schema check (both
sides) -> `pq_ingest.ingest_summary` / `flush_summaries` (in-memory backend) -> `bank_measurement`.

## Run

From the repository root, with a Python that has both packages' dependencies:

```bash
python -m pytest tests-e2e/pq -q
```

`conftest.py` puts this checkout's `orchestrator/src` and `integration-sims/src` first on `sys.path`, so no
`PYTHONPATH` is needed.

## Known failure (xfail, strict)

- **`bank_measurement()` reports `thd_current_pct` as the mean of per-hub THD_I**, not the §3.2(b) vector sum that
  §6.5 step 3 and TS-15b require. On a diverse (cancellation-regime) bank the mean overstates the bank THD_I by
  about an order of magnitude. `test_ts_15b_bank_measurement_thd_current_is_the_vector_sum` records this; it will
  XPASS (and fail, being strict) once the aggregation calls `opengrid.core.pq.bank_thd_current_pct`, and the marker
  should then be removed.
