# OpenGrid Orchestrator: 20-step sign-off demo

A presenter runs this start to finish in about 10 minutes against the dev stack or the server. Every
injected moment is a scenario file in `integration-sims/scenarios/demo-*.yaml`, so the run is repeatable.
The operator guide (`docs/operator/README.md`) explains each screen in depth; this document only says what
to do, where to look, and what must happen.

## Real / simulated / derived / live

| Data | Kind | Where it comes from |
|---|---|---|
| ERCOT prices, load, wind, solar, ancillary-service prices; EIA; NWS weather | **Real, live** | Public APIs, polled by `og-feeds`. The Markets freshness table labels each feed `live` or `simulated` |
| Hub telemetry (2,000 hubs), SCADA bank load (40 banks), utility instructions | **Simulated** | `ogsim.fleet`, `ogsim.scada`, autonomous, over MQTT |
| Anomalies: price spike, bank overload, comms loss, forged command | **Simulated, injected on demand** | `ogsim.control` (this demo's `demo-*.yaml` scenarios) |
| Forecasts (P10/P50/P90), selector plans, commitments, grants, guardian verdicts, alerts, P&L, invoice lines, trace chain | **Derived** | Computed by the orchestrator from the rows above. Nothing on any screen is LLM-generated |

## URLs, accounts, ids used below

| Thing | Server | Local dev stack |
|---|---|---|
| Orchestrator UI | `https://base.tocy-net.net/og/` | `http://127.0.0.1:8080/og/` |
| Orchestrator API | `https://base.tocy-net.net/og/api/...` | `http://127.0.0.1:8080/og/api/...` |
| Sim control plane (UI + API) | `https://base.tocy-net.net/ogsim/` | `http://127.0.0.1:8091/` |

- UI/API accounts: **operator** (can act) and **viewer** (read only; every write button is hidden). Control
  plane account: **tester**. Passwords are in the lead's credentials file, never in this repo. In the `curl`
  lines below `$OG` is the orchestrator base, `$SIM` the control-plane base, and `-u operator:...` stands for
  the operator credentials (behind Apache). On the local stack there is no Apache, so send the identity header
  instead: `-H 'X-Remote-User: operator'`.
- Fleet ids: hubs `hub-00000`..`hub-01999`, banks `bank-000`..`bank-039`, zones `LZ_NORTH`, `LZ_SOUTH`,
  `LZ_HOUSTON`, `LZ_WEST`. Hub `i` sits on bank `i mod 40` in zone `i mod 4` (0 NORTH, 1 SOUTH, 2 HOUSTON,
  3 WEST). So LZ_SOUTH is exactly banks `bank-001, -005, ..., -037` (500 hubs).
- Targets this demo uses, so you know where to look:

| Moment | Scenario | Target | Screen to watch |
|---|---|---|---|
| Price spike during delivery | `demo-01-price-spike-lock` | `np6-905-cd` (wholesale), `np4-188-cd` RRS | Dispatch, Control room, Profitability |
| SCADA overload | `demo-02-bank-overload` | `bank-012` (LZ_NORTH, 75 kVA) | Health, Dispatch |
| Comms loss | `demo-03-zone-comms-loss` | `hub-00001` then zone `LZ_SOUTH` | Fleet, Health |
| Forged command | `demo-04-tampered-command` | `hub-00142` (bank-022, LZ_HOUSTON) | Fleet drill-down, Control room |
| Scoped safe stop | operator action, no scenario | `bank-022` | Fleet |

Scenario anomaly ids are deterministic: `<scenario>:<type>:<at_s>`, e.g.
`demo-02-bank-overload:bank_overload:0`. That is the id you `DELETE` to end a moment early.

## Before you start (5 minutes, not part of the 10)

1. **Pause random anomalies** so nothing fires mid-demo:
   `curl -u tester:... -X POST $SIM/api/random/pause` → JSON with `"paused": true`. Confirm nothing is active:
   `curl -u tester:... $SIM/api/anomalies` → `{"active": []}`. Cancel any leftover with
   `curl -X DELETE $SIM/api/anomalies/<id>`.
2. **All seven processes ok.** Open `/og/health`. The Processes table shows `feeds, engine, guardian, safestop,
   sim, settle, api` all `ok`, no degraded banner, and the Alerts table has no open critical alert.
3. **Several customers committed.** The Dispatch header must already read at least `3 customers`, with cards
   in COMMITTED or DELIVERING on `bank-012` for the current window (services ERCOT_ENERGY, DIST_DEFERRAL and
   one more). If not, the operator creates them (contracts first, then opportunities; the selector commits at
   the next gate):
   ```bash
   # one contract per customer (repeat with a new customer_id and service_type)
   curl -u operator:... -X POST $OG/api/contracts -H 'Content-Type: application/json' -d '{
     "customer_id": "'$(uuidgen)'", "service_type": "ERCOT_ENERGY", "tier": "T2",
     "profile_ref": "demo", "start_at": "2026-09-26T00:00:00Z"}'
   # one opportunity per contract. The window must start at a quarter-hour boundary at least ONE FULL
   # GATE ahead: the gate that runs at :00/:15/:30/:45 first expires any offered window that has already
   # started (R-EXPIRED-UNSELECTED) and only then selects. Posting at 02:58 for a 03:00 window is expired
   # at 03:00; post for 03:30 and the 03:15 gate commits it (confirmed live).
   curl -u operator:... -X POST $OG/api/opportunities -H 'Content-Type: application/json' -d '{
     "contract_id": "<contract_id from above>", "window_start": "<quarter hour after next, ISO UTC>",
     "window_end": "<+30 min>", "requested_kw": 40}'
   ```
   A DIST_DEFERRAL contract must exist on bank-012 or step 10 has no PI loop to show.
4. **Two browser windows**: one logged in as **operator** (the one you present from), one as **viewer** on the
   Fleet screen, to show in step 3 that the viewer has no Propose buttons.
5. Open the control-plane UI (`$SIM/`) in a third tab: its Scenarios panel lists the four `demo-*` scenarios
   with a **Run** button each; the Active anomalies panel has a **Cancel** button per id. Use it or the
   `curl` lines, whichever you prefer to show.

## The 20 steps

Timing is the budget per step; the total is about 10 minutes. Steps marked **[JUDGES]** are the 2-minute
short version for hackathon judges: steps 7-8, 13 and 20 (run the price spike, show the lock holding and the
forgone upside; run the forged command and show it rejected; run chain verify).

### Topic 1: live feeds

**Step 1: The control room is live** (30 s)
- **Action:** Open `/og/` as operator.
- **Show:** Header badge; the Market ticker panel; the seven KPI tiles.
- **Expect:** Header badge reads `live` (not `reconnecting`). Every tile shows an age badge under 10 s. The three
  invariant tiles **Reserve breaches**, **kWh sold twice**, **Commitment switches** all read `0` with a green
  badge. Market ticker shows a moving ERCOT price line.

**Step 2: Real feeds, labelled as such** (30 s)
- **Action:** Open `/og/markets`.
- **Show:** The "Freshness & source status" table, then the "Forecast band (P10 / P50 / P90)" chart.
- **Expect:** Rows for the ERCOT products (`np6-905-cd` price, `np4-188-cd` AS, load, wind, solar), EIA and
  NWS, each with mode `live`, age under its window, `0` consecutive failures and breaker `closed`. Header badge
  says `poll: 30s`. The forecast band is drawn ahead of now; the P50 line lies between P10 and P90.

### Topic 2: the fleet

**Step 3: 2,000 hubs, 40 banks, 4 zones** (30 s)
- **Action:** Open `/og/fleet`. Set the filter to zone `LZ_NORTH`, bank `bank-012`
  (`/og/fleet?zone=LZ_NORTH&bank=bank-012`). Glance at the viewer window on the same screen.
- **Show:** The Hubs table and its filters; the "Scoped safe stop" and "Manual command" panels.
- **Expect:** Unfiltered, the table paginates 2,000 rows, all `online`. Filtered, exactly 50 hubs
  (`hub-00012, hub-00052, ..., hub-01972`) all `online`, telemetry age under 4 s, SoC around the reserve line
  and power non-zero on hubs that are delivering. The operator window shows both **Propose** buttons; the viewer
  window shows neither.

**Step 4: One hub up close** (30 s)
- **Action:** Click the `hub-00012` row (or press Enter on it): `/og/fleet/hubs/hub-00012`.
- **Show:** The drill-down: lease epoch, lease expiry, last command id, telemetry sparkline.
- **Expect:** Lease expiry is in the future and renews (30 s TTL); last command id changes every 2 s cycle;
  the sparkline moves. Say once: a hub only executes a command signed by the guardian's key for the current
  lease epoch, which step 13 will prove.

### Topic 3: several customers committed at once

**Step 5: Several customers in the pipeline** (30 s)
- **Action:** Open `/og/dispatch`.
- **Show:** "Opportunity pipeline (N customers, M obligations)" board.
- **Expect:** Header shows `3 customers` or more. Cards in **COMMITTED** and **DELIVERING** with different
  service types (ERCOT_ENERGY, DIST_DEFERRAL, ...), tier and committed kW. No card has an amber at-risk
  border. The "Latest selector plan" panel shows mode `lp` (not `rule`), a gate time, and solver status
  optimal.

**Step 6: One bank, several committed bands** (30 s)
- **Action:** Pick `bank-012` in the ledger timeline selector.
- **Show:** "Ledger timeline: bank bank-012" stacked chart; "Commitment-lock events (K13)" table.
- **Expect:** One coloured band per committed obligation stacked to the bank's capacity, free headroom on top.
  The lock-events table is empty (`No lock events`) or contains only rows older than this run. Say: new
  opportunities may only take the headroom band.

### Topic 4: a price spike during delivery, the lock holds

**Step 7 [JUDGES]: Inject a $5,000/MWh spike** (30 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-01-price-spike-lock/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
  (or press **Run** next to `demo-01-price-spike-lock` in the control-plane UI).
- **Show:** Control room Market ticker, then Markets.
- **Expect:** Response `{"ok": true, "scenario": "demo-01-price-spike-lock", "steps": 2}`. Within 30 s (next
  feed poll) the ticker's price line jumps to `5000` on `np6-905-cd`; Markets shows the RRS AS price at `800`.
  `$SIM/api/anomalies` lists `demo-01-price-spike-lock:price_spike:0` and `...:as_price_jump:0` as active.

**Step 8 [JUDGES]: The committed customers keep their capacity** (45 s)
- **Action:** Back to `/og/dispatch`, ledger on `bank-012`; then `/og/` tiles; then `/og/profitability`.
- **Show:** The COMMITTED/DELIVERING cards and their committed kW; the "Commitment-lock events (K13)" table;
  the **Commitment switches** tile; the **Forgone upside (lock)** tile.
- **Expect:** Every committed card keeps the same kW; **no new row** appears in Commitment-lock events (the only
  reasons that may ever appear there are `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2` and `R-COMMIT-LOCK-INFEASIBLE`,
  none of which is a price). **Commitment switches** stays `0` green. Within one profitability refresh
  (30 s) **Forgone upside (lock)** turns non-zero with a caution badge: the money the fleet chose not to
  chase. Any new opportunity created now (repeat the step 3 `curl` in "Before you start") lands as OFFERED
  and, if selected, takes only headroom.

### Topic 5: a SCADA overload, DIST_DEFERRAL responds, an alert fires

**Step 9: Overload bank-012** (30 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-02-bank-overload/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
- **Show:** `/og/health`, Alerts table.
- **Expect:** Within two SCADA cycles a new alert row: rule `ALR-SCADA-OVERLOAD`, severity **critical**
  (25% over the 75 kVA rating is above the 120% critical line), summary
  `Bank bank-012 SCADA load 93.8 kVA over rating 75.0 kVA (125%)`. The same row appears in the Control room
  "Open alerts".

**Step 10: The DIST_DEFERRAL loop pulls the bank back** (45 s)
- **Action:** `/og/dispatch`, "Real-time grants & substitutions" table and the bank-012 ledger.
- **Show:** Grant rows for `bank-012`; the DIST_DEFERRAL card's committed kW; the Health alert.
- **Expect:** Over the next 2 s cycles the DIST_DEFERRAL grant on bank-012 rises (more discharge to relieve the
  substation) with kind/reason `R-GRANT-DIST-DEFERRAL-PI`, while every committed kW on the cards is unchanged
  and no Commitment-lock event is written. If you also want to show the utility path, inject
  `utility_instruction` `{mode: limit, limit_kw: 50}` on bank-012 by hand: the lock-events table then gets a
  row with `R-COMMIT-LOCK-OVERRIDE-L2`, the only legitimate way a price-independent instruction reduces a
  commitment. The alert clears on its own when the anomaly ends (300 s), or now via
  `curl -X DELETE $SIM/api/anomalies/demo-02-bank-overload:bank_overload:0`.

### Topic 6: zone comms loss, substitution

**Step 11: One hub goes quiet, the bank covers for it** (45 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-03-zone-comms-loss/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
  then open `/og/fleet?bank=bank-001`.
- **Show:** The 50 hubs of `bank-001`; `hub-00001`'s row; the bank-001 grant in Dispatch.
- **Expect:** `hub-00001` age climbs; after 6 s its badge reads `stale`, after 30 s `offline`. Its power drops to
  `--` while the remaining 49 hubs' power rises so the obligation on bank-001 keeps its granted kW
  (substitution, reason `R-SUBSTITUTION`, a grant change and never a commitment write). No lock event, no
  alert yet (1 of 500 LZ_SOUTH hubs is 0.2%, under the 5% warning line).

**Step 12: The whole zone goes quiet** (45 s)
- **Action:** At +90 s the scenario disconnects all of `LZ_SOUTH`. Open `/og/health`, then `/og/`.
- **Show:** "Hub health by zone" chart; Alerts table; Control room fleet map; Dispatch pipeline.
- **Expect:** LZ_SOUTH's bar turns to 500 `offline` while the other three zones stay `online`. Alert
  `ALR-HUB-OFFLINE-RATIO` for `LZ_SOUTH`, severity **critical** (`ratio 100.0%`, over the 20% line). The
  fleet map shows the LZ_SOUTH hubs amber. Any obligation on an LZ_SOUTH bank with no substitute left moves to
  SHORTFALL with `R-COMMIT-LOCK-INFEASIBLE` in the lock-events table: the one honest reason a commitment
  cannot be met. Everything on LZ_NORTH/HOUSTON/WEST is untouched. Say: the hubs themselves keep serving their
  homes on local autonomy once their lease expires (30 s). Clears itself at 240 s; to end now,
  `curl -X DELETE $SIM/api/anomalies/demo-03-zone-comms-loss:zone_mass_disconnect:90` (and `...:hub_offline:0`).

### Topic 7: a forged command is rejected

**Step 13 [JUDGES]: Forge a command to hub-00142** (30 s)
- **Action:** Open `/og/fleet/hubs/hub-00142` first, then
  `curl -u tester:... -X POST $SIM/api/scenarios/demo-04-tampered-command/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
- **Show:** The hub-00142 drill-down (power, last command id), then the three invariant tiles on `/og/`.
- **Expect:** The forged batch asks hub-00142 for -5 kW with `key_id: forged-key` and an empty signature. The hub
  refuses it with reject reason `BAD_SIGNATURE` (signature is checked before freshness, so a forged key never
  gets as far as the epoch/seq check): its power and last command id do not change, no ack is applied, and
  **Reserve breaches / kWh sold twice / Commitment switches** stay `0`. The fleet simulator logs
  `fleet self-test: forged command correctly rejected (BAD_SIGNATURE)` (`journalctl -u og-sim-fleet`) and
  publishes the rejected ack on `og/v1/ack/hub-00142` with `"accepted": false, "reject_reason": "BAD_SIGNATURE"`.

**Step 14: The legitimate path, for contrast** (45 s)
- **Action:** On `/og/fleet`, Manual command panel: hub `hub-00142`, setpoint `2`, reason `demo signed path`,
  **Propose (step 1 of 2)**; read the summary; **Confirm** before the 60 s countdown ends.
- **Show:** The confirm dialog (focus starts on Cancel, Tab to Confirm, Escape closes); the result badge.
- **Expect:** Badge `PASS` (guardian signed it; the drill-down's last command id changes and power moves toward
  2 kW), or `VETOED` with the guardian rule ids listed (for example `G-02` hub power bound or `G-19` commitment
  lock if 2 kW would take capacity from a committed band). Either way the setpoint reached the hub only via
  a guardian-signed batch. If you confirm after 60 s: `EXPIRED`, propose again.

### Topic 8: a scoped safe stop

**Step 15: Propose a bank-scoped stop** (30 s)
- **Action:** `/og/fleet`, "Scoped safe stop" panel: scope `Bank`, scope id `bank-022`, reason
  `forged command seen on bank-022`, **Propose safe stop (step 1 of 2)**.
- **Show:** The proposal summary and the 30 s countdown.
- **Expect:** Summary reads `Engage safe stop on bank/bank-022 (forged command seen on bank-022)`; nothing has
  stopped yet (Fleet power tile unchanged). Say: a single message can never stop anything, and og-safestop is
  its own process with its own key, so this works even with engine and guardian down.

**Step 16: Confirm and watch the ramp** (45 s)
- **Action:** Press **Confirm** inside the 30 s window. Then filter `/og/fleet?bank=bank-022`.
- **Show:** The result line; bank-022's 50 hubs; the Fleet power tile.
- **Expect:** `Safe stop engaged for bank/bank-022.` Bank-022's hubs ramp to 0 kW over 30 s (bank ramp window);
  the other 39 banks keep delivering; Fleet power drops by bank-022's share only. No hub goes below its home
  reserve (Reserve breaches still `0`). If you confirm late: `proposal expired, propose again`. Note out loud:
  release is not built (the stop-only key cannot sign a release), so the lead lifts this stop afterwards; that
  is why it is near the end of the run.

### Topic 9: profitability and billing

**Step 17: Where the money went** (45 s)
- **Action:** Open `/og/profitability`, service filter `All`, today.
- **Show:** **Net margin** and **Forgone upside (lock)** tiles; the "Per obligation / day" table; the
  "LP vs rule baseline" panel.
- **Expect:** Net margin positive. Forgone upside (lock) non-zero from step 8, with the obligations count that
  carried it. The table has one row per obligation per day (revenue, energy cost, degradation cost, penalty,
  net); the DIST_DEFERRAL row from step 10 shows revenue, any LZ_SOUTH shortfall from step 12 shows a penalty.
  LP vs rule baseline shows the optimiser ahead of (or equal to) the rule selector. Header badge `poll: 30s`.

**Step 18: Invoice lines and M&V** (30 s)
- **Action:** Open `/og/billing`. Press **Export CSV**.
- **Show:** Invoice lines table (Line, Contract, Obligation, Period, ...); "M&V performance" panel.
- **Expect:** One line per settled obligation-interval per contract, matching the Profitability rows for the
  same day; a CSV downloads with the same lines. M&V shows delivered vs committed per obligation; the
  step-12 shortfall, if any, shows under-delivery against its committed kW.

### Topic 10: the audit chain

**Step 19: Every decision in the trace** (45 s)
- **Action:** On `/og/billing`, Trace explorer: leave filters blank, or filter class `SAFE_STOP_ENGAGE`.
- **Show:** The trace rows (Trace, Decision, Class, Stream, ...).
- **Expect:** Rows for this run in order: `OPERATOR_ACTION`/`SAFE_STOP_ENGAGE` for bank-022 carrying the
  operator's identity and the exact reason text from step 15; `OPERATOR_ACTION` for the manual command of
  step 14; `RT_ALLOCATION` records every 2 s; the shortfall/lock transitions from step 12 with their
  `R-COMMIT-LOCK-INFEASIBLE` reason; and the alerts from steps 9 and 12. Nothing from steps 7-8 reduced a
  commitment: there is no lock-override record for the spike.

**Step 20 [JUDGES]: Verify the chain** (30 s)
- **Action:** Press **Run chain verify** (or `curl -u viewer:... -X POST $OG/api/trace/verify -H 'Content-Type: application/json' -d '{}'`).
- **Show:** The verify result line.
- **Expect:** `passed: true`, `checked: <number of streams>`, `first_broken: null`. Say: records are
  hash-chained per stream, so nothing above could have been edited or removed without this turning to
  `passed: false` with the first broken `stream_id`/`seq`. That closes the run.

## Reset between runs (3 minutes)

1. **End anything still active**: `curl -u tester:... $SIM/api/anomalies`, then
   `curl -u tester:... -X DELETE $SIM/api/anomalies/<id>` for each id (the four scenarios expire on their own at
   300 s, 300 s, 300 s and 60 s). Confirm `{"active": []}`. The scenario runner has no stop verb: a scenario
   whose later steps have not fired yet (`demo-03` before +90 s) still fires them; cancel those ids when they
   appear, or wait for `GET $SIM/api/scenarios/demo-03-zone-comms-loss/status` to read `"running": false`.
2. **Wait for health to clear**: `ALR-SCADA-OVERLOAD` and `ALR-HUB-OFFLINE-RATIO` clear themselves once the
   condition ends; LZ_SOUTH hubs return to `online` within one telemetry interval of the cancel.
3. **Lift the safe stop on bank-022**: not possible from the UI or API in this release (the release endpoint
   answers `501` and records the attempt). The lead clears the stop on the server (`og-safestop` owner). If the
   next run is soon, skip steps 15-16, or stop a different bank that carries no committed obligation.
4. **Resume random anomalies** if the system is going back to normal operation:
   `curl -u tester:... -X POST $SIM/api/random/resume`. Leave it paused if another run follows.
5. **Fresh commitments**: the customers from "Before you start" stay; create new opportunities for the next
   window so steps 5-8 have COMMITTED cards again.

## Known gaps

What this script relies on that is not yet in the code is listed, with owner, in `NEEDS_FROM_OTHER_OWNERS.md`
next to this file. Read it before presenting to judges: the two places that need a fallback are the
forged-command visibility (step 13) and the per-hub substitution rows (step 11).
