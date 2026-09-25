# opengrid.ledger

The single writer of `og.reservation` (02a S4, S1.9). Enforces the one-buyer invariant (K2) at
`reserve()` time and the commitment-lock invariant (K13) at `release()`/`reduce()` time, both via
`opengrid.core.limits` rather than re-deriving the checks.

## Interface (fixed, `orchestrator/INTERFACES.md`)

- `reserve(obligation_id, selected_kw_by_interval, plan_id)` -- the one-buyer check + commitment-lock
  entry point. All-or-nothing across every interval in `selected_kw_by_interval`; raises
  `ReservationError("R-COMMIT-LOCK-INFEASIBLE")` on failure.
- `release(reservation_id, reason_code)` -- releases a committed reservation in place. Raises
  `CommitmentLockViolation` unless `reason_code` is one of `ALLOWED_RELEASE_REASONS` (K13); `R-AS-RELEASE`
  additionally requires the ledger to be constructed with `as_release_enabled=True` (default off,
  review S7.4 / TS-05-12).
- `ledger_version()` -- the current monotonic ledger version (strictly increases on every write).
- `free_headroom(bank_id, interval_start)` -- `capability(bank, t) - committed(bank, t)`, served from
  an in-memory read cache for the 2 s allocator.

`ReservationLedger.reduce()` and `.substitute()` extend the fixed module functions for the allocator's
S5 substitution (moves a committed obligation's reservation to a different bank, preserving the
obligation's committed total -- review S3: hub substitution within one obligation is allowed, switching
to a different obligation is not).

`selected_kw_by_interval`'s string keys pack `"<bank_id>|<interval_start_iso>|<interval_end_iso>"`
(`encode_interval_key`/`decode_interval_key`) since the fixed `reserve()` signature carries no separate
bank/interval arguments.

## Architecture

Decision logic (`__init__.py`) has no `psycopg` import; persistence is isolated behind the
`LedgerBackend` protocol, mirroring `opengrid.trace.store`/`opengrid.trace.pg_backend`'s split
(BUILD.md S5a "pure logic separated from I/O"). `pg_backend.PgLedgerBackend` implements it over
`og.reservation` with `SELECT ... FOR UPDATE` inside a transaction plus an advisory lock for the version
counter, so concurrent `og-engine` processes serialize cleanly with no double sale.

A process wires one instance via `opengrid.ledger.configure(ReservationLedger(...))`; every other
package imports the module-level `reserve`/`release`/`ledger_version`/`free_headroom` functions.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\ledger -q
```

Hypothesis property tests (`test_ledger_properties.py`) replay random reserve/release/substitute
sequences and assert K2 (sum of reservations <= capability), K13 (no reduction without an allowed
reason), substitution preserving obligation totals, and the strictly-increasing ledger version.

Integration tests against real Postgres (`tests/integration/ledger/test_pg_ledger.py`) prove the
single-writer/no-double-sale property under concurrency; they auto-skip when no database is reachable
and are meant to run via:

```
powershell -File tools\remote.ps1 -Ws ledg -Cmd "cd orchestrator && bash tools/check.sh"
```
