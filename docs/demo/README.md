# OpenGrid Orchestrator: sign-off demo (DEMO-2, release R2)

A presenter runs this start to finish in about 15 minutes against the server or the local dev stack. Every
injected moment is a scenario or anomaly on the simulator control plane (`/ogsim/`), so the run is repeatable.
The operator guide (`docs/operator/README.md`) explains each screen and action in depth; this document only
says what to do, where to look, and what must happen.

What changed since DEMO-1 (R1.6): the two-person safe-stop release is built (steps 19-20), ERCOT AS awards are
held and deployed by an operator (steps 9-10), the guardian escalates on repeated vetoes (steps 15-16), the
degraded-mode banner is on screen (step 18), a lost delivery continues on best effort (step 14), and prices
are per load zone (step 2). Screens were renamed: Health is now **System Health**.

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
| SCADA overload | [ogsim] `demo-02-bank-overload` | `bank-012` | System Health, Dispatch |
| Comms loss, best effort | [ogsim] `demo-03-zone-comms-loss` | `hub-00001`, then zone `LZ_SOUTH` | Fleet, System Health, Dispatch |
| Guardian escalation (K7) | operator API burst | one hub on `bank-012` | Control room, System Health, Fleet |
| Degraded mode | [ogsim] [feeds→sim] `feed_outage_and_stale` | `np6-905-cd` | System Health, Control room |
| Scoped safe stop and release | operator actions, two operators | `bank-022` | Fleet |
| Energy runs low | [ogsim] `demo-05-energy-runs-low` | `LZ_NORTH` | Dispatch, System Health |

Scenario anomaly ids are `<scenario>:<type>:<at_s>`, e.g. `demo-02-bank-overload:bank_overload:0`; that is
the id you `DELETE` to end a moment early.

## Before you start (5 minutes, not part of the 15)

1. **Pause random anomalies.** They start unpaused, and each random arrival targets the whole fleet (all hubs,
   all banks or all products):
   `curl -u tester:... -X POST $SIM/api/random/pause` → `"paused": true`. Pausing does not cancel what is
   already active: list `curl -u tester:... "$SIM/api/anomalies?source=random"` and
   `curl -u tester:... -X DELETE $SIM/api/anomalies/<id>` each one, then check `$SIM/api/anomalies` returns
   `{"active": []}`. A restart of `og-sim-control` un-pauses random mode again; pause it after any restart.
2. **All processes ok.** Open **System Health**. The Processes table shows `feeds, engine, guardian, safestop,
   settle, api` `ok`, no degraded-mode banner, and no open critical alert.
3. **Customers committed.** The Dispatch pipeline header reads at least `3 customers`, with cards in COMMITTED
   or DELIVERING on `bank-012` for the current window (ERCOT_ENERGY, DIST_DEFERRAL and one more). If not, create
   them (contracts first, then opportunities; the selector commits at the next gate):
   ```bash
   curl -u og-op-a:... -X POST $OG/contracts -H 'Content-Type: application/json' -d '{
     "customer_id": "'$(uuidgen)'", "service_type": "ERCOT_ENERGY", "tier": "T2",
     "profile_ref": "demo", "start_at": "2026-09-26T00:00:00Z"}'
   # The window must start at a quarter hour at least ONE FULL GATE ahead: the gate at :00/:15/:30/:45 first
   # expires any offered window that has already started, then selects.
   curl -u og-op-a:... -X POST $OG/opportunities -H 'Content-Type: application/json' -d '{
     "contract_id": "<contract_id>", "window_start": "<quarter hour after next, ISO UTC>",
     "window_end": "<+30 min>", "requested_kw": 40}'
   ```
   A DIST_DEFERRAL contract must exist on bank-012, or step 12 has no PI loop to show.
4. **The AS award.** Dispatch → "AS awards & deployment" lists the seeded ERCOT_AS award (contract
   `00000000-0000-7000-8000-000000000d03`, ECRS since migration `0022`) in state `held`. If the table reads "No
   active ERCOT_AS awards are visible", give that contract an opportunity for the demo window as in item 3.
