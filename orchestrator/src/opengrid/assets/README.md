# opengrid.assets

Asset-health state machine and remote-calibration workflow (WP-I, `docs/orchestrator/07-delivery/
06-service-profiles-and-power-quality.md` S5.5, S6.7; `00-invariants.md` K14).

## Purpose

Tracks each hub inverter's asset-health lifecycle (`og.hub_inverter_pq.asset_state`):

```
OK -> WATCH -> DEGRADED/DERATED -> QUARANTINED -> AWAITING_REPLACEMENT -> RECOMMISSIONING -> OK
```

and drives the remote-recalibration ladder ahead of any physical inverter swap: detect persistent drift
-> build a bounded, guardian-signed `CalibrationCommand` -> verify from a post-calibration waveform ->
`CORRECTED`/`IMPROVED`/`NO_CHANGE`/`WORSE_ROLLED_BACK` -> escalate to a maintenance work order and a
physical replacement only when recalibration cannot or does not correct the drift.

**Substitution** (moving delivery to different hubs, software) and **inverter swap/replacement**
(physical hardware) are never conflated here — the module only ever *requests* a calibration or records
a hardware replacement; it never decides substitution (that is the allocator's S5.4-step-2 concern).

## Interface

- `state_machine.py` -- pure `AssetState`/`DriftEvent` lifecycle (`next_asset_state`), no I/O.
- `calibration.py` -- builds the unsigned `CalibrationCommand` candidate (`build_calibration_candidate`),
  reusing `opengrid.core.pq.calibration`'s bounded-correction math; never signs anything (K3: only the
  guardian signs, via `opengrid.guardian.pq_checks.check_g25_calibration_safety`).
- `ports.py` -- `Protocol`s for every I/O dependency (`AssetHealthPorts`), so `service.py` has no
  Postgres/MQTT import.
- `service.py` -- `AssetHealthService`: `evaluate_drift`, `request_calibration`,
  `record_calibration_result`, `quarantine`, `open_work_order`, `schedule_replacement`,
  `record_inverter_replaced`, `verify_recommissioning`. Every transition is traced (K10/K11,
  `ASSET_STATE_TRANSITION`/`CALIBRATION_ATTEMPT`) via `AssetTracePort`.
