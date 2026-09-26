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
- Calibration signing (06 §6.7, K14): `opengrid.guardian.evaluate_and_sign_calibration(proposed:
  ProposedCalibrationCommand) -> CalibrationCommand | None` runs G-20 then G-25 and returns the signed
  wire command (guardian-assigned per-hub `(epoch, seq)`, signature over `CalibrationCommand.signing_payload()`),
  or `None` (a hold; the refusal is traced). **Cross-process hand-off:** the ladder (`opengrid.assets`)
  records a `PENDING` `og.calibration_attempt` row with `command_batch_id IS NULL`; `og-guardian` polls it
  every cycle (`main.process_pending_calibrations`), and publishes each signed command on
  `<root>/cmd/cal/<hub_id>` (QoS 1). Its own `GUARDIAN_VERDICT` trace row (`kind='CALIBRATION'`, outcome
  `SIGNED`/`REFUSED`) claims the attempt, so it is evaluated at most once; rows older than
  `guardian.calibration_max_request_age_s` (600 s) are ignored. The signing key never leaves this process.
- Stop RELEASE (K8, crypto.md §2.3): `GuardianService.evaluate_and_sign_stop_release(request)` signs a
  RELEASE only for a Tier-2 request og-api recorded (`og.operator_action`: SAFE_STOP_RELEASE, TIER2,
  distinct `operator_ref`/`approver_ref`, both in `guardian.stop_release_authorised_operators`, approved
  within `guardian.stop_release_max_age_s`, traced), for a scope that is still engaged, whose ENGAGEs all
  predate the approval, that was not a utility stop, and with no ESTOP/BLOCK active on any of its banks.
  `main.process_pending_stop_releases` then hands each signed event to og-safestop, which verifies and
  publishes it. Full path: `stop_release.py` module docstring.
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
| `repo.py` | Postgres-backed port implementations (`PgCommitmentPort` incl. its own `active_obligations_for_bank` enumeration, `PgProposalPort`, `PgCalibrationQueuePort`, …) and the K12 clock adapters (`KernelClockPort`, default, reads `adjtimex` under any NTP daemon; `ChronyClockPort`), both fail-closed: any read failure or unsynchronised clock reports `inf`. Deliberately has no hub-state port — that must always be `mqtt_io.MqttHubStatePort`. |
| `mqtt_io.py` | Guardian's own independent inputs — the telemetry cache (`<root>/tel/#`, stale after `guardian.telemetry_max_age_s`) and utility instructions (`<root>/scada/instruction/+`, `MqttL2InstructionPort`) — and signed publish (`<root>/cmd/*/batch`, `<root>/cmd/cal/*`, `<root>/lease/*`). |
| `main.py` | Process wiring: pool, MQTT client, `run_forever` cycle over pending `og.command_batch` rows (publishing exactly the evaluated proposal) and pending calibration attempts. |

**Override corroboration (K13/G-19).** A reduction below the commitment lock is signed only when the
guardian's own reads back the batch's override reason: `R-COMMIT-LOCK-OVERRIDE-L2` needs an active
instruction in `MqttL2InstructionPort`; `-L0`/`-L1`/`R-COMMIT-LOCK-INFEASIBLE` need the bank's deliverable
discharge capability, computed from the guardian's own telemetry of every member hub (`bank_members`), to
be below the bank's committed floor. Anything else is vetoed.

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
