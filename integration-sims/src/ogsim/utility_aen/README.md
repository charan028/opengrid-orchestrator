# ogsim.utility_aen

The simulated utility EMS for the D-29 tolling contract. By default it is Austin Energy (`AUSTIN_ENERGY`).
It calls the orchestrator the way a real utility would. It shares no code with `opengrid`.

## What it does

- **Peak schedule:** on a hot day it issues one discharge call inside the evening window (16:30-18:00
  America/Chicago by default). The call is `call_kw` for `duration_min`, capped at the 90 min product.
  It then reads the call's status every `status_poll_s` until the call completes, logging the
  orchestrator's MEASURED delivery (`delivered_kw` signed, `delivered_kwh`, `delivery_state`; None/UNMEASURED
  until DELIVERY-VERIFY has evaluated it). `weather.mode` selects
  how hot days are chosen:
  - `random`: a reproducible daily high per seed and date; a day is hot at or above `hot_threshold_f`;
  - `hot` / `mild`: force every day hot, or every day not hot.
- **No reservation, no call:** if the channel can list obligations and today has no toll reservation
  (for example, a suspended contract), the day is skipped.
- **Scenarios (/ogsim/, catalogue owner `utility`):** `utility_call_normal`, `utility_call_overlap`,
  `utility_call_over_cap`, `utility_call_charge` and `utility_call_cancel_mid`. They are triggered by
  `<root>/scenario/cmd` targeting the utility_id (or `*`). Each logs `utility_scenario_result` with
  PASS/FAIL against its expected orchestrator answer. The five `scenarios/utility-aen-*.yaml` files put
  them on the control page's scenario list.
- **Utility parameter:** `utility:` (env `OGSIM_UTILITY`) with `utilities.<id>.{env_code, enabled}`.
  LCRA and RAYBURN are listed but disabled (D-37). A disabled utility stays idle: no channel is built,
  no credential is read, no MQTT connection is made.

## Channels

`channels/base.py` defines `Channel` (`issue_call`, `cancel`, `status`) with the types `CallSpec` and
`CallResult`. The configured transport is `channel:`, and its settings come from `channels.<name>`:

- `customer_api`: HTTPS through Apache to `/og/api/customer/v1/utility/`.
- `grid_link`: DNP3, from the GRID-LINK lane.

Env and credentials:

- `OGSIM_UTILITY_API_BASE`;
- `OGSIM_UTILITY_<env_code>_USER` / `_PASSWORD`;
- `OG_MQTT_UTILITY_PASSWORD` (MQTT user `og_sim_utility`, read `og/v1/scenario/cmd` only).

They are all in `/etc/opengrid/utility_sim.env` and are never logged.

## Run and test

- Unit `og-sim-utility.service`, part of `ogsim.target`. Install with
  `deploy/scripts/r343_utility_sim_install.sh` (a dry run unless `APPLY=1`).
- Tests: `.venv\Scripts\python.exe -m pytest integration-sims\tests\test_utility_aen.py -q`. They use a
  fake channel and a fake clock; no orchestrator or broker is needed.