- `repo.py` -- Postgres-backed port implementations against `og.hub_inverter_pq`'s asset-health columns
  and `og.calibration_attempt`/`og.maintenance_work_order`/`og.asset_event` (migration
  `0011_asset_health.sql`, owned by Agent A). `PgDriftObservationRepo` reads measured summaries through
  `opengrid.pq_ingest.latest_summaries` (Agent G's own read-through) rather than querying
  `og.pq_waveform_summary` itself.
- `runner.py` -- `run_once(service, now=...)`: the periodic drift-evaluation sweep (host process:
  `opengrid.settle`, see "Wiring" below). Per hub: `evaluate_drift` -> (`WATCH`: `request_calibration`,
  which durably records the candidate as a `PENDING` `og.calibration_attempt` row for the guardian to
  poll and sign -- this module never publishes anything itself) -> (`DEGRADED` with no open work order:
  `open_work_order`).
- `calibration_ack.py` -- `handle_calibration_ack(service, payload, now=...)`: the ack half of the
  calibration MQTT loop. Looks up the pending attempt's pre-calibration offset, classifies the outcome
  (`opengrid.core.pq.classify_calibration_outcome`), advances the asset-state lifecycle, and opens a
  work order on `NO_CHANGE`/`WORSE_ROLLED_BACK` (including a `REJECTED`/`EXPIRED` ack, via
  `AssetHealthService.record_calibration_rejected`).

## Wiring (for the live-path agent -- not implemented here, per BUILD.md S4 ownership)

**1. `opengrid.settle` -- periodic sweep.** Add a fifth `JobRunner` job beside `heartbeat`/
`health.evaluate_once`/`settle_job`/`trace_prune` in `settle/main.py`:

```python
from opengrid.assets.runner import run_once as run_asset_drift_sweep
from opengrid.assets.service import AssetHealthPorts, AssetHealthService
from opengrid.assets.repo import (
    PgAssetHealthRepo,
    PgCalibrationAttemptRepo,
    PgWorkOrderRepo,
    PgAssetEventRepo,
    PgDriftObservationRepo,
    TraceStoreAssetTracePort,
    PgSensitiveGrantPort,
)

asset_health_service = AssetHealthService(
    ports=AssetHealthPorts(
        drift=PgDriftObservationRepo(pool),
        asset_health=PgAssetHealthRepo(pool),
        calibration_attempts=PgCalibrationAttemptRepo(pool),
        work_orders=PgWorkOrderRepo(pool),
        asset_events=PgAssetEventRepo(pool),
        trace=TraceStoreAssetTracePort(trace_store),
        sensitive_grants=PgSensitiveGrantPort(pool),
    )
)


async def asset_drift_job() -> None:
    result = await run_asset_drift_sweep(asset_health_service, now=datetime.now(UTC))
    logger.info("asset drift sweep", extra=result.__dict__)


# alongside the other jobs, e.g.:
jobs.append(("asset_drift", Cadence(float(cfg.get("assets.drift_interval_s", 60.0))), asset_drift_job))
```

**2. Guardian -- sign and publish the calibration command.** `guardian/service.py`'s
`evaluate_and_sign_calibration` already exists but has a **payload-shape bug**: it signs only
`{hub_id, correction, bounds}`, while `CalibrationCommand.signing_payload()` (and ogsim's
`apply_calibration`/`_SIGNED_FIELDS`) sign over `calibration_id, hub_id, epoch, seq, issued_at,
expires_at, reference, correction, bounds` — every signature it produces today will fail ogsim's
`verify_calibration_signature` as `BAD_SIGNATURE`. Fix it to mirror `sign_command_batch`:

```python
def sign_calibration_command(self, command: CalibrationCommand) -> CalibrationCommand:
    return command.model_copy(
        update={"signature": sign_payload(self.signing_seed, command.signing_payload())}
    )
```

`main.py` needs a poll step (there is currently none) since nothing calls `evaluate_and_sign_calibration`
today: poll `og.calibration_attempt WHERE outcome = 'PENDING' AND command_batch_id IS NULL ORDER BY
requested_at`, build the `CalibrationCommand`/`ProposedCalibrationCommand` from each row's
`reference_*`/`correction_*`/a firmware-bounds lookup, run G-25 (`evaluate_and_sign_calibration`), and on
PASS: `sign_calibration_command`, publish via a new `publish_calibration_command` in `mqtt_io.py`
(mirrors `publish_command_batch`: `topic(cfg, f"cmd/cal/{command.hub_id}")`, qos=1, retain=False —
`og_guardian`'s existing `cmd/#` ACL scope already covers it), then mark the row claimed (e.g. set
`command_batch_id` to a synthetic id, or add an additive `guardian_picked_at`/`published_at` column to
`og.calibration_attempt` if a clean claim marker is wanted — flagged here, not added by this package).
On refusal, leave the row `PENDING`; it is naturally re-evaluated (and re-rate-limited) on `assets`'s next
sweep or simply expires via its own `expires_at` once built into a candidate again.

Also add to `orchestrator/src/opengrid/platform/mqtt.py`'s `_SCHEMA_BY_KIND`:
`"calibration_command": "calibration_command.schema.json"` and
`"calibration_ack": "calibration_ack.schema.json"` (missing today; `validate_payload` for either kind
currently raises `KeyError`). `platform/` is outside this package's edit scope.

**3. Engine -- route inbound acks.** In `opengrid.engine`'s MQTT ingest loop (the same one that already
subscribes to `ack/#`), route `ack/cal/<hub_id>` to:

```python
from opengrid.assets.calibration_ack import handle_calibration_ack

# inside the ack-topic branch, when the suffix is "cal/<hub_id>" rather than a CommandBatch ack:
await handle_calibration_ack(asset_health_service, payload)
```

(`asset_health_service` is the same instance settle's job uses, or an equivalent engine-owned one wired
against the same Postgres pool -- either is fine, `AssetHealthService` holds no in-process state beyond
its ports.)

**4. ogsim.** Done in this change: `ogsim.fleet.runtime.run_fleet` now subscribes to `cmd/cal/+`, and
`ogsim.fleet.__main__._dispatch_message` routes it to the existing (unmodified)
`FleetEngine.handle_calibration_command`, publishing the ack (narrowed to the wire schema's fields) on
`ack/cal/<hub_id>`.

## Known limitations (see the build report for the full list)

- `PgSensitiveGrantPort`/`PgDriftObservationRepo` (`repo.py`) note in their own docstrings where they
  depend on data or a query shape another agent's wiring should confirm (no dedicated hub-level grant
  table in MVP-S's schema yet; no bank/fleet-wide correlated-event feed yet).
- `runner.py`'s calibration `epoch`/`seq` are placeholders (K6-style command freshness has no durable
  per-hub lease table for the calibration channel yet, and `evaluate_and_sign_calibration` does not
  currently check them at all) -- see "Wiring" item 2.
- `DEFAULT_CALIBRATION_BOUNDS` (`runner.py`) duplicates the three literals in guardian's own
  `StaticFirmwareCalibrationBoundsPort` (`guardian/pq_repo.py`) by value, not by import (the two packages
  have no shared owner for this constant today) -- keep them in sync until a real per-firmware-family
  bounds table exists.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\assets -q
```

`repo.py`'s Postgres adapters need a live database; run them against a workspace via
`tools/remote.ps1 -Ws assets -Cmd "..."` once migration `0011_asset_health.sql` is applied there.
