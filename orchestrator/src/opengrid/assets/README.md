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
  `0011_asset_health.sql`, owned by Agent A).

## Known limitations (see the build report for the full list)

- `PgSensitiveGrantPort`/`PgDriftObservationRepo` (`repo.py`) note in their own docstrings where they
  depend on data or a query shape another agent's wiring should confirm (no dedicated hub-level grant
  table in MVP-S's schema yet; no bank/fleet-wide correlated-event feed yet).
- Guardian wiring (the calibration-signing method on `GuardianService`, and where `AssetHealthService`
  hands a `CalibrationCandidate` to it) is **not** implemented in this package -- `opengrid.guardian.
  service` is owned by the live-path agent. See the build report for the exact call shape to add.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\assets -q
```

`repo.py`'s Postgres adapters need a live database; run them against a workspace via
`tools/remote.ps1 -Ws assets -Cmd "..."` once migration `0011_asset_health.sql` is applied there.
