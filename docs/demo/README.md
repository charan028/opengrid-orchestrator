# OpenGrid Orchestrator: sign-off demo (DEMO-2, release R3.1)

A presenter runs this start to finish in about 15 minutes against the server or the local dev stack. Every
injected moment is a scenario or anomaly on the simulator control plane (`/ogsim/`), so the run is repeatable.
The operator guide (`docs/operator/README.md`) explains each screen and action in depth; this document only
says what to do, where to look, and what must happen.

What changed since DEMO-1 (R1.6):
- the two-person safe-stop release is built (steps 19-20);
- ERCOT AS awards are held, and deployed by an operator (steps 9-10);
- the guardian escalates on repeated vetoes (steps 15-16; not runnable at R3, see step 15);
- a stale feed stops new commitments and shows a banner (step 18);
- a lost delivery continues on best effort (step 14);
- prices are per load zone (step 2).

The Health screen is now called **System Health**.

## Step markers

| Marker | Meaning |
|---|---|
| **[ogsim]** | Needs an injection on the simulator control plane (`$SIM`, account `tester`) |
| **[feeds→sim]** | Works only while `og-feeds` reads `ogsim.market` instead of the live ERCOT/EIA/NWS APIs (see "Market moments") |
| **[root]** | Needs a root shell on the server (the lead) |
| **[JUDGES]** | Part of the 3-minute version for judges: steps 9-10, 19-20 and 24 (plus 7-8 when feeds read the sim) |
| **Known gap** | The release under test does not yet do this; the step says what to show instead |

## Market moments: live feeds or the simulator

The server's `og-feeds` polls the live ERCOT, EIA and NWS APIs (`orchestrator/config/orchestrator.toml`
`[feeds.*] base_url`). A market anomaly injected on `/ogsim/` (price spike, AS price jump, HTTP 5xx, stale
posting) changes only `ogsim.market`, so it reaches the orchestrator only when `og-feeds` is pointed at the
simulator, as `dev/config/dev.toml` does. That switch is configuration plus `systemctl restart og-feeds`
(`deploy/README.md`, "Switching live APIs vs. the market simulator"), and on the server it is the lead's call.

- **Feeds live (the server as deployed):** skip steps 7-8 and 18; every other step works on live market data.
- **Feeds on the simulator (the dev stack, or the server after the switch):** run everything.

## Real / simulated / derived / live

| Data | Kind | Where it comes from |
|---|---|---|
| ERCOT prices per load zone, load, wind, solar, AS prices; EIA; NWS | **Real, live** on the server; **simulated** when feeds read `ogsim.market` | `og-feeds`. The Markets freshness table labels each feed `LIVE`, `SIM` or `HIST` |
| Hub telemetry, SCADA bank load, utility instructions | **Simulated** | `ogsim.fleet`, `ogsim.scada`, autonomous, over MQTT |
| Anomalies: overload, comms loss, energy drain, feed outage | **Simulated, injected on demand** | `ogsim.control` (`integration-sims/scenarios/*.yaml`) |
| Forecasts, selector plans, commitments, grants, guardian verdicts, escalations, alerts, P&L, invoice lines, trace chain | **Derived** | Computed by the orchestrator. Nothing on any screen is LLM-generated |

## URLs, accounts, ids

| Thing | Server | Local dev stack |
|---|---|---|
| Orchestrator UI | `https://base.tocy-net.net/og/` | `http://127.0.0.1:8088/og/` through `dev/scripts/dev_proxy.py` (og-api refuses a browser on :8080) |
| Orchestrator API (`$OG`) | `https://base.tocy-net.net/og/api` | `http://127.0.0.1:8088/og/api` (same proxy) |
| Simulator control plane (`$SIM`) | `https://base.tocy-net.net/ogsim` | `http://127.0.0.1:8091` |