5. **Three browser windows**: `og-op-a` (the one you present from), `og-op-b` (the second operator, for the
   release), and `viewer` on the Fleet screen.
6. **The control plane.** Use the `curl` lines below. Its web page calls `/api/...` by absolute path, which the
   server's Apache does not proxy under `/ogsim/`, so its buttons may not work there.

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
- **Known gap:** the tiles' age badges turn "stale" about 10 s after load even though Fleet power, Active
  commitments, Reserve breaches and kWh sold twice keep updating live; reload to reset them.

**Step 2: Prices per load zone** (45 s)
- **Action:** Open `/og/markets`.
- **Show:** "Wholesale price ($/MWh)" (one line per load zone), then "Freshness & source status", then
  "Forecast band (P10 / P50 / P90)".
- **Expect:** One price line per ERCOT load zone; each bank is dispatched and settled at its own zone's price,
  never at a hub price (decision D-10). Freshness rows for `np6-905-cd` (price), `np6-345-cd` (load),
  `np4-732-cd` (wind), `np4-737-cd` (solar), `np4-188-cd` (AS), EIA and NWS, each `LIVE` (or `SIM` when feeds
  read the simulator), failures `0`, breaker `closed`. The forecast band is drawn ahead of now for `LZ_NORTH`.
  The "Bid funnel" panel may say "No bid funnel yet" until its API is on the release.

### Topic 2: the fleet

**Step 3: One fleet, many homes** (30 s)
- **Action:** Open `/og/fleet`. Filter zone `LZ_NORTH`, bank `bank-012`. Glance at the viewer window.
- **Show:** The Fleet map and the Hubs table; the operator panels.
- **Expect:** Filtered, exactly 50 hubs (`hub-00012, hub-00052, ..., hub-01972`), `online`, age a few seconds.
  og-op-a sees "Scoped safe stop", "Release a safe stop (two operators)", "Manual command" and "Command the
  selection"; the viewer sees none of them. Unfiltered, the table and map show the first 200 hubs only (a known
  cap of this release), so always filter by zone or bank.

**Step 4: One hub up close** (30 s)
- **Action:** Click the `hub-00012` row (or Enter on it).
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
- **Action:** In the header, Ledger `Fleet` → View; then `Feeder segment (bank)` `bank-012` → View.
- **Show:** "Ledger timeline" (stacked committed capacity per obligation, dashed uncommitted capacity on top);
  "Commitment-lock events (K13)".
- **Expect:** One band per committed obligation, headroom above. Say: a new opportunity may only take the
  headroom.

### Topic 4: a price spike during delivery, the lock holds [feeds→sim]

