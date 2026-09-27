# 16 — Firmware updates from the console (operator note)

Status: R3.1, 2026-09-26 (owner decision: operators dispatch firmware updates to batteries from the console,
safely). Code: `opengrid.firmware`, guardian check G-36 (`opengrid.guardian.firmware_check`), API
`/og/api/firmware/*`, migration 0037, wire schemas `interfaces/mqtt/firmware_command.schema.json` and
`firmware_status.schema.json`. Related runbook: RB-005 (firmware cohort regression, settings drift) in
`02-architecture/05-failure-modes-and-recovery.md` §5.6.25.

## What you can and cannot do

- **Only catalogued images.** A campaign targets a version from the firmware catalogue (`GET
  /og/api/firmware/catalogue`): version × hardware revision, image sha256 and release note. There are no free
  version strings. Hubs whose hardware revision has no entry for the version, or which already run it, are
  listed as SKIPPED when you propose.
- **No silent downgrades.** An older version needs `allow_downgrade`, and that always needs a second operator.
- **Every command is guardian-signed (K3).** The console never sends anything to a battery. The engine asks,
  and the guardian re-checks each hub itself (G-36) and signs one command per attempt with a fresh
  `(epoch, seq)` and a 120 s lease (K6).

## Running a campaign

1. **Propose:** `POST /og/api/firmware/campaigns` with the target version, the hubs (explicit ids, or a
   filter: banks, zones, feeders, hardware revisions, current versions), the waves (default: canary of 1 hub,
   then 25% steps), the caps (default: at most 10% of a bank's hubs and 10% of a feeder's hubs updating at
   once, minimum 1), the halt threshold (default: 2 terminal failures or 5% of the campaign, whichever comes
   first), an optional maintenance window, and a reason. The hub list is fixed at this moment.
2. **Confirm within 60 s:** `POST .../{id}/confirm`. An expired proposal is aborted; propose again.
3. **Second operator,** when the proposal says `requires_second_operator`: more than 50 hubs, any hub serving
   a COMMITTED/DELIVERING obligation with `override_committed`, or a downgrade. A different operator calls
   `POST .../{id}/approve`. The proposer and the confirmer can never approve (the same two-person pattern as
   the safe-stop release).
4. The engine starts the campaign on its next cycle and works through the waves. The next wave starts only
   when every hub of the current one has finished. A canary wave with no success halts the campaign.

## What protects the grid and the customer (G-36, per hub, per attempt)

The guardian refuses to sign unless: the campaign is RUNNING; the hub is online and not inside a safe stop;
it is not already updating; it serves no committed obligation in the next 15 min (unless overridden with two
operators, in which case the engine moves its committed kW to other hubs, as it does for a manual target);
SoC ≥ reserve + 10% of capacity; the image and its sha256 are in the catalogue for the hub's hardware; and the
bank and feeder caps hold. A refusal is a hold, not a failure: the hub is re-checked 60 s later. Catalogue
refusals are terminal.

## Watching it

- `GET .../campaigns/{id}` shows per-hub state (PENDING, SENT, UPDATING, SUCCEEDED, FAILED, ROLLED_BACK,
  SKIPPED), attempts, next retry time and reason, plus counts.
- `GET .../campaigns/{id}/events?after=<event_id>` is the timestamped feed of every transition: SENT, ACKED,
  UPDATING, SUCCEEDED, FAILED, RETRY_SCHEDULED, DEFERRED, ROLLED_BACK, the hub's own progress
  (HUB_DOWNLOADING, HUB_INSTALLING, HUB_REBOOTING, HUB_DONE, HUB_FAILED, stamped with the battery's time), and
  campaign started, paused, halted and completed.
- **Alerts** (alerts panel, bulk ack): `ALR-FIRMWARE-CAMPAIGN-STARTED` and `ALR-FIRMWARE-CAMPAIGN-COMPLETED`
  (info), `ALR-FIRMWARE-HUB-FAILED` (warning, one per hub that failed for good) and
  `ALR-FIRMWARE-CAMPAIGN-HALTED` (critical).

## Failures and retries

- **Retried automatically** (up to 3 attempts, backoff 60 s, 120 s, 240 s … capped at 15 min; each retry is
  re-checked and re-signed): no ack before the lease expired, the hub went quiet mid-update (10 min), download
  or verify errors.
- **Never retried; terminal and alerted:** sha256 mismatch, hardware incompatibility, a bad signature, an
  install or boot failure (the battery reverts to its previous version by itself).
- Only terminal failures count toward the halt threshold.

## Operator actions

| Action | How | Notes |
|---|---|---|
| Pause / resume | `POST .../{id}/pause`, `.../resume` | Hubs already updating finish; nothing new starts. |
| Abort | `POST .../{id}/abort` | Pending hubs become SKIPPED. A hub mid-install cannot be recalled. |
| Retry failed hubs | `POST .../{id}/retry-failed`, then `.../retry-failed/{proposal_id}/confirm` | Fresh attempt budget. A HALTED campaign becomes PAUSED; resume it deliberately. |
| Roll back one hub | `POST .../{id}/hubs/{hub_id}/rollback`, then `.../rollback/{proposal_id}/confirm` | Installs the version the hub ran before the campaign. Allowed while paused, halted or completed. |

A HALTED campaign never resumes directly. Read the failures first (RB-005): compare the failed hubs' cohort
against the control cohort before you retry, and roll back affected hubs if the image is suspect.