- **Accounts** (passwords are in the lead's credentials file, never in this repo):
  - `og-op-a` and `og-op-b`: named operators. Only these two can complete a safe-stop release: the guardian
    signs a release only for two different accounts on `[guardian] stop_release_authorised_operators`.
  - `operator`: the shared operator account. It can do everything except complete a release (its approval
    stays PENDING, refused as `OPERATOR_NOT_AUTHORISED`).
  - `viewer`: read only; every write panel is hidden.
  - `tester`: the `/ogsim/` control plane.
- Since R3 every state-changing call to the simulator control plane (`POST`/`DELETE` on `$SIM`) needs the
  header `X-OGSim-Request: 1` (a CSRF guard); the `curl` lines below carry it.
- In the `curl` lines, `-u og-op-a:...` stands for that account's credentials behind Apache. On the local
  stack, drop `-u` and go through the dev proxy with `-H 'X-Remote-User: og-op-a'`.
- **Fleet ids (server):** hubs `hub-00000`..`hub-01999`, banks `bank-000`..`bank-039`, zones `LZ_NORTH`,
  `LZ_SOUTH`, `LZ_HOUSTON`, `LZ_WEST`. Hub `i` sits on bank `i mod 40` in zone `i mod 4` (0 NORTH, 1 SOUTH,
  2 HOUSTON, 3 WEST). So LZ_SOUTH is banks `bank-001, -005, ..., -037` (500 hubs).
- **Dev stack:** 200 hubs, 8 banks (`bank-000`..`bank-007`); pick targets from those.

| Moment | How | Target | Screen to watch |
|---|---|---|---|
| Zone prices | live data | all load zones | Markets, Control room ticker |
| Price spike during delivery | [ogsim] [feeds→sim] `demo-01-price-spike-lock` | `np6-905-cd`, RRS | Dispatch, Control room, Profitability |
| AS award held, then deployed | operator action | the seeded ECRS award (contract `...0d03`) | Dispatch |
| SCADA overload | [ogsim] `bank_overload` on the demo bank | `$BANK` | System Health, Dispatch |
| Comms loss, best effort | [ogsim] `demo-03-zone-comms-loss` | `hub-00001`, then zone `LZ_SOUTH` | Fleet, System Health, Dispatch |
| Guardian escalation (K7) | operator API burst (R2 only; skip at R3, see step 15) | `$HUB`, a hub on the demo bank | Control room, System Health, Fleet |
| Degraded mode | [ogsim] [feeds→sim] `stale_posting`, 60 min, started during setup | `np6-905-cd` | System Health, Control room |
| Scoped safe stop and release | operator actions, two operators | `bank-022` | Fleet |
| Energy runs low | [ogsim] `reserve_floor_pressure` on the demo bank's zone | `$ZONE` | Dispatch, System Health |

Scenario anomaly ids are `<scenario>:<type>:<at_s>`, e.g. `demo-02-bank-overload:bank_overload:0`; that is
the id you `DELETE` to end a moment early.

## Before you start (5 minutes, not part of the 15)

1. **Pause random anomalies.** They start unpaused, and each random arrival targets the whole fleet (all hubs,
   all banks or all products):
   `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/random/pause` → `"paused": true`. Pausing does not cancel what is
   already active: list `curl -u tester:... "$SIM/api/anomalies?source=random"` and
   `curl -u tester:... -H 'X-OGSim-Request: 1' -X DELETE $SIM/api/anomalies/<id>` each one, then check `$SIM/api/anomalies` returns
   `{"active": []}`. Since R2 hotfix v3 the pause is saved across a restart of `og-sim-control`. Since R3 an
   unreadable state file keeps random mode paused, but a pause that failed to save is still lost on restart, so
   check `curl -u tester:... $SIM/api/random/status` after one.
2. **All processes up.** Open **System Health**: every process in the Processes table has a heartbeat time from
   the last few seconds (the Status column always reads `ok`, so read the time), no `ALR-PROCESS-DOWN`, no
   degraded-mode banner, and no open critical alert. One `ALR-XFMR-UNMAPPED` warning per commanded bank is
   expected in this release (no hub has a service-transformer mapping yet).
3. **Customers committed: run the demo seed.** `python dev/scripts/seed_demo_customers.py` offers the three
   seeded demo contracts (ERCOT_ENERGY, DIST_DEFERRAL, PARTNER_CAPACITY) for a one-hour window starting at the
   quarter hour after next, through the real admission path, and waits until the selector has committed them.
   It is idempotent: re-running it leaves live obligations alone. On the server it is run by the release manager
   with the lead's OK [root]. The selector decides which banks carry each customer, so the seed ends with a line
   like `demo bank: bank-015 (zone LZ_WEST; carries ERCOT_ENERGY, DIST_DEFERRAL, PARTNER_CAPACITY); a hub on it:
   hub-00015`. Set the three names this run uses:
   ```bash
   BANK=bank-015; ZONE=LZ_WEST; HUB=hub-00015     # from the seed's "demo bank" line
   ```
   The steps below use `$BANK`, `$ZONE` and `$HUB`; the Dispatch pipeline must read at least `3 customers` with
   cards in COMMITTED (then DELIVERING) for the window.

   **For steps 13-14 (demo-03),** the seed's last line names the zone to take dark. Substitution and SHORTFALL
   show only where a committed obligation sits, and the selector, not the seed, picks the banks (it holds kW
   where the zone's forecast price makes it cheapest), so LZ_SOUTH is not guaranteed:
   ```bash
   # e.g. "demo-03 zone: LZ_SOUTH (bank-005 carries DIST_DEFERRAL); a hub on it: hub-00005"
   ZONE3=LZ_SOUTH; HUB3=hub-00005
   ```
   If it reads LZ_SOUTH, run the shipped scenario in step 13. If it names another zone ("no demo customer
   landed in LZ_SOUTH"), either re-run the seed with `--start` on a later quarter hour and check again, or use
   step 13's two `curl` lines on `$HUB3` and `$ZONE3` instead of the scenario (every base zone has 500 hubs).
4. **The AS award.** Dispatch → "AS awards & deployment" lists the seeded ERCOT_AS award (contract
   `00000000-0000-7000-8000-000000000d03`, ECRS since migration `0022`) in state `held`. If the table reads "No
   active ERCOT_AS awards are visible", give that contract an opportunity for the demo window as in item 3.
5. **Three browser windows**: `og-op-a` (the one you present from), `og-op-b` (the second operator, for the
   release), and `viewer` on the Fleet screen.
6. **The control plane.** Use the `curl` lines below. R2 hotfix v3 gave its web page Run, Stop and Stop all
   buttons, and support for a path prefix. But the repo's Apache and systemd config do not set that prefix
   yet, so behind `/ogsim/` the page's calls may still fail.
7. **Only when feeds read the simulator: start step 18's stale price now**, at least 50 minutes before step 18
   (the `curl` is in step 18). Everything already committed keeps delivering, but nothing new is committed once
   the price is 45 minutes old, so run the demo seed (item 3) first.

## The steps

Timing is the budget per step; the total is about 15 minutes.

### Topic 1: live markets, per load zone

**Step 1: The control room is live** (30 s)
- **Action:** Open `/og/` as og-op-a.
- **Show:** Header badge; the one-line fleet story; "Promises kept"; the KPI tiles; the Grid map.
- **Expect:** Header badge `live`. The story line reads "N of M hubs online · n obligations promised to b
  buyers ... **0 promises broken today**". The three "Promises kept" tiles, **Reserve breaches**, **kWh sold
  twice**, **Commitment switches**, all read `0` in green. Fleet power, Active commitments and Today's net
  margin have values (Fleet energy reads `--`).
- **Known gaps:** **Commitment switches** is not measured yet (always 0), and the other two tiles are running
  totals since the checks started, not today's. The tiles' age badges turn "stale" about 10 s after load even
  though four of them keep updating live; reload to reset them.

**Step 2: Prices per load zone** (45 s)
- **Action:** Open `/og/markets`.
- **Show:** "Wholesale price ($/MWh)" (one line per load zone), then "Freshness & source status", then
  "Forecast band (P10 / P50 / P90)".
- **Expect:** One price line per ERCOT load zone; each bank is dispatched and settled at its own zone's price,
  never at a hub price (decision D-10). Freshness rows for `np6-905-cd` (price), `np6-345-cd` (load),
  `np4-732-cd` (wind), `np4-737-cd` (solar), `np4-745-cd` (solar by region, where deployed), `np4-188-cd` (AS), EIA
  and NWS, each `LIVE` (or `SIM` when feeds read the simulator), failures `0`, breaker `closed`. The forecast
  band is drawn ahead of now for `LZ_NORTH`. The "Bid funnel" is derived from the pipeline, so it shows the
  demo offers.

### Topic 2: the fleet

**Step 3: One fleet, many homes** (30 s)
- **Action:** Open `/og/fleet`. Filter zone `$ZONE`, bank `$BANK`. Glance at the viewer window.
- **Show:** The Fleet map and the Hubs table; the operator panels.
- **Expect:** Filtered, exactly 50 hubs (`$HUB` and every 40th hub id after it), `online`, age a few seconds.
  og-op-a sees "Scoped safe stop", "Release a safe stop (two operators)", "Manual command" and "Command the
  selection"; the viewer sees none of them. Unfiltered, the Hubs table shows the first 200 hubs only (a known
  cap of this release; the map draws every hub), so filter by zone or bank.

**Step 4: One hub up close** (30 s)
- **Action:** Click the `$HUB` row (or Enter on it).
- **Show:** "Hub detail": lease epoch, lease expires, last command.
- **Expect:** Lease expiry in the future and renewing (30 s TTL); the last command id changes as the engine
  commands it. Say once: a hub only executes a command signed by the guardian's key for its current lease
  epoch.

### Topic 3: several customers committed at once

**Step 5: The pipeline** (30 s)
- **Action:** Open `/og/dispatch`.
- **Show:** "Opportunity pipeline (N customers, M open ...)".
- **Expect:** `3 customers` or more; cards in **Committed** and **Delivering** with different services, tier and
  kW, each reading "Locked: this promise is kept even if a better price appears (K13)". No card has an amber
  at-risk border. "Latest selector plan" shows "LP optimizer" and an optimal solver status.

**Step 6: The ledger, fleet first** (30 s)
- **Action:** In the header, Ledger `Fleet` → View; then `Feeder segment (bank)` `$BANK` → View.
- **Show:** "Ledger timeline" (stacked committed capacity per obligation, dashed uncommitted capacity on top).
- **Expect:** One band per committed obligation, headroom above. Say: a new opportunity may only take the
  headroom.

### Topic 4: a price spike during delivery, the lock holds [feeds→sim]

**Step 7 [JUDGES]: Inject a $5,000/MWh spike** [ogsim] (30 s)
- **Action:** `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/scenarios/demo-01-price-spike-lock/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
- **Show:** Control room "Market ticker", then Markets.
- **Expect:** `{"ok": true, "scenario": "demo-01-price-spike-lock", "steps": 2}`. Within one feeds poll every
  load-zone price line jumps to `5000` (the simulator spikes the product, all zones at once) and RRS to `800`.

**Step 8 [JUDGES]: The committed customers keep their capacity** (45 s)
- **Action:** `/og/dispatch` (`$BANK` ledger), then `/og/`, then `/og/profitability`.
- **Show:** The Committed/Delivering cards; the **Forgone upside (lock)** tile on Profitability.
- **Expect:** Every committed card keeps its kW and its "Locked ... (K13)" sentence; only L0/L1/L2 overrides
  and infeasibility may ever reduce a commitment, never a price, and the guardian re-checks any such claim
  (G-19). Within a Profitability refresh (30 s) **Forgone upside (lock)** turns non-zero with a caution badge:
  the money the fleet chose not to chase.

### Topic 5: an ERCOT AS award, held then deployed

**Step 9 [JUDGES]: The award is held, not sold** (45 s)
- **Action:** `/og/dispatch`, panel "AS awards & deployment".
- **Show:** The award row: Product, Committed, Energy held, State, Risk.
- **Expect:** State `held`, Risk `OK`. Say: an AS award sits at **0 kW** until ERCOT (here, an operator) deploys
  it; the allocator grants it 0 kW with reason `R-GRANT-AS-HOLD`, keeps its capacity out of the headroom it
  sells, and keeps enough energy above the homes' reserve to run the whole product (ECRS 1 h, Non-Spin 4 h).
  The guardian independently signs a below-commitment hold only when its own reads show an ERCOT_AS award with
  no active deployment and an unused reservation (G-19).
- The Product column reads `ECRS · 1 h hold`; "Energy held" shows only the requirement (`-- / N kWh`).

**Step 10 [JUDGES]: Deploy it** (60 s)
- **Action:** Press **Deploy** on the award row (15 minutes). Read the summary in "Confirm ERCOT AS
  deployment", then **Confirm deployment**.
- **Show:** The result line; the award's State; "Real-time grants & substitutions"; the bank ledger.
- **Expect:** "Deployment `<id>` is active."; State `deployed`. From the next 2 s cycle the award is dispatched
  up to its committed kW like any committed delivery; when the deployment ends, or on **Stop deploy** →
  **Stop deployment**, it returns to a 0 kW hold on the next cycle. Every step is in the trace
  (`AS_DEPLOYMENT`, `AS_DEPLOYMENT_END`), step 23.
- **Known gap:** the form's "Deploy" selector still offers "all held AS awards" and 1-240 min, and og-api
  refuses both a deployment without an award and one longer than the product (ECRS: 60 min), with a raw error.
  Use the row's **Deploy** button.

### Topic 6: a SCADA overload, DIST_DEFERRAL responds, an alert fires

**Step 11: Overload the demo bank** [ogsim] (30 s).
- **Action:** the same anomaly as scenario `demo-02-bank-overload` (which targets `bank-012`), on `$BANK`:
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/inject -H 'Content-Type: application/json' -d '{"type": "bank_overload", "target": "'$BANK'", "params": {"kva_over_rating_pct": 25}, "duration": 300}'`
  (the response carries the anomaly's `id`).
- **Show:** System Health, "Alerts".
- **Expect:** Within a few SCADA cycles an `ALR-SCADA-OVERLOAD` row for `$BANK`: warning above 100% of the
  600 kVA rating, critical above 120%. The simulator reports 25% over the rating (750 kVA, 125%) whatever the
  bank's actual load, so the row lands critical. The same alert appears in the Control room "Open alerts" (reload).

**Step 12: The DIST_DEFERRAL loop responds** (45 s)
- **Action:** `/og/dispatch`, ledger `$BANK`: "Real-time grants & substitutions" and the ledger.
- **Show:** The DIST_DEFERRAL grant on `$BANK`; the committed kW on the cards.
- **Expect:** The DIST_DEFERRAL grant on `$BANK` rises (more discharge to relieve the feeder segment) while
  every committed kW is unchanged. The alert clears when the anomaly ends (300 s), or now:
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X DELETE $SIM/api/anomalies/<id>`.
- **Note:** after 3 overloaded readings the SCADA simulator also issues a LIMIT at 90% of the rating (540 kW) on
  `$BANK`. Since R3 it lifts that LIMIT after 3 readings back under the rating, or after 900 s at most, and
  og-engine releases the cap on the next cycle. The cap does not affect the demo's 40-60 kW obligations.

### Topic 7: comms loss, substitution, then best effort

**Step 13: One hub goes quiet, the bank covers for it** [ogsim] (45 s)
- **Action (the seed's demo-03 zone is LZ_SOUTH):** `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/scenarios/demo-03-zone-comms-loss/run -H 'Content-Type: application/json' -d '{"speed": 1}'`,
  then `/og/fleet?bank=bank-001`. The scenario's hub is `hub-00001` on bank-001, so its substitution shows only if
  the seed's bank is bank-001; for another LZ_SOUTH bank, also run the `hub_offline` line below on `$HUB3` and
  watch that bank. The zone step still gives the SHORTFALL.
- **Action (another zone):** `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/inject -H 'Content-Type: application/json' -d '{"type": "hub_offline", "target": "'$HUB3'", "params": {}, "duration": 300}'`
  now, and 90 s later (step 14)
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/inject -H 'Content-Type: application/json' -d '{"type": "zone_mass_disconnect", "target": "'$ZONE3'", "params": {}, "duration": 240}'`.
  Each response carries the anomaly's `id` to `DELETE`. Read `$ZONE3` for LZ_SOUTH in step 14. Every ogsim
  POST/DELETE needs the `X-OGSim-Request: 1` header (CSRF, since R3).
- **Show:** the hub's bank on `/og/fleet?bank=<bank>`; the hub.
- **Expect:** the hub goes `stale` after 25 s and `offline` after 60 s (`[health] hub_stale_s`, `hub_offline_s`;
  hubs report every 10 s since R3); the other 49 hubs pick up its share, so the obligation on that bank keeps its
  granted kW (substitution: a grant change, never a commitment write).

**Step 14: The whole zone goes quiet; best effort** (60 s)
- **Action:** At +90 s the scenario disconnects all of `LZ_SOUTH`. Open System Health, then `/og/dispatch`.
- **Show:** "Hub health"; "Alerts"; the pipeline cards.
- **Expect:** 500 LZ_SOUTH hubs `offline` about 60 s after the disconnect (so about 150 s after the run starts);
  `ALR-HUB-OFFLINE-RATIO` for LZ_SOUTH, critical (over 20%). A
  delivering obligation on an LZ_SOUTH bank with no substitute left moves to **SHORTFALL** after 60 s
  sustained, with reason `R-COMMIT-LOCK-INFEASIBLE` (on the card at load, and in the trace: stream
  `shortfall-<obligation id>`, class `ALLOCATOR_SHORTFALL`), and its card turns amber (AT_RISK). Say (decision
  D-17): it keeps receiving the maximum feasible kW for the rest of the window, never 0, never stopped. The
  hubs themselves serve their homes on local autonomy once their lease lapses. The zone returns at 240 s; to end now,
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/scenarios/demo-03-zone-comms-loss/stop` (cancels its pending steps and
  ends what it injected).
- **Known gaps:**
  - The card moves to "Fulfilled / shortfall" with the text "Delivered short; penalty applies", although
    delivery continues.
  - The "Commitment-lock events (K13)" table is not populated yet; use the trace.
  - (Fixed in R3: a delivery cut by a utility (L2) instruction now returns to its full commitment on the cycle
    after the instruction expires.)

  The state itself stays SHORTFALL until the window closes.

### Topic 8: the guardian gets cautious (K7)

**Step 15: A burst of vetoed commands** (45 s)
- **Not runnable as written at R3 — skip steps 15 and 16.**
  - Since R3 a confirmed manual command is a ramped operator target (operator guide 6.2). The confirm returns
    `202` `RAMPING`, and the setpoint is not checked against the hub's rating when it is confirmed.
  - So the loop below prints `202`s, not `409`s, and each confirm leaves a 15-minute +60 kW (charging) target
    on `$HUB`.
  - If `$BANK` carries no obligation grant, the guardian refuses every step with G-09 (the gap in step 17). The
    escalation then does appear, but for that reason, and it stays until the targets end. If `$BANK` is
    delivering, the engine ramps `$HUB` toward 60 kW instead.
  - If it was run, cancel the targets: take each `trace_id` from `curl -s -u og-op-a:... "$OG/fleet/manual-targets"`
    and call `curl -u og-op-a:... -X POST "$OG/fleet/manual-targets/<trace_id>/cancel"`.
  - A way to trigger the K7 escalation from genuine vetoes is being routed with the matching e2e test. The text
    below describes R2.
- **Action (R2):** From a shell, send over-limit commands to one hub (60 kW is above every hub's 11 or 20 kW
  inverter), about one per second for 30 s:
  ```bash
  for i in $(seq 1 30); do
    id=$(curl -s -u og-op-a:... -X POST "$OG/fleet/command" -H 'Content-Type: application/json' \
         -d '{"hub_id": "'$HUB'", "p_kw_setpoint": 60, "reason": "K7 demo: over the hub limit"}' | jq -r .proposal_id)
    curl -s -o /dev/null -w '%{http_code} ' -u og-op-a:... -X POST "$OG/fleet/command/$id/confirm"
  done; echo
  ```
- **Show:** The printed status codes; then System Health (reload).
- **Expect:** A row of `409`: the guardian refuses every one, as VETOED or PARTLY_VETOED, with rule ids such as
  G-04 hub ramp, G-26 home meter, G-27 transformer, G-31 peak and G-05 fleet ramp. An occasional TIMEOUT is
  possible. More than 5% of a tick's commands vetoed puts `$BANK` (and its zone, if the zone crosses
  5% too) **CONSERVATIVE**: "Guardian escalations" shows "Scope held conservative" (`ALR-SCOPE-CONSERVATIVE`),
  and the engine stops selling spot headroom there. After three bad ticks it shows "Guardian requests a safe
  stop" (`ALR-SAFE-STOP-REQUESTED`) with **Review safe stop (two-step)**.

**Step 16: A person decides** (30 s)
- **Needs step 15's escalation**, so skip it at R3 (see step 15).
- **Action:** Click **Review safe stop (two-step)**.
- **Show:** The Fleet screen's "Scoped safe stop" panel, prefilled.
- **Expect:** Scope and scope id filled from the guardian's request, reason "Guardian escalation: safe stop
  requested", and the note "Prefilled from the guardian's safe-stop request ... nothing is engaged until you
  confirm". Nothing has stopped: the guardian never engages a stop by itself (K8). Do not propose it; stop
  here. With no more vetoes the escalation clears on its own, after 3 consecutive good ticks (at most 2.5%
  vetoed), or after 30 ticks with no commands (about 60 s).
- **Known gap:** "Guardian escalations" is drawn at page load; reload to see it change.

### Topic 9: a forged command, and the legitimate path

**Step 17: The signed path, for contrast** (45 s)
- **Known gap (R3; seen on the dev stack):** a target moves a hub only while the hub's bank carries an
  obligation's grant in that cycle.
  - On an idle bank the engine proposes the step with ledger version 0, and the guardian refuses it every cycle
    (G-09, stale ledger version). The hub stays where it is.
  - Within seconds the bank and its zone raise `ALR-SAFE-STOP-REQUESTED`.
  - So run this step on `$HUB` (on the demo bank) while one of `$BANK`'s obligations is delivering, or skip it.
  - If the progress bar does not move, cancel the target; the alert clears about 60 s later.
- **Action:** `/og/fleet`, "Manual command": hub `$HUB`, setpoint `0.1`, duration `5`, reason `demo signed path`,
  **Propose (step 1 of 2)**; read the summary; **Send command** within the countdown.
- **Show:** The confirm dialog (focus starts on Cancel, Tab to the confirm button, Escape closes); the result.
- **Expect:**
  - `RAMPING` "Ramping 1 hub to 0.1 kW ... Holds until <+5 min>. Trace ...", with a progress bar.
  - The Fleet table shows `target 0.1 kW` on `$HUB`.
  - Since R3 a manual command is an operator target: every engine cycle moves the hub toward it by at most
    0.9 × its G-04 step, from the hub's last report, so about one step per 10 s report.
  - Each step reached the hub only in a guardian-signed batch.
- **End:** **Cancel target**, or let it expire after 5 minutes.
- **Forged command:** since #43 B3, a hub that rejects a batch with `BAD_SIGNATURE` raises a critical
  `ALR-COMMAND-BAD-SIGNATURE` for its bank (System Health "Alerts"; it clears 300 s after the last rejection).
  But `demo-04-tampered-command` still only registers an active anomaly (the simulator's self-test is not
  invoked), so the scenario alone shows nothing on screen; leave it out of the demo.

### Topic 10: degraded mode [feeds→sim]

**Step 18: A feed goes stale** [ogsim] (30 s, started at least 50 minutes earlier)
- **Why the long lead time:** since R2 the price freshness window is 2,700 s on the server and on the dev stack
  (the simulator stamps prices like ERCOT, at the 15-min interval start). The `feed_outage_and_stale` scenario
  (120 s of 503, then 900 s without new data) no longer reaches it.
- **Action:** during "Before you start", stop the price feed posting for an hour:
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/inject -H 'Content-Type: application/json' -d '{"type": "stale_posting", "target": "np6-905-cd", "params": {}, "duration": 3600}'`
  (the response carries the anomaly's `id`). At step 18, open System Health, then Dispatch.
- **Show:** The banner at the top; "Feed freshness"; "Alerts"; then the Dispatch pipeline.
- **Expect:** "Degraded mode: **Feed stale**" once the price is older than 2,700 s (`ALR-FEED-STALE` "... stale
  for over 2700s", warning). The banner is live on System Health; the Control room shows it on reload.
- **Say:** Feed stale is enforced. While it lasts, og-engine skips intake and every selector gate commits nothing
  new, and the committed deliveries continue. The other modes (Engine down, Guardian down, SCADA silent) are
  shown and recorded only. "Engine down" and "Guardian down" need `systemctl stop` [root] and interrupt
  delivery, so they are not part of this run.
- **End:** `curl -u tester:... -H 'X-OGSim-Request: 1' -X DELETE $SIM/api/anomalies/<id>`. The banner clears once a fresh price posts.

### Topic 11: a scoped safe stop, released by two operators

**Step 19 [JUDGES]: Stop bank-022** (45 s)
- **Action:** As og-op-a, `/og/fleet`, "Scoped safe stop": scope `Bank`, scope id `bank-022`, reason
  `demo: stop bank-022`, **Propose safe stop (step 1 of 2)**; read the summary; **Engage safe stop within 30 s**.
- **Show:** The dialog, then the result; `/og/fleet?bank=bank-022`; Fleet power.
- **Expect:** Summary `Engage safe stop on bank/bank-022 (demo: stop bank-022)`, then `ENGAGED` "Safe stop
  engaged for bank/bank-022." bank-022's hubs drop toward 0 kW and get no new signed command (the guardian
  vetoes batches for a stopped bank); the other banks keep delivering; Reserve breaches stays `0`. Say:
  og-safestop is its own process with its own stop-only key, so this works with engine and guardian down; a
  single message can never stop anything.
- Confirm within **30 s**: the dialog counts down from 30 s and disables **Engage safe stop** at 0 (propose
  again). In the simulator the stopped hubs reach 0 kW over the 4 s stop ramp.

**Step 20 [JUDGES]: Release it, two people** (60 s)
- **Action:** As og-op-a, "Release a safe stop (two operators)": scope `Bank`, scope id `bank-022`, reason
  `demo: release`, **Request release (operator 1)**. Copy the request id. Then, still as og-op-a, paste it into
  "Release request id" → **Review and approve (operator 2)** → **Approve release**. Then do the same as og-op-b.
- **Show:** The three results.
- **Expect:** `REQUESTED` "... Request id `<id>` -- a second operator approves it below within 60 s." Then, for
  og-op-a approving its own request, `REFUSED` "The requesting operator cannot approve their own release." (the
  API answers 403 and the request stays valid). For og-op-b, `RELEASED` "Safe stop released for BANK:bank-022"
  once the guardian has signed the release and og-safestop relayed it; bank-022's hubs resume.
- If the guardian has not signed within og-api's 10 s wait, og-op-b sees `PENDING`; the release usually lands a
  moment later (check bank-022 on Fleet). The same approvals from a shell, with the request id:
  ```bash
  curl -s -u og-op-a:... -X POST "$OG/safestop/release/<id>/approve" -w ' %{http_code}\n'   # 403
  curl -s -u og-op-b:... -X POST "$OG/safestop/release/<id>/approve" -w ' %{http_code}\n'   # 200 released (202 = pending)
  ```

### Topic 12: profitability and billing

**Step 21: Where the money went** (45 s)
- **Action:** `/og/profitability`, Service `All`, today.
- **Show:** The totals tiles (Revenue, Energy cost, Degradation, Penalty, Net margin, Forgone upside (lock));
  "Economics per kW (annualised)"; "Per settled interval"; "LP vs rule baseline".
- **Expect:** Net margin positive; Forgone upside (lock) non-zero if step 8 ran. One row per settled
  obligation-interval; a shortfall from step 14 shows a penalty. "Economics per kW" shows $/kW-in, $/kW-out and
  payback per scope (operators only).
- **Also (since R3):** "LP value added (latest selector gate)" shows the latest gate's value added over the rule
  baseline, with a per-gate trend. It reads "Not available yet: ..." until the optimizer has reported a gate; in
  that case show "LP vs rule baseline".

**Step 22: Invoice lines and M&V** (30 s)
- **Action:** `/og/billing`; set From to today and To to tomorrow, then **Export CSV** (or
  `curl -s -u viewer:... "$OG/billing/invoice-lines?from=<today>&to=<tomorrow>&format=csv"`, dates as `YYYY-MM-DD`).
- **Show:** "Invoice lines"; "M&V performance".
- **Expect:** One line per settled obligation-interval per contract, matching Profitability; the CSV downloads
  with the same lines (To is exclusive of that day, hence tomorrow). M&V shows average compliance and pass
  rate.

### Topic 13: the audit chain

**Step 23: Every decision in the trace** (45 s)
- **Action:** `/og/billing`, "Trace explorer", filters blank (or class `SAFE_STOP_RELEASE`).
- **Show:** Trace rows.
- **Expect:** This run's operator actions: `SAFE_STOP_ENGAGE` on stream `operator_action:og-op-a` (step
  19); `SAFE_STOP_RELEASE` on stream `operator_action:og-op-b`, the approver (its payload records og-op-a as
  requester); the guardian's signed release (`GUARDIAN_VERDICT`); `AS_DEPLOYMENT` from step 10; the manual
  target from step 17 (`MANUAL_TARGET` on stream `operator_action:<operator>`). Nothing from steps 7-8 reduced a
  commitment.

**Step 24 [JUDGES]: Verify the chain** (30 s)
- **Action:** `curl -s -u viewer:... -X POST $OG/trace/verify -H 'Content-Type: application/json' -d '{}'`
- **Expect:** `"passed": true`, `"first_broken": null`, and `checked` = the number of streams verified. Say:
  records are hash-chained per stream, so nothing above could have been edited or removed without this turning
  to `passed: false` with the first broken `stream_id`/`seq`.
- **Or** use the "Run chain verify" button on Billing & audit. Since R3 it also works from an unfiltered page.

### Topic 14: energy runs low

**Step 25: Homes draining toward their reserve** [ogsim] (60 s)
- **Action:** the anomaly of scenario `demo-05-energy-runs-low` (which targets `LZ_NORTH`), on `$ZONE`:
  `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/inject -H 'Content-Type: application/json' -d '{"type": "reserve_floor_pressure", "target": "'$ZONE'", "params": {"home_load_kw": 10}, "duration": 300}'`
  (home load 10 kW on every hub in the zone for 5 minutes).
- **Show:** Dispatch, the `$BANK` cards; System Health, "Alerts"; the Control room "Promises kept".
- **Expect:** The affected cards turn amber (AT_RISK) with a falling "energy margin" and "depletes in";
  `ALR-ENERGY-SHORTFALL-RISK` opens; substitution moves delivery to hubs with energy left; **Reserve breaches
  stays 0** throughout (K1 on energy, not only power). A negative margin held 60 s on a delivering obligation
  escalates it to SHORTFALL, as in step 14. End early with `curl -u tester:... -H 'X-OGSim-Request: 1' -X DELETE $SIM/api/anomalies/<id>`.

## Reset between runs (3 minutes)

1. **End anything still active:** `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/scenarios/stop-all` stops every scenario
   and what it injected; `DELETE` any remaining direct injection (`curl -u tester:... $SIM/api/anomalies`, then
   `DELETE $SIM/api/anomalies/<id>`, such as step 18's stale posting); confirm `{"active": []}`.
2. **Let health clear:** the overload and offline-ratio alerts clear once their condition ends.
3. **Release any stop still engaged** with two operators, as in step 20 (og-op-a requests, og-op-b approves).
4. **End any AS deployment still active:** Dispatch, **Stop deploy**, or
   `curl -u og-op-a:... -X DELETE $OG/dispatch/as-deployments/<deployment_id>`.
5. **Random mode:** leave it paused for another run; `curl -u tester:... -H 'X-OGSim-Request: 1' -X POST $SIM/api/random/resume` to
   return to normal operation.
6. **Fresh commitments:** run the demo seed again (item 3 of "Before you start") for the next window.

## Known gaps

The Known gap lines above are tracked for fixing; the ones that need another owner are listed, with owner, in
`NEEDS_FROM_OTHER_OWNERS.md` next to this file. Re-check them after each release: when a fix lands, the step's
fallback comes out.
