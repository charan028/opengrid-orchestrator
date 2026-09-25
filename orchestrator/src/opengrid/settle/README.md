# `opengrid.settle`

Owner: settle agent (`BUILD.md` S4). Implements 02a S7 (M&V, billing, profitability) and runs the
`og-settle` process's trace-pruning cadence (02a S8.3).

## Purpose

Sole owner of M&V and profitability math (02b S12) -- the UI only ever reads `og.meter_interval`,
`og.performance`, `og.invoice_line` and `og.pnl`; it never computes any of these numbers itself.

For one obligation-interval, `settle()`:

1. meters delivered kWh from telemetry (`metering.py`), grading quality `GOOD`/`ESTIMATED` by the
   >=13-of-15-samples rule (02a S7.2);
2. computes the M&V baseline (`baselines.py`) and compliance % against it (`performance.py`);
3. computes revenue/energy-cost/degradation/penalty/net-value, the LP-vs-rule-baseline comparison, and
   the commitment lock's forgone upside (`profitability.py`, 02a S7.4);
4. posts insert-only, versioned `invoice_line` rows (`billing.py`, 02a S7.3);
5. writes one `SETTLEMENT` trace record per obligation-interval that actually changed something.

Every step above is idempotent: re-running `settle()` for an unchanged obligation-interval inserts
nothing; re-running it after telemetry changed inserts new, insert-only versioned rows referencing the
originals (never an `UPDATE`/`DELETE` of financial data).

## Interface

Fixed by `orchestrator/INTERFACES.md`:

```python
async def settle(obligation_id: UUID, interval_start: datetime, interval_end: datetime) -> None: ...
async def run_trace_pruning_cycle() -> dict[str, int]: ...
```

Both take no dependency-injection parameters; call `opengrid.settle.configure(backend, trace_store)`
once (the process entry point `opengrid.settle.main` does this at startup; tests do it with fakes)
before calling either.

`run_settle_cycle(*, max_concurrency=20)` is the batch entry point `og-settle`'s tick calls: it fetches
every pending obligation-interval from the backend and settles them concurrently (BUILD.md S2: many
customers/obligations at once), logging and skipping any single failure rather than stopping the batch
(K7).

## Package layout

| Module | Owns |
|---|---|
| `metering.py` | Telemetry -> delivered kWh, pure |
| `baselines.py` | Per-service M&V baseline / meter source, pure |
| `performance.py` | Compliance % and threshold pass/fail, pure |
| `profitability.py` | Revenue/cost/penalty/net, rule-baseline comparison, forgone upside, pure |
| `billing.py` | Which invoice lines to post, insert-only versioning decision, pure |
| `csv_export.py` | CSV rendering of invoice-line/meter-interval export rows (ES08-S05) |
| `backend.py` | `SettleBackend` Protocol -- the only I/O contract the pure modules above never see |
| `pg_backend.py` | Postgres implementation of `SettleBackend` |
| `trace_pg_backend.py` | A settle-owned Postgres `TraceBackend` (`opengrid.trace.pg_backend` is health-owned and not built yet; delete this once it lands) |
| `main.py` | `og-settle` process wiring (`python -m opengrid.settle.main`) |

## How to test

```powershell
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\settle -q
```

Integration (Postgres, `og_t_settle`):

```powershell
powershell -File tools\remote.ps1 -Ws settle -Cmd "cd orchestrator && .venv/bin/pytest tests/integration/settle -q"
```