**Step 7 [JUDGES]: Inject a $5,000/MWh spike** [ogsim] (30 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-01-price-spike-lock/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
- **Show:** Control room "Market ticker", then Markets.
- **Expect:** `{"ok": true, "scenario": "demo-01-price-spike-lock", "steps": 2}`. Within one feeds poll every
  load-zone price line jumps to `5000` (the simulator spikes the product, all zones at once) and RRS to `800`.

**Step 8 [JUDGES]: The committed customers keep their capacity** (45 s)
- **Action:** `/og/dispatch` (bank-012 ledger), then `/og/`, then `/og/profitability`.
- **Show:** The Committed/Delivering cards; "Commitment-lock events (K13)"; **Commitment switches**; the
  **Forgone upside (lock)** tile on Profitability.
- **Expect:** Every committed card keeps its kW and its "Locked ... (K13)" sentence; only L0/L1/L2 overrides and
  infeasibility may ever reduce a commitment, never a price, and the guardian re-checks any such claim (G-19). Within a Profitability
  refresh (30 s) **Forgone upside (lock)** turns non-zero with a caution badge: the money the fleet chose not
  to chase.

### Topic 5: an ERCOT AS award, held then deployed

**Step 9 [JUDGES]: The award is held, not sold** (45 s)
- **Action:** `/og/dispatch`, panel "AS awards & deployment".
- **Show:** The award row: Product, Committed, Energy held, State, Risk.
- **Expect:** State `held`, Risk `OK`. Say: an AS award sits at **0 kW** until ERCOT (here, an operator) deploys
  it; the allocator grants it 0 kW with reason `R-GRANT-AS-HOLD`, keeps its capacity out of the headroom it
  sells, and keeps enough energy above the homes' reserve to run the whole product (ECRS 1 h, Non-Spin 4 h).
  The guardian independently signs a below-commitment hold only when its own reads show an ERCOT_AS award with
  no active deployment and an unused reservation (G-19).
- **Known gap:** the row may read `ERCOT_AS · 4h` for the ECRS award (the API rows do not carry the product
  yet); the energy hold behind it is ECRS's 1 h.

**Step 10 [JUDGES]: Deploy it** (60 s)
- **Action:** Press **Deploy** on the award row (15 minutes). Read the summary in "Confirm ERCOT AS
  deployment", then **Confirm deployment**.
- **Show:** The result line; the award's State; "Real-time grants & substitutions"; the bank ledger.
- **Expect:** "Deployment `<id>` is active."; State `deployed`. From the next 2 s cycle the award is dispatched
  up to its committed kW like any committed delivery; when the deployment ends, or on **Stop deploy** →
  **Stop deployment**, it returns to a 0 kW hold on the next cycle. Every step is in the trace
  (`AS_DEPLOYMENT`, `AS_DEPLOYMENT_END`), step 23.
- **Known gap:** the form's "Deploy" selector also offers "all held AS awards", and the duration bound is one
  global 1-240 min rather than the product's own window; deploy the one award.

### Topic 6: a SCADA overload, DIST_DEFERRAL responds, an alert fires

**Step 11: Overload bank-012** [ogsim] (30 s). **Known gap:** on the dev stack this injection does not change
the SCADA readings yet (tests-e2e finding), so no alert fires there; if none appears on the server, skip to
step 13.
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-02-bank-overload/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
- **Show:** System Health, "Alerts".
- **Expect:** Within a few SCADA cycles an `ALR-SCADA-OVERLOAD` row for bank-012: warning above 100% of the
  600 kVA rating, critical above 120%. The simulator reports the bank's actual load × 1.25, so the percentage
  depends on the load at that moment. The same alert appears in the Control room "Open alerts" (reload).

**Step 12: The DIST_DEFERRAL loop responds** (45 s)
- **Action:** `/og/dispatch`, bank-012: "Real-time grants & substitutions" and the ledger.
- **Show:** The DIST_DEFERRAL grant on bank-012; the committed kW on the cards.
- **Expect:** The DIST_DEFERRAL grant on bank-012 rises (more discharge to relieve the feeder segment) while
  every committed kW is unchanged. The alert clears when the anomaly ends
  (300 s), or now: `curl -u tester:... -X DELETE $SIM/api/anomalies/demo-02-bank-overload:bank_overload:0`.

### Topic 7: comms loss, substitution, then best effort

**Step 13: One hub goes quiet, the bank covers for it** [ogsim] (45 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-03-zone-comms-loss/run -H 'Content-Type: application/json' -d '{"speed": 1}'`,
  then `/og/fleet?bank=bank-001`.
- **Show:** bank-001's 50 hubs; `hub-00001`.
- **Expect:** `hub-00001` goes `stale` after 6 s and `offline` after 30 s; the other 49 hubs pick up its share, so
  the obligation on bank-001 keeps its granted kW (substitution: a grant change, never a commitment write).

**Step 14: The whole zone goes quiet; best effort** (60 s)
- **Action:** At +90 s the scenario disconnects all of `LZ_SOUTH`. Open System Health, then `/og/dispatch`.
- **Show:** "Hub health"; "Alerts"; the pipeline cards and lock events.
- **Expect:** 500 LZ_SOUTH hubs `offline`; `ALR-HUB-OFFLINE-RATIO` for LZ_SOUTH, critical (over 20%). A
  delivering obligation on an LZ_SOUTH bank with no substitute left moves to **SHORTFALL** after 60 s
  sustained, with reason `R-COMMIT-LOCK-INFEASIBLE` (on the card at load, and in the trace: stream
  `shortfall-<obligation id>`, class `ALLOCATOR_SHORTFALL`), and its card turns amber (AT_RISK). Say (decision D-17): it keeps receiving the
  maximum feasible kW for the rest of the window, never 0, never stopped. The hubs themselves serve their homes
  on local autonomy once their lease lapses. The zone returns at 240 s; to end now,
  `curl -u tester:... -X DELETE $SIM/api/anomalies/demo-03-zone-comms-loss:zone_mass_disconnect:90` (and
  `...:hub_offline:0`).
- **Known gaps:** the card moves to "Fulfilled / shortfall" with the text "Delivered short; penalty applies"
  although delivery continues; the "Commitment-lock events (K13)" table is not populated yet (use the trace);
  and restoring the full commitment once capacity returns is an open defect (found with a lifted L2 block: the
  delivery stays at the best-effort remainder). The state itself stays SHORTFALL until the window closes.

### Topic 8: the guardian gets cautious (K7)

**Step 15: A burst of vetoed commands** (45 s)
- **Action:** From a shell, send over-limit commands to one hub (60 kW is above every hub's 11 or 20 kW
  inverter), about one per second for 30 s:
  ```bash
  for i in $(seq 1 30); do
    id=$(curl -s -u og-op-a:... -X POST "$OG/fleet/command" -H 'Content-Type: application/json' \
         -d '{"hub_id": "hub-00012", "p_kw_setpoint": 60, "reason": "K7 demo: over the hub limit"}' | jq -r .proposal_id)
    curl -s -o /dev/null -w '%{http_code} ' -u og-op-a:... -X POST "$OG/fleet/command/$id/confirm"
  done; echo
  ```
- **Show:** The printed status codes; then System Health (reload).
- **Expect:** A row of `409`: the guardian vetoes every one (G-02, hub power). More than 5% of a tick's batches
  vetoed puts bank-012 (and its zone, if the zone crosses 5% too) **CONSERVATIVE**: "Guardian escalations"
  shows "Scope held conservative" (`ALR-SCOPE-CONSERVATIVE`), and the engine stops selling spot headroom there.
  After three consecutive conservative ticks it shows "Guardian requests a safe stop"
  (`ALR-SAFE-STOP-REQUESTED`) with **Review safe stop (two-step)**.

**Step 16: A person decides** (30 s)
- **Action:** Click **Review safe stop (two-step)**.
- **Show:** The Fleet screen's "Scoped safe stop" panel, prefilled.
- **Expect:** Scope and scope id filled from the guardian's request, reason "Guardian escalation: safe stop
  requested", and the note "Prefilled from the guardian's safe-stop request ... nothing is engaged until you
  confirm". Nothing has stopped: the guardian never engages a stop by itself (K8). Do not propose it; stop
  here. With no more vetoes the escalation clears on its own (after 30 idle ticks, about 60 s).
- **Known gap:** "Guardian escalations" is drawn at page load; reload to see it change.

### Topic 9: a forged command, and the legitimate path

**Step 17: The signed path, for contrast** (45 s)
- **Action:** `/og/fleet`, "Manual command": hub `hub-00142`, setpoint `2`, reason `demo signed path`,
  **Propose (step 1 of 2)**; read the summary; **Send command** within the countdown.
- **Show:** The confirm dialog (focus starts on Cancel, Tab to the confirm button, Escape closes); the result.
- **Expect:** `PASS` "Command accepted. Trace ..." (the drill-down's last command id changes), or a guardian
  veto with its rule ids. Either way the setpoint reached the hub only in a guardian-signed batch.
- **Known gap:** in this release a veto renders as `FAILED` with a raw "409 Conflict" text instead of the
  VETOED badge; the 409 in step 15 is the same veto seen from the API.
- **Forged command:** `demo-04-tampered-command` still only registers an active anomaly (the simulator's
  self-test is not invoked), so there is nothing to show on screen; leave it out.

### Topic 10: degraded mode [feeds→sim]

**Step 18: A feed goes down** [ogsim] (30 s, started about 10 minutes earlier)
- **Action:** Start it during step 3:
  `curl -u tester:... -X POST $SIM/api/scenarios/feed_outage_and_stale/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
  (np6-905-cd returns 503 for 120 s, then stops posting new data for 900 s). Now open System Health.
- **Show:** The banner at the top; "Feed freshness"; "Alerts".
- **Expect:** "Degraded mode: **Feed stale**" once the price feed is older than 600 s (`ALR-FEED-STALE`,
  warning), or at once if its breaker opened (`ALR-FEED-LGV-EXHAUSTED`, critical). Markets shows the row's
  failures and breaker. The banner is live on System Health; the Control room shows it on reload.
- **Say:** in this release the four modes (Feed stale, Engine down, Guardian down, SCADA silent) are shown to
  the operator but do not yet change what the engine or selector does; see the operator guide. "Engine down"
  and "Guardian down" need `systemctl stop` [root] and interrupt delivery, so they are not part of this run.

### Topic 11: a scoped safe stop, released by two operators

**Step 19 [JUDGES]: Stop bank-022** (45 s)
- **Action:** As og-op-a, `/og/fleet`, "Scoped safe stop": scope `Bank`, scope id `bank-022`, reason
  `demo: stop bank-022`, **Propose safe stop (step 1 of 2)**; read the summary; **Engage safe stop within 30 s**.
- **Show:** The dialog, then the result; `/og/fleet?bank=bank-022`; Fleet power.
- **Expect:** Summary `Engage safe stop on bank/bank-022 (demo: stop bank-022)`, then `ENGAGED` "Safe stop
  engaged for bank/bank-022." bank-022's hubs drop toward 0 kW and get no new signed command (the guardian
  vetoes batches for a stopped bank); the other banks keep delivering; Reserve breaches stays `0`. Say: og-safestop is its own process with
  its own stop-only key, so this works with engine and guardian down; a single message can never stop
  anything.
- **Known gaps:** confirm within **30 s**. The dialog counts down from 60, but og-safestop drops the proposal
  after 30; a confirm after that shows `TIMEOUT` and nothing stops (propose again). In the simulator a hub
  that was commanded above about half its rating holds a reduced setpoint until its lease lapses (up to about
  35 s) instead of reaching 0 kW over the 4 s stop ramp.

**Step 20 [JUDGES]: Release it, two people** (60 s)
- **Action:** As og-op-a, "Release a safe stop (two operators)": scope `Bank`, scope id `bank-022`, reason
  `demo: release`, **Request release (operator 1)**. Copy the request id. Then, still as og-op-a, paste it into
  "Release request id" → **Review and approve (operator 2)** → **Approve release**. Then do the same as og-op-b.
- **Show:** The three results.
- **Expect:** `REQUESTED` "... Request id `<id>` -- a second operator approves it below within 60 s." Then, for
  og-op-a approving its own request, `REFUSED` "The requesting operator cannot approve their own release." (the
  API answers 403 and the request stays valid). For og-op-b, `RELEASED` "Safe stop released for BANK:bank-022"
  once the guardian has signed the release and og-safestop relayed it; bank-022's hubs resume.
- **Known gap** (until the fix is in): a release that takes the guardian more than 5 s to sign shows `FAILED`
  although it may still land (the screen gives up after 5 s, the API waits 10 s). Fallback, with the request id:
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
  payback per scope, or "The $/kW view is not available on this deployment yet."

**Step 22: Invoice lines and M&V** (30 s)
- **Action:** `/og/billing`; then the CSV:
  `curl -s -u viewer:... "$OG/billing/invoice-lines?from=<today>&to=<tomorrow>&format=csv"` (dates as `YYYY-MM-DD`).
- **Show:** "Invoice lines"; "M&V performance".
- **Expect:** One line per settled obligation-interval per contract, matching Profitability; the CSV downloads
  with the same lines. M&V shows average compliance and pass rate.
- **Known gap:** the **Export CSV** button relays the From/To filter unchanged, and the API needs both as plain
  dates, so with the filter blank or holding a time the export fails; use the `curl` until it is fixed.

### Topic 13: the audit chain

**Step 23: Every decision in the trace** (45 s)
- **Action:** `/og/billing`, "Trace explorer", filters blank (or class `SAFE_STOP_RELEASE`).
- **Show:** Trace rows.
- **Expect:** This run's operator actions: `SAFE_STOP_ENGAGE` on stream `operator_action:og-op-a` (step
  19); `SAFE_STOP_RELEASE` on stream `operator_action:og-op-b`, the approver (its payload records og-op-a as
  requester); the guardian's signed release (`GUARDIAN_VERDICT`); `AS_DEPLOYMENT` from step 10; the manual
  command from step 17. Nothing from steps 7-8 reduced a commitment.

