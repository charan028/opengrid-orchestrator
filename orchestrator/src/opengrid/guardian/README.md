# opengrid.guardian

**Purpose.** The `og-guardian` process (02a §6): the sole Ed25519 signer for anything that moves MW. It
independently re-reads hub telemetry (its own MQTT `<root>/tel/#` subscription), bank SCADA, the ledger
version, commitments and prior grants, and runs the canonical checks G-01, G-02, G-03, G-04, G-05, G-06,
G-09, G-13, G-14, G-15, G-19, G-20 (00-invariants.md) using the *same* `opengrid.core.limits` /
`opengrid.core.timeutil` functions the allocator uses at planning time — independence comes from
guardian's own inputs, never from a second implementation of the formulas.

## Interface

- Fixed public entry point (`orchestrator/INTERFACES.md`): `opengrid.guardian.evaluate_and_sign(batch:
  CommandBatchRow) -> Verdict`. Delegates to a `GuardianService` installed once via `configure()`.
- Process entry point: `python -m opengrid.guardian.main` (unit `og-guardian`).
- Keygen CLI: `python -m opengrid.guardian keygen --out <dir> [--key-id guardian-2026a]` writes
  `<key-id>.key` (private seed, mode 0600) and `<key-id>.pub` (hex public key) — the file the fleet
  simulator loads to verify guardian-signed batches.

## Module map

| Module | Responsibility |
|---|---|
| `ports.py` | `Protocol`s for every independently-read input, plus the `ProposedBatch`/`ProposedItem` shape of the engine→guardian hand-off. No I/O. |
| `checks.py` | Pure G-01…G-20 check functions wrapping `opengrid.core.limits`/`timeutil`. No I/O. |
| `config.py` | `GuardianConfig` (`[guardian]`/`[allocator]` TOML, no hard-coded thresholds). |
| `keys.py` | Ed25519 signing-seed resolution (`GUARDIAN_SIGNING_SEED` env or key file) + `keygen`. |
| `service.py` | `GuardianService.evaluate_and_sign` — the decision engine: runs every check, classifies PASS/VETOED/PARTLY_VETOED/TIMEOUT, signs on PASS, traces the verdict. No I/O. |
| `repo.py` | Postgres-backed port implementations (`PgHubStatePort`, `PgCommitmentPort`, `PgProposalPort`, `ChronyClockPort`, …). |
| `mqtt_io.py` | Guardian's own independent telemetry cache (`<root>/tel/#`) and signed publish (`<root>/cmd/*/batch`, `<root>/lease/*`). |
| `main.py` | Process wiring: pool, MQTT client, `run_forever` cycle over pending `og.command_batch` rows. |

**Known open item for the architect:** `og.command_batch` has no `bank_id`/epoch/seq columns, so there is
no durable per-bank lease-sequence table yet. `InMemoryLeaseStatePort` (in `repo.py`) tracks the last
accepted (epoch, seq) per bank in guardian's own process memory; this is safe (a restart is strictly more
permissive of the next batch, never less safe) but should get a real `og.bank_lease` table in a follow-up
migration. Likewise, the proposed batch's per-hub item content is read back from the `RT_ALLOCATION` trace
pre-image payload (`_trace_payload_to_proposal` in `repo.py` documents the expected shape) rather than a
dedicated table, since `og.command_batch` only persists `merkle_root`/`command_count`.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\guardian -q --cov=opengrid.guardian --cov-report=term-missing
.venv\Scripts\python.exe -m ruff check orchestrator\src\opengrid\guardian orchestrator\tests\unit\guardian
.venv\Scripts\python.exe -m ruff format --check orchestrator\src\opengrid\guardian orchestrator\tests\unit\guardian
.venv\Scripts\python.exe -m mypy orchestrator\src\opengrid\guardian
```

Server integration (`og_t_guard`, topic root `ogtest/guard`):

```
powershell -File tools\remote.ps1 -Ws guard -Cmd "cd orchestrator && bash tools/check.sh"
```
