# OpenGrid Orchestrator: operator guide (release R2)

For the people who watch and act on the orchestrator: the operators on shift and the lead. It covers how to
get in, how to start and stop the system, every screen, every action an operator can take, every alert, and
what the degraded modes mean. Server administration beyond start/stop is in `deploy/RUNBOOK.md`; the
scripted demo is `docs/demo/README.md`; what is not built yet is in
`docs/orchestrator/07-delivery/13-known-limitations.md`.

Two rules run through everything below:

- **Every grid-changing action takes two steps.** Step 1 proposes and shows you the exact summary; nothing
  changes until you confirm it in step 2. The guardian still checks every command at confirm time.
- **The console never assumes success.** Each action reports what actually happened (PASS, VETOED, ENGAGED,
  RELEASED, TIMEOUT, EXPIRED, ...), and every action lands in the audit trace.

"Known gap" marks behaviour of this release that is being fixed; each one says what to do meanwhile.

## 1. Access and roles

| What | Server | Local dev stack |
|---|---|---|
| Operator console | `https://base.tocy-net.net/og/` | `http://127.0.0.1:8088/og/` through `dev/scripts/dev_proxy.py` |
| Orchestrator API (docs at `/og/api/docs`) | `https://base.tocy-net.net/og/api/...` | `http://127.0.0.1:8088/og/api/...` |
| Simulator control plane ("Scenarios" in the nav) | `https://base.tocy-net.net/ogsim/` | `http://127.0.0.1:8091/` |