**Step 24 [JUDGES]: Verify the chain** (30 s)
- **Action:** `curl -s -u viewer:... -X POST $OG/trace/verify -H 'Content-Type: application/json' -d '{}'`
- **Expect:** `"passed": true`, `"first_broken": null`, and `checked` = the number of streams verified. Say:
  records are hash-chained per stream, so nothing above could have been edited or removed without this turning
  to `passed: false` with the first broken `stream_id`/`seq`.
- **Known gap:** the "Run chain verify" button on Billing & audit does not forward the viewer's identity in this
  release and reports FAIL; use the `curl` until it is fixed.

### Topic 14: energy runs low

**Step 25: Homes draining toward their reserve** [ogsim] (60 s)
- **Action:** `curl -u tester:... -X POST $SIM/api/scenarios/demo-05-energy-runs-low/run -H 'Content-Type: application/json' -d '{"speed": 1}'`
  (home load 10 kW on every LZ_NORTH hub for 5 minutes).
- **Show:** Dispatch, the bank-012 cards; System Health, "Alerts"; the Control room "Promises kept".
- **Expect:** The affected cards turn amber (AT_RISK) with a falling "energy margin" and "depletes in";
  `ALR-ENERGY-SHORTFALL-RISK` opens; substitution moves delivery to hubs with energy left; **Reserve breaches
  stays 0** throughout (K1 on energy, not only power). A negative margin held 60 s on a delivering obligation
  escalates it to SHORTFALL, as in step 14. End early with
  `curl -u tester:... -X DELETE $SIM/api/anomalies/demo-05-energy-runs-low:reserve_floor_pressure:0`.

## Reset between runs (3 minutes)

1. **End anything still active:** `curl -u tester:... $SIM/api/anomalies`, then `DELETE` each id; confirm
   `{"active": []}`. There is no "stop scenario" verb: `demo-03`'s +90 s step still fires after its first
   step is cancelled; cancel it when it appears, or wait for
   `GET $SIM/api/scenarios/demo-03-zone-comms-loss/status` to read `"running": false`.
2. **Let health clear:** the overload and offline-ratio alerts clear once their condition ends.
3. **Release any stop still engaged** with two operators, as in step 20 (og-op-a requests, og-op-b approves).
4. **End any AS deployment still active:** Dispatch, **Stop deploy**, or
   `curl -u og-op-a:... -X DELETE $OG/dispatch/as-deployments/<deployment_id>`.
5. **Random mode:** leave it paused for another run; `curl -u tester:... -X POST $SIM/api/random/resume` to
   return to normal operation.
6. **Fresh commitments:** create opportunities for the next window so steps 5-8 have COMMITTED cards.

## Known gaps

The Known gap lines above are tracked for fixing; the ones that need another owner are listed, with owner, in
`NEEDS_FROM_OTHER_OWNERS.md` next to this file. Re-check them after each release: when a fix lands, the step's
fallback comes out.
