"""Firmware update campaigns (R3.1, owner decision 2026-09-26; operator note: docs/orchestrator/07-delivery/
16-firmware-updates.md).

- `model`         states, records, the signed `FirmwareCommand` wire model
- `catalogue`     the allowed (version, hardware revision, sha256) images
- `planner`       pure: waves, caps, job state machine, retries, halt/complete
- `campaigns`     operator lifecycle: propose/confirm/approve (two-person), pause/resume/abort, retry, rollback
- `executor`      og-engine: advances campaigns each cycle, requests commands, alerts
- `guardian_flow` og-guardian: G-36 check (`opengrid.guardian.firmware_check`), sign, trace, publish
- `ingest`        the hub's firmware status -> og.firmware_command
- `repo`          Postgres (migration 0037)

Kept import-light on purpose: importing this package pulls in nothing else.
"""