Accounts (Apache Basic Auth; passwords are in the lead's credentials file, never in the repo):

| Account | Role | What it can do |
|---|---|---|
| `operator` | operator | Everything on the console, except completing a safe-stop release (the guardian signs a release only for the named operators below) |
| `og-op-a`, `og-op-b` | operator | Everything, including the two-person release: one requests, the other approves |
| `viewer` | viewer | Read every screen; no write panel is shown, and every write is refused (403) |
| `og-cust-*` (`dc`, `pipe`, `ercot`, `dist`, `partner`, `pjm`, `mobile`, `largeld`) | customer | The customer API only (`/og/api/customer/`); not the console. **Switched off in this release:** og-api serves the customer API only when `[api.customer_api] enabled = true`, and the shipped config sets `false` |
| `tester` | control plane | The simulator control plane at `/ogsim/` |

How identity works: Apache authenticates you and forwards your account name to og-api together with a
shared proxy secret; og-api believes the name only with that secret. So:

- Opening og-api's own port (`127.0.0.1:8080`) directly shows a banner "... no identity reached the console;
  open it through Apache (production) or the dev proxy". Use the URLs above.
- The footer of the left navigation shows your role ("Role: operator" with a shield, or "Role: viewer").
- An account with no role mapping is treated as a viewer.

## 2. Start, stop and check (server)

Run as root on the server (`deploy/RUNBOOK.md`, "Start / stop"). Stop units by exact name; never `pkill`.

```bash
systemctl start postgresql@17-main mosquitto             # data and broker first
systemctl start opengrid.target ogsim.target             # the 6 orchestrator units and the 4 simulators
systemctl stop ogsim.target opengrid.target              # stop: application first
systemctl restart og-engine                              # one unit
journalctl -u og-engine -f                               # its log
curl -fsS http://127.0.0.1:8080/og/api/health >/dev/null && echo api-ok   # health probe, loopback only
```

| Unit | Target | What it is |
|---|---|---|
| `og-feeds` | `opengrid.target` | Polls ERCOT, EIA and NWS into the feed store; circuit breakers per product |
| `og-engine` | `opengrid.target` | The 2 s real-time allocator and the quarter-hour selector gates; writes every batch's trace pre-image |
| `og-guardian` | `opengrid.target` | Independently checks and signs every command batch; signs safe-stop releases; K7 escalation |
| `og-safestop` | `opengrid.target` | Engages safe stops with its own stop-only key; relays guardian-signed releases. No dependency on engine or guardian |
| `og-settle` | `opengrid.target` | Metering, M&V, P&L, invoice lines; also runs the health evaluator (alerts and degraded modes) |
| `og-api` | `opengrid.target` | The API and this console |
| `og-sim-fleet`, `og-sim-scada`, `og-sim-market`, `og-sim-control` | `ogsim.target` | The simulated hubs, SCADA, market stand-in and the control plane |

- **Caution:** restarting og-engine, og-guardian or an og-sim unit while a firm or AS obligation is DELIVERING
  interrupts it; hubs hold their last setpoint for about 35 s (30 s lease plus 5 s hold) and then fall back to
  serving their own homes.
- **Deploy, rollback, backup, restore:** `deploy/RUNBOOK.md` sections "Deploy a release", "Rollback" and
  "Backup and restore". The deploy script refuses to run while a firm or AS obligation is DELIVERING unless
  given `--during-delivery`, health-checks the new release and rolls back on failure. Nightly backups go to
  `/srv/ogbackup` (the RUNBOOK is authoritative where `deploy/README.md` still names an older path or restore
  procedure).
- **After any start or deploy:** every unit `active`; the console loads for operator and viewer; System Health
  shows no `ALR-PROCESS-DOWN` and every heartbeat time current; the guardian's verdicts are mostly PASS.

## 3. The screens

The left navigation lists eight screens, plus "Scenarios" (the simulator control plane, separate sign-in) and
a theme switch. Every value carries its age: a badge reads `age: 3s` and turns amber with "· stale" when it
passes its threshold. Live screens show a header badge `live`, or `reconnecting · stale` while their stream is
down (the browser reconnects by itself). Screens marked "poll: 30s" re-fetch every 30 s.

| Screen | Path | Refresh | What it is for |
|---|---|---|---|
| Control room | `/og/` | live (2 s) for the power, commitments and invariant tiles; the rest at load | Is everything all right, right now? |
| Fleet | `/og/fleet` | at load | The hubs; safe stop, release, manual and bulk commands |
| Dispatch | `/og/dispatch` | live (2 s) for the pipeline cards; the rest at load | Customers, commitments, the ledger, AS awards |
| Markets | `/og/markets` | poll 30 s | Prices per zone, feeds and their health, forecast |
| System Health | `/og/health` | live (2 s) for the banner, hub health and alerts; processes and feeds at load | Processes, feeds, hubs, alerts, degraded modes |
| Power quality | `/og/pq` | at load | One hub's power quality, calibration, work orders |
| Profitability | `/og/profitability` | poll 30 s | Money: per interval, per contract, per kW |
| Billing & audit | `/og/billing` | at load | Invoice lines, M&V, the trace and its verification |

"At load" panels show what was true when the page loaded; reload to refresh them.

### 3.1 Control room (`/og/`)

- **Degraded-mode banner** and **Guardian escalations** (section 7), when present.
- **Story line:** "N of M hubs online · n obligations promised to b buyers, next window HH:MM · X kW committed
  · d delivering now · 0 promises broken today".
- **Promises kept:** three tiles that must always read `0`: **Reserve breaches** (a home pulled below its
  outage reserve), **kWh sold twice** (a kWh with two buyers), **Commitment switches** (a committed customer
  dropped for a better price). Any non-zero value is red and is backed by the trace.
- **KPIs:** Fleet power (MW), Fleet energy (MWh, shows `--` in this release), Active commitments, Today's net
  margin (USD).
- **Grid map:** transmission lines, ERCOT zone load, utility batteries, our homes coloured by activity
  (delivering, serving its home, charging, idle, fault, offline) and the best sell destination this hour (the
  highest-load zone). Layers are switched top right. Homes without exact coordinates are placed inside their
  load zone.
- **Market ticker:** wholesale price, one line per load zone.
- **Open alerts:** ID, severity, summary, opened; operators acknowledge by id (section 6.8).
- **Known gaps:** **Commitment switches** is not measured yet and always reads 0; **Reserve breaches** and
  **kWh sold twice** are running totals since the invariant checks started, not today's, so "promises broken
  today" is really "ever" (and "kWh sold twice" also rises when hubs lose capacity under future
  reservations). The tiles' age badges turn "stale" about 10 s after load although four of them keep updating;
  the banner, escalations and alert list reflect page load (the control-room stream does not carry the degraded
  modes; System Health's banner and alerts are live).

### 3.2 Fleet (`/og/fleet`)

- **Fleet map:** hubs coloured by health (online, stale, offline, fault). Operators can **Select an area**
  (drag a rectangle; Shift adds) or tick table rows to build a selection for a bulk command; **Clear
  selection** empties it.
- **Filters:** zone, bank, health (`any`, `online`, `stale`, `offline`, `fault`).
- **Hubs** table: hub, bank, zone, health, SoC (kWh), P (kW), age. Click a row (or Enter) for **Hub detail**:
  bank, zone, health, SoC, power, lease epoch, lease expiry, last command.
- Operator panels: **Scoped safe stop**, **Release a safe stop (two operators)**, **Manual command**,
  **Command the selection** (section 6).
- **Known gaps:** the Hubs table shows at most the first 200 hubs by id, so filter by zone or bank on the
  2,000-hub server, and it does not update live (reload). The map draws every hub that matches the Zone and Bank
  filters; it ignores the Health filter.

### 3.3 Dispatch (`/og/dispatch`)

- **Ledger scope** (header): Fleet, a zone, or a feeder segment (bank) → **View**.
- **Opportunity pipeline (N customers, M open ...):** cards in Offered, Selected, Committed, Delivering and
  "Fulfilled / shortfall". Each card: obligation, service · tier · kW, customer, and for committed ones the
  energy margin and time to depletion, plus one sentence on the decision ("Locked: this promise is kept even
  if a better price appears (K13)" for a commitment). An amber border means AT_RISK.
- **AS awards & deployment:** ERCOT ancillary-service awards, held or deployed (section 6.5). The table lists
  every ERCOT_AS obligation, up to 500, in any state: an expired or fulfilled award also reads `held` with a
  **Deploy** button, which og-api refuses ("award is ..., not deployable").
- **Ledger timeline:** committed capacity per obligation stacked over time, with the uncommitted capacity on
  top, for the chosen scope. New opportunities can only take the uncommitted part.
- **Latest selector plan:** mode ("LP optimizer" or "Rule-based fallback"), gate, horizon, solver status, gap,
  solve time, objective.
- **Commitment-lock events (K13):** meant to list every change to a commitment and its reason. The only
  reasons that may ever reduce one are `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2` (device safety, homeowner reserve, a
  utility or ISO instruction) and `R-COMMIT-LOCK-INFEASIBLE`; a price never does. **Known gap:** nothing writes
  these rows yet, so the table stays empty; the lock itself is enforced (the guardian's G-19) and every
  shortfall is in the trace (section 7.3).
- **Real-time grants & substitutions:** the latest grants for the first bank in scope, with kind headroom,
  commitment or substitution (a delivery moved to other hubs).
- **Known gaps:** a card in SHORTFALL sits under "Fulfilled / shortfall" with "Delivered short; penalty
  applies" while it is still delivering on best effort, and after a utility (L2) instruction is lifted og-engine
  keeps applying it (section 7.3).

### 3.4 Markets (`/og/markets`)

- Five series with their latest value: **Wholesale price ($/MWh)** (one line per load zone), **Load (MW)**,
  **Wind (MW)**, **Solar (MW)**, **AS price ($/MW)** (RRS). Each bank is dispatched and settled at its own load
  zone's price (decision D-10), never at a hub price.
- **Forecast band (P10 / P50 / P90)** for `LZ_NORTH`.
- **Bid funnel:** what ERCOT made available, what we submitted, won and had rejected, with reasons. It is derived
  from the opportunity pipeline, so it fills as soon as offers exist; the market-submission part is empty until
  a market adapter reports it.
- **Freshness & source status:** per feed its mode (`LIVE`, `SIM` for the simulator, `HIST` for a replay), age,
  consecutive failures and circuit breaker (`closed`, or `OPEN` in red).

### 3.5 System Health (`/og/health`)

- **Degraded-mode banner** and **Guardian escalations** (section 7).
- **Processes:** each process and the time of its last heartbeat. A missing heartbeat raises
  `ALR-PROCESS-DOWN` (critical) within 15 s. **Known gap:** the Status column reads `ok` even for a stopped
  process, and the table (with its Since time) reflects page load; trust the alert, or reload. The simulators
  are not in this list (`ALR-SIM-OFFLINE` covers them).
- **Feed freshness** (at load): per feed, its quality and age. STALE shows only while the feed's breaker is open;
  a feed that is merely old shows GOOD with a large age, while the banner already says "Feed stale".
- **Hub health:** how many hubs are online, stale, offline or in fault.
- **Cycle latency (p50/p99):** og-engine's rolling-window p50 and p99, read by og-api from its loopback
  `/metrics` (`[health] engine_metrics_url`, `127.0.0.1:9101`). og-engine republishes the window every 60 s, so
  a new point appears about once a minute (none in the first minute after an engine start, or with the URL
  unset); the note under the chart gives the latest p50, p99 and max.
- **Alerts:** live list of open alerts; operators acknowledge by id (section 6.8). Every rule is in section 8.

### 3.6 Power quality & assets (`/og/pq`)

Enter a hub id and **Show**. Without one it opens the hub of the first open work order.

- **Maintenance work orders**, **Waveform summary** (frequency, per-phase V, I, PF, THD, angle), **Bank measured
  PQ**, **Harmonic spectrum**, **Current harmonics, last 15 minutes**, **Raw waveform captures**, **Asset
  health** (state and history) and **Calibration history** (with the guardian's G-25 decision).
- Operator actions: **Request raw capture (step 1 of 2)** and **Request calibration (step 1 of 2)** (section
  6.7).
- A missing measurement is never shown as compliant. A panel is empty only when that hub has no data of that
  kind; with `[assets] drift_enabled = false` there may be no open work order, so enter a hub id.

### 3.7 Profitability (`/og/profitability`)

Filters: customer, contract, service, day (market-local, CT).

- Totals: Revenue, Energy cost, Degradation, Penalty, Net margin, **Forgone upside (lock)** (the value the
  fleet chose not to chase because it kept its commitments).
- **Economics per kW (annualised):** $/kW-in, $/kW-out, net and payback per scope (fleet, regulated market,
  free market, contract), against the 3-year target. Operators only: a viewer sees "Operator role required for
  the $/kW view."
- **LP value added (latest selector gate):** the latest selector gate's value added over the rule baseline
  ("Value added (LP - rule)"), with a trend of one point per gate. Gates overlap in time, so values are shown per
  gate and never summed. Until the optimizer has reported a gate it reads "Not available yet: ...". (Since R3; in
  R2 the panel never loaded.)
- **Net by contract**, **Net by day**, **Per settled interval** (one row per obligation-interval; superseded
  rows struck through and left out of totals) and **LP vs rule baseline**.

### 3.8 Billing & audit (`/og/billing`)

- **Invoice lines** with totals by contract and by line type, filterable by customer, contract and
  obligation; corrections and superseded lines are marked.
- **M&V performance:** average compliance and pass rate.
- **Trace explorer:** every recorded decision (trace id, decision type, class, stream, sequence, reason
  codes), filterable by class and time.
- **Run chain verify:** checks the hash chain of every trace stream (section 6.9).
- **Export CSV** always sends plain dates: a blank From/To means the 1st of this month to today (CT). An export
  includes lines up to the To date only, so set To to tomorrow to include today's lines.
- Since R3, **Run chain verify** also works from an unfiltered page: blank From/To are sent as "no filter".
  Every stream is verified whatever the filter.

### 3.9 Copilot (launcher in the footer, every screen)

The copilot is a question panel. It is **advisory only**: it reads what the console reads, cites its sources,
and cannot command, approve or release anything. Viewers may use it.

**Fleet counts and totals (r3.4.1).** The copilot answers these from the fleet data itself, through the same
filters as the Fleet table (`GET /og/api/fleet/summary`, read-only). The numbers are exact counts, not
estimates. For example:

| Ask | What it counts |
|---|---|
| "how many units have capacity 78.4 kWh" | hubs rated 78.4 kWh (dual-unit homes) |
| "how many hubs are below 30% charge in LZ_NORTH" | hubs in LZ_NORTH at or below 30% SoC, lowest charge listed first |
| "how many trucks are at home" | D-31 trucks within 250 m of their home station (the same rule the guardian's G-35 check uses) |
| "total available kW in LZ_AEN" | rated kW of LZ_AEN hubs that are online or stale on an available bank |
| "how many hubs by zone" / "total available kWh by soc bucket" | a breakdown by zone, availability, health, asset class or 20% SoC band |
| "which units have health issues?" | hubs that are stale, degraded, quarantined, in fault or offline |

It understands rated capacity (kWh) and power (kW) per hub (exact, "over", "under", "between"), charge in
percent, load zone (`LZ_...`), bank, availability (including regulated market with no contract), health,
firmware and hardware version, and asset type (home, dual-unit, substation, truck). **Available kWh** is the
energy above each hub's reserve floor, on hubs that could be dispatched now.

**Reading the line under an answer:**

- "answered from console data · question screened by `<model>`": a model checked the question for intent
  and prompt injection; every number in the answer came from the console's own records.
- "AI-assisted · `<model>`": a model wrote the explanation. Any figure it cites must be in the data it was
  given; if one is not, the explanation is withheld and the console's own answer is shown.
- "answered from console data · no model used": no model is configured or reachable. Fleet counts still work.

**What reaches a model:** only query results (counts, totals, equipment ids, zones, ratings, SoC, health).
Never configuration files, credentials, anything under `/etc/opengrid`, household data or positions. With a
model configured, text that tries to instruct the assistant is refused before any fleet data is read. Model use is capped by the daily budget
shown on System Health (`GET /og/api/ai/status`).

## 4. Commitments, in one page

- A customer's opportunity is **Offered**, then **Selected** by the optimizer at a quarter-hour gate, then
  **Committed**: from then on it is locked (K13). It goes **Delivering** in its window and ends **Fulfilled**
  or **Shortfall**.
- A commitment is never reduced for a better price. Only device safety (L0), the homeowner's reserve (L1), a
  utility or ISO instruction (L2) or proven infeasibility may reduce it, each with its reason in the
  lock-events table and the trace; the guardian re-checks every such claim against its own reads (G-19).
- **Need-basis commitments** (a service profile with measured feedback) may be granted less than their
  reserved maximum when the measured need is lower (reason `R-GRANT-CLOSED-LOOP`); the unused reservation stays
  locked for that customer and is never resold. The guardian checks this (G-19) and settlement pays the full
  reservation when the measured need was met. **Not active in this release:** those contracts' admission is
  off (`[contracts.activation] data_center = false`), and the closed-loop control that grants
  `R-GRANT-CLOSED-LOOP` is off (`[allocator.closed_loop] enabled = false`, `[site_ingest] enabled = false`).
- **AS awards** are held at 0 kW until deployed (section 6.5).
- **Energy counts, not only power:** every 2 s the engine checks that the committed hubs hold enough energy
  above their homes' reserve for the rest of the window. A commitment short of energy turns AT_RISK (amber)
  with `ALR-ENERGY-SHORTFALL-RISK`, and delivery moves to hubs with energy left.

## 5. Safe stop and release (K8), in one page

- **What it does:** a safe stop stops new dispatch for a scope (the fleet, a zone, or a bank). It never forces a
  home below its reserve.
- **Who does it:** og-safestop, a separate process with its own stop-only key. It works with og-engine and
  og-guardian down; it needs og-api (the request), Postgres and Mosquitto.
- **How it reaches the hubs:** a signed, retained stop message per scope. While a bank is stopped the guardian
  signs no command for it, so its hubs get no new setpoint and fall back to serving their homes when their
  lease lapses.
- **Release needs two people.** og-safestop's key can never sign a release. One authorised operator requests it,
  a different authorised operator approves it, and only then does the guardian sign a release that og-safestop
  relays. The authorised operators are `[guardian] stop_release_authorised_operators` (`og-op-a`, `og-op-b`).
- **Why a release can be refused:** before signing, the guardian checks that both people are on that list and
  distinct, that the approval is fresh (at most 300 s), that a stop is actually engaged, that no stop was
  engaged after the approval, that the stop was not utility-initiated, and that no ESTOP or BLOCK is active in
  scope. A refusal leaves the stop engaged; the operators must request and approve again after fixing the
  cause. The reason is in the trace (Billing & audit, class `GUARDIAN_VERDICT`, outcome REFUSED) and in the
  guardian's log.
- **In the simulator:** stopped hubs ramp to 0 kW within about 4 s (the simulator's own `stop_ramp_s`). The
  `[safestop] ramp_bank_s`, `ramp_zone_s` and `ramp_fleet_s` settings are reserved and not read by any code.

## 6. Actions

### 6.1 The two-step pattern

1. Fill the form and press the **(step 1 of 2)** button. Nothing changes; a dialog opens with the exact summary
   the API returned and, for most actions, a countdown "Expires in Ns".
2. Read it. **Cancel** (or Escape) closes it and leaves everything as it was. The confirm button sends it.
3. The result appears under the panel, as a badge and a sentence with the trace id.

Focus starts on **Cancel**; Tab moves to the confirm button. At 0 s the dialog shows "Proposal expired, propose
again." and disables its confirm button. Proposals live in og-api's memory: an og-api restart expires them. A
failure at step 1 shows **UNAVAILABLE** with the reason.

| Action | Where | Step 1 | Confirm | Window |
|---|---|---|---|---|
| Manual command | Fleet, "Manual command" | Propose (step 1 of 2) | Send command | 60 s |
| Bulk command | Fleet, "Command the selection" | Propose for selection (step 1 of 2) | Send to selection | 60 s |
| Safe stop | Fleet, "Scoped safe stop" | Propose safe stop (step 1 of 2) | Engage safe stop | 30 s |
| Release request | Fleet, "Release a safe stop (two operators)" | Request release (operator 1) | none (the request itself) | 60 s for the approval |
| Release approval | same panel | Review and approve (operator 2) | Approve release | until the request expires |
| AS deployment | Dispatch, "AS awards & deployment" | Deploy / Propose deployment | Confirm deployment | none |
| Stop an AS deployment | same panel | Stop deploy | Stop deployment | none |
| Raw waveform capture | Power quality | Request raw capture (step 1 of 2) | Request capture | per the API |
| Calibration | Power quality | Request calibration (step 1 of 2) | Request calibration | per the API |

### 6.2 Manual command (one hub or bank)

Fields: Bank id or Hub id (one is required), Setpoint (kW, + charge / − discharge), Duration (1–240 minutes,
default 15), Reason.

Since R3 a confirmed command is an operator **target**, not a one-shot setpoint:
- It is recorded as a `MANUAL_TARGET` trace event (K10) for the hub, or for every hub of the bank.
- Every 2 s engine cycle then moves each hub toward the target by at most 0.9 × its G-04 ramp step, counted from
  the hub's last reported power. Hubs report every 10 s, so in practice a hub moves about one step per report:
  about 0.11 kW per 10 s for an 11 kW hub and 0.2 kW for a 20 kW hub. A large change takes minutes.
- Each step goes to the hub only in a guardian-signed batch, like any engine setpoint. A refused step leaves the
  hub where it is, so the progress bar stalls; the refusal is recorded as a guardian verdict, not shown in this
  dialog.
- The target holds until its duration ends or you cancel it. Meanwhile those hubs are left out of the engine's own
  allocation.
- In the Fleet table a hub under a live target shows `target X kW`, and the page line counts "N under an operator
  target". The hub drawer shows "Operator target X kW until ... (by ...)".
- The setpoint is not checked against the hub's rating when you confirm. A target beyond the rating ramps until
  the guardian refuses the step (G-02).

| Result | Meaning |
|---|---|
| **RAMPING** | Accepted (HTTP 202). "Ramping N hub(s) to X kW ... Holds until <expiry>. Trace ...". A progress bar follows the hubs' reported power; **Cancel target** ends the target (API: `POST $OG/fleet/manual-targets/<trace_id>/cancel`) |
| **EXPIRED** | The proposal expired (confirm within 60 s), or the hub id is unknown. Propose again |
| **FAILED** | Anything else, with the error |

**Known gap (R3; seen on the dev stack):** a target moves a hub only while the hub's bank carries an obligation's
grant in that cycle.
- On a bank with no grant, the engine proposes the step with ledger version 0, and the guardian refuses it every
  cycle (G-09, stale ledger version). The hub does not move.
- After 3 ticks the bank and its zone raise `ALR-SAFE-STOP-REQUESTED` ("Review safe stop"). The alert stays
  until the target ends; do not engage the stop it offers.
- So command hubs on banks that are delivering, and cancel a target whose progress bar does not move. The
  alert clears about 60 s after the last refused step.

**Known gap (R3):** a confirm that reports **FAILED** while the database is unavailable can still take effect.
The target's trace event is written first, and if the database refuses it, the event is kept in the local trace
journal. The journal is replayed when the database returns, and the engine then ramps to the target until its
original expiry. After a FAILED confirm during a database problem, look for `target X kW` on those hubs in the
Fleet table. To cancel one, take its `trace_id` from `GET $OG/fleet/manual-targets` and call
`POST $OG/fleet/manual-targets/<trace_id>/cancel`.

### 6.3 Scoped safe stop

Fields: Scope (Fleet, Zone, Bank), Scope id (the zone, e.g. `LZ_SOUTH`, or the bank, e.g. `bank-022`; empty for
Fleet), Reason. Summary: "Engage safe stop on bank/bank-022 (reason)".

- **Confirm within 30 s.** The dialog counts down from 30 s (og-safestop's `[safestop] confirm_window_s`) and
  disables **Engage safe stop** at 0. A confirm that still arrives late is refused and shows FAILED; propose
  again.
- Results: **ENGAGED** (red, on purpose) "Safe stop engaged for bank/bank-022. Trace ..."; **TIMEOUT** (no
  confirmation from og-safestop: it is down, or its 30 s passed); **EXPIRED**; **FAILED**.
- A zone or bank stop needs its id; with an empty id og-safestop ignores the request and the confirm times out.

### 6.4 Two-person release

1. **Operator 1** (e.g. og-op-a): Scope, Scope id, Reason → **Request release (operator 1)**. Result
   **REQUESTED**: "Release safe stop on BANK:bank-022 (reason); needs a second operator. Request id `<id>` -- a
   second operator approves it below within 60 s." Nothing is released yet.
2. **Operator 2** (og-op-b, a different person): paste the id into "Release request id" → **Review and approve
   (operator 2)** → **Approve release**.

| Result | Meaning |
|---|---|
| **RELEASED** | The guardian signed the release and og-safestop relayed it: "Safe stop released for BANK:bank-022" |
| **PENDING** | Approved, but the guardian has not signed within og-api's 10 s wait (the screen waits 15 s); it may still, or it refused (section 5). Check the bank on Fleet or the trace |
| **REFUSED** | You requested it yourself: "The requesting operator cannot approve their own release." The request stays valid for the other operator |
| **EXPIRED** | More than 60 s passed, or the id is unknown. Request again |
| **FAILED** | Anything else |

The shared `operator` account can request and approve through the API, but the guardian refuses it
(`OPERATOR_NOT_AUTHORISED`); use og-op-a and og-op-b.

Operator 2 can also approve from a shell:

```bash
curl -s -u og-op-b:... -X POST https://base.tocy-net.net/og/api/safestop/release/<request id>/approve
# 200 {"released": true, ...}; 202 = approved, waiting for the guardian; 403 = you requested it; 410 = expired
```

### 6.5 ERCOT AS awards: hold and deployment

- **Held:** an awarded AS obligation sits at **0 kW** (reason `R-GRANT-AS-HOLD`) until ERCOT deploys it. Its
  capacity is kept out of the headroom the fleet sells, and its homes keep enough energy above reserve to run
  the whole product (ECRS 1 h, Non-Spin 4 h). The guardian signs a held award's 0 kW only when its own reads
  show an ERCOT_AS award with no active deployment and an unused reservation (G-19). A held award short of that
  energy is flagged AT_RISK.
- **The table:** Obligation, Product · hours, Committed kW, Energy held (held / required kWh), State (`held` or
  `deployed`), Risk (`OK` or `AT_RISK`), Action.
- **Deploy:** **Deploy** on the award's row (15 minutes), or the form: pick the award, Duration (min), Reason →
  **Propose deployment**. The dialog "Confirm ERCOT AS deployment" repeats the summary; **Confirm deployment**
  makes it active from now: "Deployment `<id>` is active." While active, the allocator discharges the award up
  to its committed kW like any committed delivery.
- **End early:** **Stop deploy** → **Stop deployment**: "Deployment `<id>` stopped."; the award returns to a 0 kW
  hold on the next cycle. A deployment also ends at its end time.
- Every deploy and stop is traced (`AS_DEPLOYMENT`, `AS_DEPLOYMENT_END`).
- **The Product column** reads `ECRS · 1 h hold` (Non-Spin `· 4 h hold`); an award whose product is unknown shows
  `ERCOT_AS` with no hold. **Energy held** reads `-- / N kWh` (only the requirement is shown).
- **Known gaps:** the Deploy selector still lists "all held AS awards" and the form accepts 1-240 min, but og-api
  refuses a deployment without an award, and one longer than the award's product (ECRS 60 min, Non-Spin
  240 min); the result line then shows a raw 422 or 409 error. Deploy one award at a time, within its product.
  og-api accepts a second deployment of an award that is already deployed, so never chain deployments past the
  product's window: the energy hold covers one product duration.

**Automatic deployments from ERCOT (r3.4.1, D-35).** When `[feeds.ercot_as_poll]` is enabled, og-feeds reads
ERCOT's AS dispatch instructions every 5 s (today from the ogsim MMS simulator) and applies each one exactly
as the Deploy button would: the same checks, the same deployment row (source `ERCOT`), the same hold rules. An
ERCOT recall ends the deployment it names. You don't act on an accepted instruction; it appears in the
award's row as `deployed` and in the trace (stream `ercot_as_poll`, origin `ERCOT_POLL`).

- **Refused instruction** (`ALR-ERCOT-AS-REFUSED`). The summary names the instruction and why: `404`
  no award for that resource and service now; `409` not deployable, already deployed, longer than the
  product, more MW than awarded, two awards match, or arrived too late; `422` unreadable. ERCOT has been told
  REJECT with that reason. Check the award (Dispatch, AS table). If ERCOT really needs the deployment, use
  **Deploy** on the award and record the instruction id in the reason. Acknowledge the alert; it doesn't
  clear on its own.
- **Poll failing or stale** (`ALR-ERCOT-AS-POLL-FAILED`, `ALR-ERCOT-AS-POLL-STALE`). og-feeds cannot read
  instructions, so a deployment could be missed. Check og-feeds and the simulator (`systemctl status
  og-feeds og-sim-market`). Retries run every 5 to 60 s, and both alerts clear on the next good poll.
- **Simulating ERCOT.** On `/ogsim/`, run a scenario `ercot_as_01_ecrs_deploy` … `ercot_as_07_exceed_award`,
  or inject an `ercot_as_*` type with the resource as target. The market sim lists every instruction and our
  answer at `/mms/admin/vdis`. A deployment on a real committed award discharges it: run these only when you
  intend to.
- **Switching it off:** set `[feeds.ercot_as_poll].enabled = false` and restart og-feeds. Active deployments
  run to their end; Stop deploy still works.

### 6.6 Bulk command (a selection of hubs)

Build a selection on the Fleet map (**Select an area**) or with the table's checkboxes, then Setpoint (kW) and
Reason → **Propose for selection (step 1 of 2)** → **Send to selection**.

- When any selected hub is serving a customer, is delivering, is in fault or offline, is under a critical alert
  or is at its reserve, the dialog adds a checkbox "I understand this overrides what these hubs are doing now:
  ...": the confirm button does nothing until you tick it. The API may then ask for a **SECOND CONFIRM**
  ("Confirm again and send to the selection").
- Results since R3: **RAMPING**, meaning one operator target for every selected hub, ramped as in 6.2, with the
  same progress bar and **Cancel target**. Otherwise **EXPIRED** or **FAILED**; the known gap in 6.2 applies to
  FAILED too.
- The guardian checks and signs every hub's every step; a selection never bypasses it. At most 500 hubs per bulk
  command (a larger selection is refused).

### 6.7 Power-quality actions

**Request raw capture** (a waveform capture from one hub; rate-limited) and **Request calibration** (records a
PENDING calibration that the guardian checks under G-25 and then signs or refuses). Results are sentences:
"Capture requested ...", "Calibration ... recorded PENDING; awaiting the guardian's G-25 decision.", "Rate
limited: ...", "Refused: ...".

### 6.8 Acknowledging an alert

Control room "Open alerts" or System Health "Alerts": type the alert's id into "Alert id" → **Acknowledge**.
Operators only. It records who acknowledged ("Alert N acknowledged by og-op-a."); it does **not** clear the
alert. An alert clears by itself when its condition ends.

### 6.9 Verifying the audit chain; exporting invoices

- Every decision (dispatch, verdicts, operator actions, alerts) is hash-chained per stream. Chain verify
  re-checks every stream: `passed: true` and `first_broken: null` mean nothing was edited or removed; a failure
  names the first broken stream and sequence.
- If **Run chain verify** shows FAIL on an unfiltered page (section 3.8), or for the CSV, from a shell:

```bash
curl -s -u viewer:... -X POST https://base.tocy-net.net/og/api/trace/verify -H 'Content-Type: application/json' -d '{}'
curl -s -u viewer:... "https://base.tocy-net.net/og/api/billing/invoice-lines?from=2026-09-26&to=2026-09-27&format=csv" -o invoice_lines.csv
```

### 6.10 Utility grid-control link (r3.4.1, disabled by default)

A utility's EMS (Austin Energy first) can control the fleet directly over DNP3 with mutual TLS. It sends
toll calls (discharge only, at most 90 minutes) and L2 LIMIT/BLOCK, and it reads status back. The full spec
is `docs/orchestrator/07-delivery/integrations/grid-link.md` (D-34). The link is off until the lead and the
owner enable it for a utility.

**What you see when the utility calls**

- The call arrives exactly like a utility call raised from the Dispatch screen: the same obligation, the same
  90-minute cap and the same checks. It raises **ALR-UTILITY-CALL**, and it is listed with origin
  `GRID_LINK` and requester `grid_link:<utility>`.
- A refused call raises **ALR-UTILITY-CALL-REFUSED** with its reason code, for example
  `R-CALL-OUTSIDE-WINDOW`.
- You can **end** an EMS call early from Dispatch the same way as any utility call.
- The utility sees the call's state and reason on its own screen.
- Every inbound command appears in the audit trail on stream `grid_link:<utility>` (`GRID_LINK_COMMAND`,
  origin `GRID_LINK`). Refused connections and controls appear as `AUTHZ_DENY` (`GRID_LINK_DENY`).

**L2 LIMIT/BLOCK from the link** appear as ordinary utility instructions, issued by `UTILITY_GRID_LINK`. They
show on the Fleet page and are enforced by the guardian (G-15), like SCADA instructions.

**Heartbeat lost** (trace `GRID_LINK_STATE` = `HEARTBEAT_LOST`, and an error in og-engine's log):

- The link refuses new calls until heartbeats resume. A call already running continues to its own end.
- L2 limits and blocks stay exactly as the utility last set them. Nothing is lifted automatically.
- Contact the utility's control room.
- If a call must stop, end it from Dispatch. Cancels from the EMS are also still accepted.

**Checks from a shell** (read only):

```bash
journalctl -u og-engine --since -10min | grep -i 'grid link'   # listening, associations, refusals, state changes
```

**Enabling a utility** is a release-manager step, never an operator one. It needs:

1. the signed point list;
2. certificates in `/etc/opengrid/certs/`;
3. MQTT user `og_gridlink` with publish on `og/v1/scada/instruction/#`;
4. a firewall opening for the utility's EMS addresses only;
5. both `enabled` switches in `[grid_link]`, then a restart of og-engine.
### 6.10 Regulated zones with no contract (LZ_LCRA, LZ_RAYBN; D-37)
Hubs and banks in `LZ_LCRA` (bank-050..059) and `LZ_RAYBN` (bank-060..069) carry the badge **"Regulated market – no contract"**
(`og.bank.availability = UNAVAILABLE`, reason `REGULATED_NO_CONTRACT`). The tooltip reads: *Unavailable: regulated
(NOIE) territory, so energy can't be sold into ERCOT, and there is no utility capacity contract to reserve it.
Available once a contract is signed.*
- Nothing is offered, planned or dispatched there, and they are not charged (idle hold). A manual target other than
  0 kW is refused (409) with that reason; the guardian vetoes any non-idle item there (G-33,
  `R-BANK-UNAVAILABLE-REGULATED-NO-CONTRACT`).
- They stay monitored: telemetry, alerts, health, safe stop (a 0 kW hold), firmware and the invariants all work.
- Profitability and the Control room show their capacity on its own line ("Regulated market – no contract: N kW"), never in
  available kW. The Fleet table filter `Availability` lists them.
- ERCOT obligations that were already committed on those banks at the switch complete untouched (K13
  grandfathering); only new commitments are affected. Check with `deploy/scripts/noie_switch_check.sql` (read-only).
- Each utility has a "Sample Contract: ..." listed as **SAMPLE – INACTIVE**: a template, never active.
**Making a zone AVAILABLE once a real contract is signed** (release manager / lead, with the owner's approval):
1. Enter the real contract for the utility (`og.contract`, `market = 'REGULATED'`, `utility_id = 'LCRA'` or
   `'RAYBURN'`, `is_sample = false`, its own name and product rule) and set it `ACTIVE`. Leave the sample SUSPENDED
   (it can never be activated: a CHECK forbids it). Update the `og.utility` terms from the contract.
2. In one transaction, flip that utility's banks:
   `UPDATE og.bank b SET availability = 'AVAILABLE', availability_reason = NULL, availability_since = now()
   FROM og.utility u WHERE u.utility_id = 'LCRA' AND b.zone = ANY (u.territory_zones);`
   (`noie_switch_seed.sql` never marks a zone unavailable again while an ACTIVE non-sample contract exists.)
3. Nothing to restart: og-engine re-reads availability with the market model, the selector every gate, the
   guardian on its topology refresh. Confirm on the Fleet page (badge gone) and at the next gate (the toll is
   reserved in the utility's window). Record it in the decision log.

## 7. Degraded modes and guardian escalation

### 7.1 Degraded modes

The banner "Degraded mode: ..." appears at the top of System Health (live) and the Control room (as of page
load). Several modes can be active together ("Feed stale + Guardian down").

| Banner | Mode | Raised when | Clears when |
|---|---|---|---|
| Feed stale | `NO_NEW_COMMITMENTS` | A feed in `[health] firm_blocking_feeds` is older than its freshness window, has its circuit breaker open, or has no status row at all. Since R3 the default list is the price feed `ERCOT:np6-905-cd` and the AS price feed `ERCOT:np4-188-cd`. Other feeds still raise `ALR-FEED-STALE` but do not block | Every blocking feed is fresh with its breaker closed |
| Engine down | `HOLD_LOCAL_AUTONOMY` | og-engine's heartbeat is missing (more than 15 s) | og-engine heartbeats again |
| Guardian down | `HOLD` | og-guardian's heartbeat is missing | og-guardian heartbeats again |
| SCADA silent | `DIST_DEFERRAL_OPEN_LOOP` | No SCADA bank reading at all for over 60 s (`[health] scada_silent_s`); never before the first reading | A SCADA reading arrives |

- **Feed stale is enforced.** While it lasts, og-engine skips intake (trace `INTAKE_SKIPPED`) and every selector
  gate selects nothing new: offers stay OFFERED, and committed deliveries continue. An unreadable mode counts as
  active. Contract admission (operator CRUD) is not blocked. Only the blocking feeds set it: the price feed since
  R2 hotfix v3, and the AS price feed as well since R3. A stale load, NWS, EIA or solar feed raises
  `ALR-FEED-STALE` only.
- **Known gaps (R3):**
  - The AS price feed posts, and is polled, once a day. A failed or skipped poll is not retried until the next
    day's poll, so one missed poll can keep Feed stale on for up to about a day.
  - A blocking feed with no status row at all (for example on a fresh database) sets Feed stale without raising
    `ALR-FEED-STALE`.
- **The other modes are shown and recorded only.** With the guardian down, the engine's own heartbeat check makes
  it propose no batches (it holds) while it keeps allocating; with the engine down there is nothing to sign.
  Either way the hubs hold their last setpoint for the lease plus hold (about 35 s) and then serve their own
  homes. **Known gap:** "SCADA silent" is raised (`ALR-SCADA-SILENT`) but nothing acts on it: the DIST_DEFERRAL
  loop keeps using the last SCADA reading. The mode is fleet-wide (no SCADA reading from any bank). Since R3 a
  single bank whose readings stop while others keep reporting raises the warning `ALR-SCADA-SILENT-BANK` (same
  60 s); that sets no mode.
- Feed freshness windows (`orchestrator/config/orchestrator.toml` `[feeds.staleness]`): ERCOT price 2,700 s,
  ERCOT load 172,800 s (48 h), wind and solar 10,800 s (including the regional solar feed `np4-745-cd`), NWS
  10,800 s, EIA 10,800 s (counted only while the ERCOT load feed is stale), AS prices 93,600 s (they post once a
  day).

### 7.2 Guardian escalation (K7)

The guardian watches its own veto rate per bank and per zone, every 2 s tick:

1. More than 5% of a scope's commands vetoed in a tick (a bad tick) → the scope goes **CONSERVATIVE**:
   `ALR-SCOPE-CONSERVATIVE`, shown under **Guardian escalations** as "Scope held conservative" with the scope
   ("bank bank-012"). The engine stops selling spot headroom there; committed deliveries continue. Each bad tick
   raises the scope's escalation count by one.
2. An escalation count of 3 → `ALR-SAFE-STOP-REQUESTED`, "Guardian requests a safe stop", plus an unconfirmed
   safe-stop proposal. Operators get **Review safe stop (two-step)**, which opens Fleet's "Scoped safe stop"
   prefilled with the scope and "Guardian escalation: safe stop requested". **The guardian never engages a stop
   itself:** a person reviews the scope and decides whether to propose and confirm.
3. The scope returns to normal, and both alerts clear, after 3 consecutive good ticks (at most 2.5% vetoed), or
   after 30 ticks (about 60 s) with no commands. A tick between 2.5% and 5% neither worsens nor recovers. After
   clearing, the count drops by one per good tick instead of resetting, so a fault that comes back soon is
   close to a stop request again. The guardian's unconfirmed proposal stays behind in the operator-action log;
   it is never acted on by itself. **Known gap:** the posture is held in the guardian's memory, so after an
   og-guardian restart a scope left CONSERVATIVE may stay so (no headroom sold there, both alerts open) until it
   goes conservative and recovers again.

A timeout is not a veto and a veto is not a stop (K7): a guardian that cannot answer in time degrades dispatch;
it never trips the fleet. **Known gap:** Guardian escalations are drawn at page load; reload.

### 7.3 A delivery that falls short (best effort)

When a delivering obligation loses capacity it cannot replace (a zone loses comms, a utility limits a bank, the
homes run low on energy) and the shortfall lasts 60 s, the obligation moves to **SHORTFALL** with the reason in
its reason (`R-COMMIT-LOCK-INFEASIBLE`, or an L0/L1/L2 override) and is flagged AT_RISK (the card's border turns
amber; no alert unless energy is the cause). Decision D-17: it keeps receiving the maximum feasible kW for the
rest of its window, never 0 and never stopped, and the full commitment comes back at the earliest feasible
interval; it settles on what was actually delivered. The state stays SHORTFALL until the window closes. Find it
in the trace: stream `shortfall-<obligation id>` (class `ALLOCATOR_SHORTFALL`) and the obligation's own stream
(`AT_RISK`, `AT_RISK_CLEARED`, the SHORTFALL transition). **Known gaps:** the card shows "Delivered short; penalty
applies" while delivery continues; and after a utility (L2) instruction is lifted, og-engine keeps applying it,
so that bank stays at the best-effort remainder until the window closes (a defect routed for fixing). Watch
"Real-time grants & substitutions", not the card.

## 8. Alerts

Every alert is a row with an id, a rule, a severity (`warning` or `critical`), a summary, and opened and cleared
times. System Health's "Alerts" list is live; the Control room's "Open alerts" reflects page load. The rule id is
not a column: the summary text says which it is. **Acknowledging** records who looked (section 6.8); it never
clears an alert. An alert clears when its condition ends, and only its owner clears it: the health evaluator
(which runs inside og-settle every 5 s), og-engine, og-settle, og-guardian or (for the ERCOT AS poll alerts)
og-feeds. If og-settle is down, no health
alert is raised or cleared and the degraded modes freeze.

| Rule | Severity | Raised when | Clears |
|---|---|---|---|
| `ALR-PROCESS-DOWN` | critical | A process's heartbeat (feeds, engine, guardian, safestop, api) is missing for more than 15 s. "Process {name} heartbeat missing" | When it heartbeats again |
| `ALR-SIM-OFFLINE` | critical | No fleet telemetry and no SCADA reading reached the database for 60 s. The engine writes both, so an og-engine outage also raises it | When readings arrive again |
| `ALR-FEED-STALE` | warning | A feed's latest value is older than its freshness window (section 7.1). "Feed ERCOT:np6-905-cd stale for over 2700s". Only the blocking feeds (price and, since R3, AS price) also set Feed stale | When the feed is fresh again |
| `ALR-SCADA-SILENT` | critical | No SCADA bank reading at all for over 60 s (never before the first reading); shown as "SCADA silent" | og-settle clears it when a reading arrives |
| `ALR-SCADA-SILENT-BANK` | warning | Since R3: one bank's own SCADA readings stopped for over 60 s while other banks may still report. "Bank {bank} SCADA silent for over 60s". Sets no mode | When that bank's reading arrives |
| `ALR-FEED-LGV-EXHAUSTED` | critical | A feed's circuit breaker is open (5 consecutive failures, or half of the last 10 polls) | When the breaker closes |
| `ALR-HUB-OFFLINE-RATIO` | warning / critical | Offline plus fault hubs in a zone exceed 5% (warning) or 20% (critical). A hub is offline after 60 s without telemetry. Since R3 hubs report every 10 s, and System Health counts a hub stale after 20 s (2 × the report interval; the `[health] hub_stale_s = 25` setting is not used) | When the ratio is 5% or less |
| `ALR-SCADA-OVERLOAD` | warning / critical | A bank's latest SCADA apparent power exceeds its kVA rating (warning) or 120% of it (critical) | When the reading is at or under the rating |
| `ALR-COMMAND-BAD-SIGNATURE` | critical | A hub on the bank rejected a command batch as BAD_SIGNATURE (forged or tampered, never applied) in the last 300 s (`[health] command_bad_signature_window_s`). "Bank {bank}: 1 command batch(es) rejected by hub(s) {hub} ..." | 300 s after the last such rejection |
| `ALR-ENERGY-SHORTFALL-RISK` | critical | A committed, delivering or shortfall obligation starting within 15 min lacks the energy above reserve to finish its window (for an AS award: its full product duration). The card turns AT_RISK | og-engine clears it when the obligation is no longer at risk |
| `ALR-RESERVE-BREACH` | critical | The K1 check has recorded any home discharging below its reserve | Only when the count is 0; **known gap:** the count is a running total, so once raised it stays |
| `ALR-CYCLE-P99`, `ALR-CYCLE-P99-APPROACHING` | warning | The engine's 2 s cycle p99 is over its 500 ms budget for 3 reads, or within 80-100% of it. Active: `[health] engine_metrics_url` is set | When it is back under |
| `ALR-GUARDIAN-TIMEOUT-RATE` | critical | More than 1% of guardian verdicts time out. **Known gap:** never raised with the shipped config: `[health] guardian_metrics_url` is not set (og-guardian serves `127.0.0.1:9103/metrics`) | When the rate drops |
| `ALR-SELECTOR-GATE-FAILED` | warning | A selector gate failed, including a solver timeout. "Selector gate {kind} failed (...)"; each failure adds a row | og-engine clears it when the same gate next succeeds |
| `ALR-OBLIGATION-STUCK-SELECTED` | critical | An obligation stayed SELECTED for more than 120 s and could be neither committed nor rejected | When it is no longer stuck |
| `ALR-SETTLE-STALLED` | critical | og-settle's settlement job has not completed for more than 180 s | og-settle clears it when the job succeeds |
| `ALR-TRACE-VERIFY-FAILED` | critical | The 5-minute trace check found a stream whose hash chain does not verify. **Known gap:** a new row appears every 5 minutes while the failure lasts | When a run finds no failure |
| `ALR-SCOPE-CONSERVATIVE` | warning | Guardian escalation: a bank or zone went CONSERVATIVE (section 7.2) | og-guardian clears it when the scope returns to normal |
| `ALR-SAFE-STOP-REQUESTED` | critical | Guardian escalation: three consecutive conservative ticks; a safe stop is requested for a person to review | og-guardian clears it when the scope returns to normal |
| `ALR-CLOCK-QUALITY` | critical | The guardian's clock is more than 200 ms off NTP (G-20). While it lasts, every verdict is TIMEOUT and nothing is signed | og-guardian clears it when the clock is back in limit |
| `ALR-CALIBRATION-BUDGET` | warning | The guardian held a remote calibration (fleet budget, concurrency or suspected systemic drift, G-25). Dormant while `[assets] drift_enabled = false` | No automatic clear |
| `ALR-CALIBRATION-PROTOCOL` | warning | A calibration acknowledgement did not match the issued command, or the hub rejected it. Dormant like the above | No automatic clear |
| `ALR-XFMR-UNMAPPED` | warning | og-guardian checked a batch with a hub that has no service-transformer mapping, so G-27 checks it as a group of one. Every hub today: expect one open row per bank the guardian commands, all with the same summary | No automatic clear |
| `ALR-ERCOT-AS-REFUSED` | warning | Since r3.4.1 (D-35): og-feeds refused an ERCOT AS dispatch instruction (404/409/422 and a reason code) and answered ERCOT REJECT. One row per instruction; a re-delivered duplicate adds none (6.5) | No automatic clear |
| `ALR-ERCOT-AS-POLL-FAILED` | warning | Three ERCOT AS instruction polls in a row failed | og-feeds clears it on the next good poll |
| `ALR-ERCOT-AS-POLL-STALE` | critical | No ERCOT AS instruction poll has succeeded for 60 s: a deployment may be missed | og-feeds clears it on the next good poll |
| `ALR-DELIVERY-RAMP-LATE` | not set yet | **Known gap: never raised in this release** (8.1). Its rule in `opengrid.core.delivery`: a discharge call's measured power has not reached 95% of the called kW 600 s after the call's start (RAMP_TOO_SLOW) | Not defined yet |
| `ALR-DELIVERY-SHORTFALL` | not set yet | **Known gap: never raised** (8.1). Its rule: after reaching that level, measured power stays under it (SUSTAIN_BELOW_TARGET when under 95% of the measured intervals hold it; the policy's `shortfall_alert_s` is 60 s) | Not defined yet |
| `ALR-DELIVERY-NONE` | not set yet | **Known gap: never raised** (8.1). Its rule: the call is commanded but the hubs deliver at most 5% of the called kW (NO_DELIVERY; the policy's `none_alert_s` is 60 s) | Not defined yet |
| `ALR-DELIVERY-METER-MISMATCH` | not set yet | **Known gap: never raised** (8.1). Its rule: over the call, an independent meter's energy differs from the batteries' telemetry by more than 10%, floored at 25 kW (UNCORROBORATED) | Not defined yet |
| `ALR-TRACE-VERDICT-WRITE-FAILED` | warning | A verdict's `GUARDIAN_VERDICT` trace row could not be written (the verdict stands; the audit row is missing) | No automatic clear |

An open alert keeps the severity it opened with: a zone that goes from 6% to 25% offline stays a warning until
it clears and re-opens.

### 8.1 Delivery alerts (D-38)

A grant is what the allocator planned, not what the homes delivered: a grant the guardian vetoes still counts as
granted. Decision D-38 judges a discharge call (a utility toll call, an AS deployment) on the power the hubs
actually reported, their telemetry, with one set of rules in `opengrid.core.delivery`.

**Known gap: in this release no delivery alert is raised.** The rules exist and the end-to-end tests use them,
but no running process applies them yet. So:

- the four `ALR-DELIVERY-*` rules in the table above never open;
- a call's status reports granted kW and kWh with `delivery_measured: false` and `delivery_state: "UNMEASURED"`,
  never RAMPING or DELIVERING;
- the grid link still serves CALL_DELIVERED_KW (AI 5) with COMM_LOST (grid link, 6.10).

**The rules** (the `DeliveryPolicy` defaults), for a call of X kW:

- **Reach:** measured discharge reaches 95% of X within 600 s (10 minutes) of the call's start. Hubs ramp, so a
  large step takes minutes (6.2).
- **Sustain:** from then on (from the 10-minute mark if it never got there), at least 95% of the measured
  intervals stay at or above that level.
- **Nothing delivered:** the average measured discharge is at most 5% of X.
- **Energy:** under half of the called kWh fails the call.
- **Stale:** an interval without telemetry is unmeasured, never counted as 0; more than 10% of them marks the
  result stale.
- **Meter:** where an independent meter covers the banks, its energy over the call must be within 10% of the
  batteries' (floored at 25 kW).
- The check only observes. A shortfall never changes dispatch (K7), a firm call keeps its best effort (7.3), and
  settlement bills measured energy.

**What to do meanwhile:** check delivered power yourself while a call runs.

1. Dispatch: set the **Ledger scope** to one of the call's banks; **Real-time grants & substitutions** then lists
   its grants. That is the plan, not the power.
2. Fleet: filter the Hubs table to those banks and read **P (kW)**. Hubs report every 10 s, and their power
   includes any other grant on those banks.
3. **Late** (the RAMP-LATE rule): 10 minutes after the start, those hubs still discharge under 95% of the called
   kW. **Short** (SHORTFALL): they reached it, then fell back under it. **None** (NONE): they barely move. Open a
   stalled hub's **Hub detail** for its health and last command; a refused step is recorded as a guardian verdict
   (6.2). Look for a safe stop, or a utility L2 limit or block on the bank (7.3). A shortfall is not a reason for a
   safe stop (K7: a measured shortfall never stops a firm obligation).
4. **Meter disagrees** (METER-MISMATCH): no screen shows it yet.
5. Tell the lead about any call that is late, short or dead, with its call id and banks.

## 9. Known gaps in this release

| Gap | What to do meanwhile |
|---|---|
| A manual target on a bank with no obligation grant never moves the hub (G-09) and raises `ALR-SAFE-STOP-REQUESTED` for the bank and zone (6.2) | Command hubs on delivering banks; cancel a target that does not move; do not engage the offered stop |
| A manual or bulk confirm that shows FAILED during a database problem can still take effect later (6.2) | Look for `target X kW` on those hubs in the Fleet table; cancel the target (6.2) |
| Fleet table does not update live (since R3.1 it pages through the whole fleet: choose Rows per page) | Reload |
| A hub's age badge turns "stale" 10 s after its last report in the Fleet table (6 s in the hub drill-down), although hubs report every 10 s | Read the Health column or System Health's hub counts instead: stale after 20 s, offline after 60 s |
| AS deploy form: one award at a time since R3, but for an award whose product has no duration it still offers up to 240 min, which og-api refuses (409) | Deploy one award, within its product's window; og-api refuses a second deployment while one is active |
| Only Feed stale (the price and AS price feeds) is enforced; SCADA silent is shown only | Treat the other banners as a call to act (7.1) |
| The AS price feed is polled once a day; one missed poll can keep Feed stale on for up to about a day | Check `ALR-FEED-STALE` for `ERCOT:np4-188-cd`; tell the lead |
| Escalations, Control-room banner and alerts reflect page load | Reload; System Health's banner and alerts are live |
| Best-effort shortfall is labelled "Delivered short" | Watch the grants, not the card |
| Delivery is not measured yet (D-38): no `ALR-DELIVERY-*` alert opens, and a call's status shows granted kW (`delivery_measured: false`) | Read the serving hubs' reported power during a call (8.1) |
| One permanent `ALR-XFMR-UNMAPPED` warning per bank | Expected until transformers are mapped; acknowledge |
