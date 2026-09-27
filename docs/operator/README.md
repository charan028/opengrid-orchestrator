# OpenGrid Orchestrator: operator guide (release r3.4.3)

Current for r3.4.3 (2026-09-27). Sections marked r3.4.1, r3.4.2 or r3.4.3 describe what changed in that release.

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
| Operator console | `https://base.tocy-net.net/og/` | `http://localhost:8088/og/` through `dev/scripts/dev_proxy.py` |
| Orchestrator API (docs at `/og/api/docs`) | `https://base.tocy-net.net/og/api/...` | `http://localhost:8088/og/api/...` |
| Simulator control plane ("Scenarios" in the nav) | `https://base.tocy-net.net/ogsim/` | `http://localhost:8091/` |

Accounts (Apache Basic Auth; passwords are in the lead's credentials file, never in the repo):

| Account | Role | What it can do |
|---|---|---|
| `operator` | operator | Everything on the console, except completing a safe-stop release (the guardian signs a release only for the named operators below) |
| `og-op-a`, `og-op-b` | operator | Everything, including the two-person release: one requests, the other approves |
| `viewer` | viewer | Read every screen; no write panel is shown, and every write is refused (403) |
| `og-cust-*` (`dc`, `pipe`, `ercot`, `dist`, `partner`, `pjm`, `mobile`, `largeld`) | customer | The customer API only (`/og/api/customer/`); not the console. Enabled since r3.4.1 (`[api.customer_api] enabled = true`, D-33); each account sees only its own customer's data |
| `og-util-aen` | utility | Since r3.4.1 (D-33): the utility API only (`/og/api/customer/v1/utility/`, section 6.11): Austin Energy's own toll calls (`[api.roles.utility]`, `[api.utility_api] enabled_utilities`). It reads and calls only its own tolling obligations; anything else is 404 and an `AUTHZ_DENY` trace row. Not the console. LCRA and Rayburn are mapped but not enabled and have no account (D-37) |
| `tester` | control plane | The simulator control plane at `/ogsim/` |

How identity works: Apache authenticates you and forwards your account name to og-api together with a
shared proxy secret; og-api believes the name only with that secret. So:

- Opening og-api's own port (`localhost:8080`) directly shows a banner "... no identity reached the console;
  open it through Apache (production) or the dev proxy". Use the URLs above.
- The footer of the left navigation shows your role ("Role: operator" with a shield, or "Role: viewer").
- An account with no role mapping is treated as a viewer.

## 2. Start, stop and check (server)

Run as root on the server (`deploy/RUNBOOK.md`, "Start / stop"). Stop units by exact name; never `pkill`.

```bash
systemctl start postgresql@17-main mosquitto             # data and broker first
systemctl start opengrid.target ogsim.target             # the 6 orchestrator units and the 5 simulators
systemctl stop ogsim.target opengrid.target              # stop: application first
systemctl restart og-engine                              # one unit
journalctl -u og-engine -f                               # its log
curl -fsS http://localhost:8080/og/api/health >/dev/null && echo api-ok   # health probe, loopback only
```

| Unit | Target | What it is |
|---|---|---|
| `og-feeds` | `opengrid.target` | Polls ERCOT, EIA and NWS into the feed store; circuit breakers per product. Since r3.4.2 it also runs the ERCOT AS instruction poller when `[feeds.ercot_as_poll]` is enabled (off in the repo config; 6.5) |
| `og-engine` | `opengrid.target` | The 2 s real-time allocator and the quarter-hour selector gates; writes every batch's trace pre-image. Hosts the utility grid-control link when it is enabled (6.10; off by default) |
| `og-guardian` | `opengrid.target` | Independently checks and signs every command batch; signs safe-stop releases; K7 escalation |
| `og-safestop` | `opengrid.target` | Engages safe stops with its own stop-only key; relays guardian-signed releases. No dependency on engine or guardian. Since r3.4.2 it serves `/metrics` on loopback port 9106 |
| `og-settle` | `opengrid.target` | Metering, M&V, P&L, invoice lines; runs the health evaluator (alerts and degraded modes); since r3.4.3 also the delivery-verification job (D-38, 6.13) |
| `og-api` | `opengrid.target` | The API and this console |
| `og-lifecycle.timer` / `og-lifecycle.service` | none | Data lifecycle every 10 minutes (partitions, rollups, exports, retention). Installed by the deploy but not enabled until the lead enables it (`deploy/RUNBOOK.md`, "Data lifecycle") |
| `og-sim-fleet`, `og-sim-scada`, `og-sim-market`, `og-sim-control`, `og-sim-utility` | `ogsim.target` | The simulated hubs, SCADA, market stand-in, the control plane, and (r3.4.3) the Austin Energy utility EMS simulator (`ogsim.utility_aen`), which places toll calls through the utility API or the grid link |

- **Caution:** restarting og-engine, og-guardian or an og-sim unit while a firm or AS obligation is DELIVERING
  interrupts it; hubs hold their last setpoint for about 35 s (30 s lease plus 5 s hold) and then fall back to
  serving their own homes.
- **Deploy, rollback, backup, restore:** `deploy/RUNBOOK.md` sections "Deploy a release", "Rollback" and
  "Backup and restore". The deploy script refuses to run while a firm or AS obligation is DELIVERING unless
  given `--during-delivery`, health-checks the new release and rolls back on failure. Nightly backups go to
  `/srv/ogbackup` (the RUNBOOK is authoritative where `deploy/README.md` still names an older path or restore
  procedure). Since r3.4.1 the deploy also checks every og-* unit and a fresh heartbeat after the restart, and
  rolls back when one fails.
- **After any start or deploy:** every unit `active`; the console loads for operator and viewer; System Health
  shows no `ALR-PROCESS-DOWN` and every heartbeat time current; the guardian's verdicts are mostly PASS.

## 3. The screens

The left navigation lists eight screens, plus "Scenarios" (the simulator control plane, separate sign-in) and
a theme switch. Every value carries its age: a badge reads `age: 3s` and turns amber with "· stale" when it
passes its threshold; a value stamped in the future (day-ahead prices, forecasts) reads `ahead: 2h` instead,
also in System Health's server-rendered Feed freshness table. Live screens show a header badge `live`, or `reconnecting · stale` while their stream is
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
- **Regulated capacity (r3.4.2, D-37):** the zone summary shows the capacity of the regulated zones with no
  contract on its own line ("Regulated market – no contract: N kW"), never inside available kW (6.12).
- **Market ticker:** wholesale price, one line per load zone.
- **Open alerts:** grouped by rule and scope, columns Severity, Rule, Scope, Summary, Count, Latest; filters
  Severity, Rule and Per page; operators acknowledge with **Ack** or in bulk (section 6.8).
- **Known gaps:** **Commitment switches** is not measured yet and always reads 0; **Reserve breaches** and
  **kWh sold twice** are running totals since the invariant checks started, not today's, so "promises broken
  today" is really "ever" (and "kWh sold twice" also rises when hubs lose capacity under future
  reservations). The tiles' age badges turn "stale" about 10 s after load although four of them keep updating;
  the banner, escalations and alert list reflect page load (the control-room stream does not carry the degraded
  modes; System Health's banner and alerts are live).

### 3.2 Fleet (`/og/fleet`)

- **Fleet map:** hubs coloured by health (online, stale, offline, fault). Hubs on an unavailable bank (a
  regulated zone with no contract, D-37) are drawn hollow with a dashed outline; the legend names them.
  Operators can **Select an area** (drag a rectangle; Shift adds) or tick table rows to build a selection for a
  bulk command; **Clear selection** empties it.
- **Filters:** zone, bank, health (`any`, `online`, `stale`, `offline`, `fault`) and, since r3.4.2,
  **Availability** (it lists the regulated-no-contract hubs; the choice is kept in the URL).
- **Hubs** table: hub, bank, zone, health, SoC (kWh), P (kW), age. A hub on an unavailable bank carries the
  badge **"Regulated market – no contract"** with a tooltip (6.12). Click a row (or Enter) for **Hub detail**:
  bank, zone, health, SoC, power, lease epoch, lease expiry, last command, availability, and the device's own
  report ("Reported by battery", with its time) when the hub has sent one.
- **Health thresholds (r3.4.1):** the Health column, the age badges, the map and System Health's hub counts
  all use the same `[health]` settings: a hub is **stale** after `hub_stale_s` (25 s) without telemetry and
  **offline** after `hub_offline_s` (60 s). Hubs report every 10 s, so a healthy hub never shows stale
  between reports.
- **Trucks (D-31, r3.4.2):** a truck's position is the position the truck itself last reported, not its seeded
  home station. A report older than 5 minutes (300 s), missing or stamped in the future means the position is
  unknown, and unknown counts as away. A truck away from its home station (more than 250 m) is never charged:
  the selector plans no charging for it and the guardian vetoes a charge (G-35). Trucks and the 20 MW
  substation assets are planned and checked at their nameplate rating.
- **Device reports:** a reported rating or location that differs from the seed raises
  `ALR-DEVICE-RATING-MISMATCH` for that hub; the seed is never changed by a report. Decide with the lead whether
  the seed is wrong.
- Operator panels: **Scoped safe stop**, **Release a safe stop (two operators)**, **Manual command**,
  **Command the selection** (section 6).
- **Known gaps:** in the Hubs table only P and the telemetry age refresh (every 10 s); reload for SoC, Activity
  and Health. It pages through the whole fleet (Rows 25 or 50). The map draws every hub that matches the Zone and Bank filters; it ignores the Health filter.

### 3.3 Dispatch (`/og/dispatch`)

- **Ledger scope** (header): Fleet, a zone, or a feeder segment (bank) → **View**.
- **Opportunity pipeline (N customers, M open ...):** cards in Offered, Selected, Committed, Delivering and
  "Fulfilled / shortfall". Each card: obligation, service · tier · kW, customer, and for committed ones the
  energy margin and time to depletion, plus one sentence on the decision ("Locked: this promise is kept even
  if a better price appears (K13)" for a commitment). An amber border means AT_RISK (since r3.4.3 also when
  the measured delivery of a running call is short, 6.13).
- **AS awards & deployment:** ERCOT ancillary-service awards and utility tolling obligations (D-29), held or
  deployed (sections 6.5 and 6.10). A deployed row says who started it: "by OPERATOR", "by ERCOT" (the AS
  poller), "by UTILITY" (the utility API) or "by GRID_LINK", with the requester and kW. A tolling row sits in
  the same table: Product reads `TOLLING` (no hold hours), Energy held "--", and its button reads
  **Utility call...** instead of **Deploy...**; in the form's Deploy list it reads "TOLLING (utility call)".
  Neither button opens a dialog: it selects the row in the form under the table (Deploy, Duration, Reason →
  **Propose deployment**, section 6.5). The table is not live: reload to see a new deployment. Only COMMITTED,
  DELIVERING and SHORTFALL obligations are listed; a SHORTFALL award can still read `held` with a button, which
  og-api refuses ("not deployable").
- **Delivery column and drawer (D-38, r3.4.3):** each deployed row of "AS awards & deployment" shows its
  measured delivery in the **Delivery** column: the result badge (`IN_PROGRESS`, `PASS`, `PARTIAL`, `FAIL`), a
  `METER` badge when the independent meter disagrees with battery telemetry, and "delivered / committed kW"
  (discharge shown positive). "pending" means no record yet; a row that is not deployed shows "--". The column
  refreshes every 15 s. Click the value to open the **Measured delivery** drawer: Result (with reasons),
  Committed, Time to target ("N s (ramp M s)" or "not reached"), Sustained, Lowest ("N kW for M s"), Energy,
  Meter, and a chart of Committed, Commanded, Delivered
  and Meter change kW over the call; **Close** closes it. Manual discharge targets are not in this table; read
  them through the API (6.13).
- **Ledger timeline:** committed capacity per obligation stacked over time, with the uncommitted capacity on
  top, for the chosen scope. New opportunities can only take the uncommitted part.
- **Latest selector plan:** mode ("MILP optimizer" for the mixed-integer linear program (MILP), plan mode L-DA
  or L-ID, or "Rule-based fallback"), gate, horizon, solver status, gap, solve time, objective.
- **Commitment-lock events (K13):** meant to list every change to a commitment and its reason. The only
  reasons that may ever reduce one are `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2` (device safety, homeowner reserve, a
  utility or ISO instruction) and `R-COMMIT-LOCK-INFEASIBLE`; a price never does. **Known gap:** nothing writes
  these rows yet, so the table stays empty; the lock itself is enforced (the guardian's G-19) and every
  shortfall is in the trace (section 7.3).
- **Real-time grants & substitutions:** the latest grants for the first bank in scope, with kind headroom,
  commitment or substitution (a delivery moved to other hubs).
- **Known gaps:** a card in SHORTFALL sits under "Fulfilled / shortfall" with "Delivered short; penalty
  applies" while it is still delivering on best effort, and after a utility (L2) instruction is lifted og-engine
  keeps applying it (section 7.3). Measured delivery shows only for deployed AS/toll rows (Delivery column);
  manual discharge targets and the per-contract summary are API only (6.13).

### 3.4 Markets (`/og/markets`)

- Five series with their latest value: **Wholesale price ($/MWh)** (one line per load zone), **Load (MW)**,
  **Wind (MW)**, **Solar (MW)**, **AS price ($/MW)** (RRS). Each bank is dispatched and settled at its own load
  zone's price (decision D-10), never at a hub price.
- **Forecast band (P10 / P50 / P90)** for `LZ_NORTH`.
- **Offer funnel (simulated):** last 24 h, stages "Made available by ERCOT", "Selected by optimizer (not sent to
  ERCOT)", "Committed (simulated)" and "Rejected", each with count and % of available; a table per product
  (Product, Available, Selected (not sent), Committed, Rejected, Commit rate (%) = committed / selected); and
  "Why offers were rejected" with counts. Nothing is sent to ERCOT: these are internal stages, not bids or
  awards. It is derived from the opportunity pipeline, so it fills as soon as offers exist; until then it reads
  "No offer funnel yet: ...".
- **Freshness & source status:** per feed its mode (`LIVE`, `SIM` for the simulator, `HIST` for a replay), age
  (a live badge; a forecast stamped in the future reads "ahead: ..."), consecutive failures and circuit breaker
  (`closed`, or `OPEN` in red).

### 3.5 System Health (`/og/health`)

- **Degraded-mode banner** and **Guardian escalations** (section 7).
- **Processes:** each process and the time of its last heartbeat. A missing heartbeat raises
  `ALR-PROCESS-DOWN` (critical) within 15 s. **Known gap:** the Status column reads `ok` even for a stopped
  process, and the table (with its Since time) reflects page load; trust the alert, or reload. The simulators
  are not in this list (`ALR-SIM-OFFLINE` covers them).
- **Feed freshness** (at load; reload to refresh): per feed, its quality and age ("34m", "1h 34m"). Quality is
  STALE while the feed's breaker is open or once its latest value is older than the product's
  `[feeds.staleness]` threshold, the same rule the feed readers use. A feed whose latest value is stamped in
  the future (day-ahead prices, forecasts) reads "ahead: 3h 10m", like the live age badges, and is never stale.
- **Hub health:** how many hubs are online, stale, offline or in fault.
- **Cycle latency (p50/p99):** og-engine's rolling-window p50 and p99, read by og-api from its loopback
  `/metrics` (`[health] engine_metrics_url`, `localhost:9101`). og-engine republishes the window every 60 s, so
  a new point appears about once a minute (none in the first minute after an engine start, or with the URL
  unset); the note under the chart gives the latest p50, p99 and max.
- **Alerts:** the same grouped alerts panel as the Control room; operators acknowledge with **Ack** or in bulk
  (section 6.8). Every rule is in section 8.

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
- **Economics per kW (annualised):** columns Scope, kW, In $/kW-yr, Out $/kW-yr, Net $/kW-yr; rows Fleet,
  "Regulated market (AE / CPS)", "Free market (ERCOT competitive)", one per contract, and "Illustrative home unit
  (reference) (Assumed)". Caption: "Net = Out - In - wear - O&M; the O&M allowance is (Assumed) · from N h of this
  month · method". Current settlement month; no payback or target column is shown. Viewers and operators both
  see it; the filters do not apply to it.
- **MILP value added (latest selector gate):** the latest selector gate's value added over the rule baseline:
  tiles "Value added (MILP - rule)", "MILP net value", "Rule baseline net" and "Forgone upside (lock)", a
  breakdown list, and a trend of one point per gate. Gates overlap in time, so values are shown per gate and
  never summed. Until the optimizer has reported a gate it reads "Not available yet: ...". (Since R3; in R2 the
  panel never loaded.)
- **Net by contract**, **Net by day**, **Per settled interval** (one row per obligation-interval; superseded
  rows struck through and left out of totals) and **MILP vs rule baseline** (bars "MILP net value" and "Rule
  baseline").
- **ERCOT AS capacity is settled at the cleared MCPC (r3.4.3).** An ERCOT_AS award's capacity revenue uses
  ERCOT's day-ahead clearing price for that product and delivery hour (`np4-188-cd`), not the value stored with
  the offer. Only when no MCPC observation exists does it fall back to the offer's value; the settlement trace
  records the price used and its flag (`MCPC` or `OPPORTUNITY_PRICE`). Energy is still settled at the bank's
  zone price (D-10).
- **Regulated sample contracts (D-37):** each regulated utility's "Sample Contract: ..." is labelled
  **SAMPLE – INACTIVE**; it never settles anything. Regulated-no-contract capacity is shown on its own line,
  never in available kW (6.12).

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

**Fleet counts and totals (r3.4.2).** The copilot answers these from the fleet data itself, through the same
filters as the Fleet table (`GET /og/api/fleet/summary`, read-only). The numbers are exact counts, not
estimates. For example:

| Ask | What it counts |
|---|---|
| "how many units have capacity 78.4 kWh" | hubs rated 78.4 kWh (dual-unit homes) |
| "how many hubs are below 30% charge in LZ_NORTH" | hubs in LZ_NORTH at or below 30% SoC, lowest charge listed first |
| "how many trucks are at home" | D-31 trucks whose fresh device-reported position is within 250 m of their home station (the same rule the guardian's G-35 check uses; 3.2) |
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
- **Firmware updates are a device exclusion:** a hub a firmware campaign has in flight is left out of dispatch
  and its share moves to other hubs. A shortfall it causes carries `R-COMMIT-LOCK-OVERRIDE-L0`, and since
  r3.4.3 the guardian's own read counts those hubs as unavailable, so the claim corroborates (G-19).
- **Measured delivery (r3.4.3, D-38):** besides the energy check, og-settle compares what each discharge call
  actually delivered with what was committed. A running call measured short turns AT_RISK too
  (`R-DELIVERY-MEASURED-SHORTFALL`) and raises a delivery alert (6.13). This only observes; it never changes a
  command.
- **Regulated zones with no contract (D-37):** nothing new is offered, planned or committed there (6.12).

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
| **NOT RECORDED** | og-api answered 503: the target could not be written to the trace. Read the sentence after the badge: "manual target not recorded ... nothing will ramp -- retry" means exactly that (since r3.4.1 it can never take effect later); "manual target outcome unknown ... it may be live" means the write failed and the re-check failed too, so check `GET $OG/fleet/manual-targets` (or `target X kW` in the Fleet table) before proposing again |
| **EXPIRED** | The proposal expired (confirm within 60 s), or the hub id is unknown. Propose again |
| **FAILED** | Anything else, with the error |
| (409) | Since r3.4.2 a target other than 0 kW on a hub of a regulated zone with no contract is refused with that reason (6.12) |

**Known gap (R3; seen on the dev stack):** a target moves a hub only while the hub's bank carries an obligation's
grant in that cycle.
- On a bank with no grant, the engine proposes the step with ledger version 0, and the guardian refuses it every
  cycle (G-09, stale ledger version). The hub does not move.
- After 3 ticks the bank and its zone raise `ALR-SAFE-STOP-REQUESTED` ("Review safe stop"). The alert stays
  until the target ends; do not engage the stop it offers.
- So command hubs on banks that are delivering, and cancel a target whose progress bar does not move. The
  alert clears about 60 s after the last refused step.

**Fixed in r3.4.1: "not recorded" is final.** Manual targets are written straight to the database on their own
trace stream (`manual_target:<operator>`), never to the local trace journal, so a target the API reported as
not recorded cannot appear later through a journal replay or an engine restart. When the answer to a confirm
is lost, og-api looks the target up by the request id: found means RAMPING (202), absent means "not recorded",
and a failed lookup means "outcome unknown" (never a false "not recorded"). If only the operator-action audit
row fails after the target is written, the answer is still RAMPING, with a warning. Single confirm, bulk confirm
and **Cancel target** all work this way; a cancel that reports NOT RECORDED leaves the target running, so press
**Retry cancel**. To cancel from a shell, take the `trace_id` from `GET $OG/fleet/manual-targets` and call
`POST $OG/fleet/manual-targets/<trace_id>/cancel`.

**Known gap (r3.4.3):** the confirm result shows the badge **NOT RECORDED** for both 503 answers, with the
sentence "nothing is ramping"; only the API's detail after it says "outcome unknown ... it may be live". Read the
detail, and check the target list before proposing again.

Trace rows the database refuses for their content (not for an outage) are never journaled: since r3.4.1 they go
to a quarantine file next to the journal and raise `ALR-TRACE-QUARANTINED` (section 8), and the journal replay
continues past them.

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
  `deployed`), Risk (`OK` or `AT_RISK`), Delivery, Action.
- **Deploy:** **Deploy...** on the award's row selects it in the form under the table (no dialog), or pick it in
  the form's **Deploy** list; set **Duration** (min, default 15; the hint reads "Up to 60 min for ECRS.") and
  **Reason** (prefilled "Operator ERCOT AS deployment") → **Propose deployment**. The dialog "Confirm ERCOT AS
  deployment" reads "Deploy award `<id>` (`<product>`) for N minutes (`<reason>`)" and "This is step 1 of 2. No
  award changes state until you confirm."; **Confirm deployment** makes it active from now: "Deployment `<id>`
  is active." While active, the allocator discharges the award up to its committed kW like any committed
  delivery. Reload to see the row turn `deployed`.
- **Utility call on a tolling row:** the same form and routes. **Utility call...** selects the row; Duration up
  to 90 min ("Up to 90 min for TOLLING."); the dialog keeps the title "Confirm ERCOT AS deployment" and reads
  "Issue the utility's call on tolling obligation `<id>` for N minutes (`<reason>`)".
- **End early:** **Stop deploy** → dialog "Confirm stop deployment" ("Stop active ERCOT_AS deployment `<id>`
  early", the same wording for a toll) → **Stop deployment**: "Deployment `<id>` stopped."; the award returns to
  a 0 kW hold on the next cycle. A deployment also ends at its end time.
- Every deploy and stop is traced before it takes effect, on stream `dispatch_call:<requester>` (decision type
  `OPERATOR_ACTION`): `DISPATCH_CALL` for an accepted deployment, `DISPATCH_CALL_REFUSED` for a refused one, and
  `DISPATCH_CALL_END` when it is stopped or shortened. Since r3.4.1 each deployment is also a row in the call
  ledger (`og.dispatch_call`).
- **The Product column** reads `ECRS · 1 h hold` (Non-Spin `· 4 h hold`); an award whose product is unknown shows
  `ERCOT_AS` with no hold, and a tolling row `TOLLING`. **Energy held** reads "N kWh required" (with "(margin
  +N kWh)" when the margin is known) while only the requirement is known, "held / required kWh" when both are,
  and "--" for a tolling row.
- **One path for every deployment (r3.4.1, D-33):** the Deploy button, a utility's call (utility API or grid
  link) and an ERCOT instruction all go through the same core call function with the same checks: the product's
  maximum duration, discharge only, at most the committed kW, inside the reservation window, never overlapping
  an active deployment of the same award, and per-requester rate limits (`[dispatch.calls]`, 30 per hour and
  200 per day). A refusal names its reason code.
- **Known gaps:** the Duration field is capped at the selected award's product length, but an award whose
  product has no known duration still offers up to 240 min, which og-api refuses; the result line then reads
  "Refused by the API: `<reason code>`: `<detail>`". Deploy one award at a time, within its product.

**Automatic deployments from ERCOT (r3.4.2, D-35).** Off in the repo config (`enabled = false`); the release
manager enables it (`deploy/RUNBOOK.md`). When `[feeds.ercot_as_poll]` is enabled, og-feeds reads
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
- **Instruction could not be processed** (`ALR-ERCOT-AS-PROCESSING-FAILED`, r3.4.3). An error while applying one instruction; it is retried every poll and the others still run. Check `journalctl -u og-feeds` and the database; tell the lead if it persists.
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
  same progress bar and **Cancel target**. Otherwise **NOT RECORDED** (503, read as in 6.2), **EXPIRED** or
  **FAILED**. Hubs of a regulated zone with no contract cannot take a non-zero target (6.12).
- The guardian checks and signs every hub's every step; a selection never bypasses it. At most 500 hubs per bulk
  command (a larger selection is refused).

### 6.7 Power-quality actions

**Request raw capture** (a waveform capture from one hub; rate-limited) and **Request calibration** (records a
PENDING calibration that the guardian checks under G-25 and then signs or refuses). Results are sentences:
"Capture requested ...", "Calibration ... recorded PENDING; awaiting the guardian's G-25 decision.", "Rate
limited: ...", "Refused: ...".

### 6.8 Acknowledging an alert

Control room "Open alerts" or System Health "Alerts" (or the notification bell): **Ack** on a grouped row, or
tick rows (**Select all on page**, **Select all N matching filter**) → **Acknowledge selected (N)** and confirm.
Operators only. It records who acknowledged (result "ACKED N alerts acknowledged."; the row then reads "acked");
it does **not** clear the alert. An alert clears by itself when its condition ends.

### 6.9 Verifying the audit chain; exporting invoices

- Every decision (dispatch, verdicts, operator actions, alerts) is hash-chained per stream. Chain verify
  re-checks every stream: `passed: true` and `first_broken: null` mean nothing was edited or removed; a failure
  names the first broken stream and sequence.
- If **Run chain verify** shows FAIL on an unfiltered page (section 3.8), or for the CSV, from a shell:

```bash
curl -s -u viewer:... -X POST https://base.tocy-net.net/og/api/trace/verify -H 'Content-Type: application/json' -d '{}'
curl -s -u viewer:... "https://base.tocy-net.net/og/api/billing/invoice-lines?from=2026-09-26&to=2026-09-27&format=csv" -o invoice_lines.csv
```

### 6.10 Utility calls: utility API (D-33) and grid-control link (D-34)

**Utility API calls (r3.4.1, D-33), in brief.** Austin Energy (account `og-util-aen`) can call its own tolling
obligation (D-29) through the utility API: discharge only, at most 90 minutes, inside the contract's tolling
window, never overlapping an active call. The utility API itself (requests, status, errors, operator playbook)
is section 6.11; this list is only what the console shows for any utility call.

- An accepted call starts a deployment on the tolling row of **AS awards & deployment** ("by UTILITY" and the
  requester), raises `ALR-UTILITY-CALL` (severity `info`) and is traced on stream `dispatch_call:<requester>`
  (`DISPATCH_CALL`). The engine caps the obligation at the called kW.
- A refused call raises `ALR-UTILITY-CALL-REFUSED` (warning) with its reason code, for example
  `R-CALL-OUTSIDE-WINDOW`; nothing is deployed. A call outside the utility's own scope is 404 and an
  `AUTHZ_DENY` trace row.
- A cancel or shortening by the utility adds another `ALR-UTILITY-CALL` row. You can end any call early with
  **Stop deploy**. A call can be shortened, never extended.
- **Call status is measured (r3.4.3):** the utility sees RAMPING or DELIVERING from the delivery og-settle
  measured (`delivered_kw`, `delivered_kwh`, `delivery_state`, `meter_status`), not from what was granted.
  Measured shortfalls raise the delivery alerts (6.13).
- **How the toll ramps (r3.4.3):** a 20 MW call on a substation asset ramps over minutes. Each 2 s cycle og-engine
  steps the asset from its last setpoint the guardian **signed** (while that batch's lease is live), else from its
  reported power, by at most 0.9 x its G-04 ramp for one cycle. A vetoed step, a lapsed lease or a safe stop
  drops that anchor, so the ramp restarts from telemetry instead of stalling on vetoes. No batch is proposed for a
  bank under an engaged safe stop (fleet, zone or bank); after the release the asset is held at 0 kW until
  telemetry newer than the release arrives, then ramps again. The guardian applies the same anchor (G-04, one
  cycle's bound) and reloads its signed anchors when og-guardian restarts.
- `GET $OG/dispatch/calls` lists the calls of every origin (read-only).
- **The simulator (r3.4.3):** `og-sim-utility` (`ogsim.utility_aen`) places Austin Energy calls on hot-day peaks
  and on demand from the five `utility-aen-*` scenarios on `/ogsim/`. Its calls are real calls on the tolling
  obligation: they discharge the fleet. LCRA and Rayburn are disabled in it.

**Utility grid-control link (r3.4.1, disabled by default, D-34).**
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
- L2 limits and blocks stay exactly as the utility last set them. Nothing is lifted automatically. Since r3.4.3
  they also survive an og-engine restart (og-engine restores them from the link's trace).
- Contact the utility's control room.
- If a call must stop, end it from Dispatch. Cancels from the EMS are also still accepted.

**Checks from a shell** (read only):

```bash
journalctl -u og-engine --since -10min | grep -i 'grid link'   # listening, associations, refusals, state changes
```

**Loopback test enablement (r3.4.3):** the release manager runs
`sudo deploy/scripts/grid_link_enable_loopback.sh --apply --test-call` (a dry run without `--apply`). This turns
on AUSTIN_ENERGY on localhost only (port 20001), with test certificates, through an og-engine drop-in and
`/etc/opengrid/grid_link.toml`, and places one 5-minute test call through the Austin Energy simulator, cancelled
at once. Outside the tolling window the call is refused (for example `R-CALL-OUTSIDE-WINDOW`), which still proves
the link end to end; expect `ALR-UTILITY-CALL-REFUSED` then. Switch it off with `--disable --apply`.

**Enabling a utility** is a release-manager step, never an operator one. It needs:

1. the signed point list;
2. certificates in `/etc/opengrid/certs/`;
3. MQTT user `og_gridlink` with publish on `og/v1/scada/instruction/#`;
4. a firewall opening for the utility's EMS addresses only;
5. both `enabled` switches in `[grid_link]`, then a restart of og-engine.

og-engine starts the link at startup (`start_grid_link` in its main) when `[grid_link].enabled` and at least one
utility entry's `enabled` are true; otherwise nothing listens. A link failure never stops the engine.

### 6.11 Utility API (Austin Energy toll calls, r3.4.1, D-33)

Austin Energy issues its own toll calls through og-api's utility API, `/og/api/customer/v1/utility/`. The full
reference is `docs/api/utility-api.md`. It signs in as `og-util-aen`: `[api.roles.utility]` maps that account to
`AUSTIN_ENERGY`, and `[api.utility_api] enabled_utilities` lists that utility. The simulator `ogsim.utility_aen`
can stand in for Austin Energy's system.

**What the utility can see and do**

- It sees its own tolling obligations (by default today's and tomorrow's 16:30-18:00 CT windows) and each one's
  active call.
- It can issue a call, now or later in the window:
  - discharge only: negative kW, at most the committed kW;
  - at most 90 minutes, starting and ending inside the obligation's window;
  - never overlapping another call on the same obligation.
- Every call carries an idempotency key. Resending the same request with the same key returns the original call
  and deploys nothing new; the same key with a different request is refused (409).
- It can read the state of every call on its toll, and cancel or shorten the calls it issued. It can never extend a
  call. From r3.4.3 it also reads its calls' measured delivery (`/og/api/customer/v1/utility/delivery-records`).
- It reaches nothing else:
  - another utility's obligations and calls answer 404 (never 403, so their existence is not revealed);
  - Apache admits the account only under `/og/api/customer/`;
  - og-api refuses it (403) on every operator and viewer endpoint.
- Reads are scoped to the utility, not the account. A call you raise on the toll from Dispatch is recorded against
  `AUSTIN_ENERGY` and appears in the utility's call history. From r3.4.3 only you can end it: the utility gets
  403 `R-CALL-NOT-ISSUER`, traced as `AUTHZ_DENY` (in r3.4.2 it could cancel it too).

**How a call appears to you**

- **ALR-UTILITY-CALL** (info) for each accepted call, for example "UTILITY call from og-util-aen: -20000 kW for
  60 min from 2026-09-27T21:30+00:00". It fires again when the utility cancels or shortens the call.
- **ALR-UTILITY-CALL-REFUSED** (warning) with the reason code, for example `R-CALL-OUTSIDE-WINDOW`. While one is
  open and unacknowledged, further refusals from the same account add no new alert. Acknowledge it to see the next.
- **Dispatch, AS awards & deployment:** the toll's row reads `deployed`, "by UTILITY · og-util-aen · -N kW". This
  starts when the call is accepted (also for a call scheduled later in the window) and lasts until the call ends.
  **Stop deploy** ends it early, as for any deployment.
- **From a shell:**
  - the call ledger, refusals included: `GET /og/api/dispatch/calls?utility_id=AUSTIN_ENERGY`;
  - the active deployments: `GET /og/api/dispatch/as-deployments` (`source` `UTILITY`, `requested_by`
    `og-util-aen`).
- **Audit:**
  - trace stream `dispatch_call:og-util-aen` (`DISPATCH_CALL`, `DISPATCH_CALL_REFUSED`, `DISPATCH_CALL_END`);
  - an operator-action row per accepted call (`UTILITY_CALL:<obligation>`, by `og-util-aen`);
  - `AUTHZ_DENY` on `authz_deny:og-util-aen` when the account reaches for another utility's data.

**States the utility sees** (r3.4.3; measured delivery, D-38)

| State | Meaning |
|---|---|
| `ACCEPTED` | Accepted, not started yet |
| `ACTIVE` | Running; its delivery is not measured yet, or the latest measurement had no telemetry |
| `RAMPING` | Running; measured discharge below `ramping_fraction` (0.9) of the call's kW |
| `DELIVERING` | Running; measured discharge at or above that share |
| `COMPLETED` | Ended, or cancelled |
| `REFUSED` | Never deployed; the reason code says why |

og-settle's delivery job measures each call from telemetry in 30 s buckets. The first measurement comes about a
minute into the call, so a call starts `ACTIVE`; `delivery_measured` turns true only once a measured value (or a
final verdict) exists. The status carries the measured `delivered_kw`/`delivered_kwh` and the verification
(`delivery_state` `IN_PROGRESS`, then `PASS`, `PARTIAL` or `FAIL`, with reasons). Its `granted_kw`/`granted_kwh`
are planned/granted values, never metered: they are deprecated and removed in r3.5 (`granted_description`:
"planned; removed in r3.5; use delivered_*").

- **Operator view (r3.4.3):** the same measurement is in the Dispatch **Delivery** column and drawer, and at
  `GET /og/api/delivery/records/{deployment_id}`. `delivery_measured` turns `true` only once a delivered kW or a
  final verdict exists. A slow
  or short delivery raises `ALR-DELIVERY-RAMP-LATE`, `ALR-DELIVERY-SHORTFALL` or `ALR-DELIVERY-NONE`.
- **In r3.4.2** nothing is measured. A running call is always `ACTIVE`, with `delivery_measured: false` and
  `delivery_state: "UNMEASURED"`; only the granted fields exist; `ramping_fraction` is unused. The utility may
  also cancel calls it did not issue, and cancelling an ended call answers 409 `R-CALL-CANNOT-EXTEND` (r3.4.3:
  `R-CALL-ALREADY-ENDED`). If the utility questions a delivery there, compare Dispatch's "Real-time grants &
  substitutions" with the trace; don't rely on the call's state.

**Limits** (`[dispatch.calls]`)

- The limits are per account and apply to every origin, operators included: `max_calls_per_hour = 30` and
  `max_calls_per_day = 200`, over rolling windows.
- Accepted and refused calls count; a resend of the same idempotency key does not.
- Over the limit the call is refused with 429 `R-CALL-RATE-LIMIT`, and **ALR-UTILITY-CALL-REFUSED** is raised.
- The utility's calls never use up an operator's budget.

**Switching it off** (release manager):

- Remove `AUSTIN_ENERGY` from `[api.utility_api] enabled_utilities`: every utility request then answers 403.
- Or set `[api.customer_api] enabled = false`: every customer and utility route answers 404.
- Then restart og-api. Calls already accepted run to their end, and Stop deploy still works.

### 6.12 Regulated zones with no contract (LZ_LCRA, LZ_RAYBN; D-37, r3.4.2)

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
- LCRA and Rayburn have no utility-API account and their grid-link entries stay disabled, so they cannot call.

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
   reserved in the utility's window). Record it in the decision log. Enabling the utility's API account or
   grid link is a separate release-manager step (6.10).

### 6.13 Delivery verification (r3.4.3, D-38)

og-settle checks every discharge call against what the hubs actually delivered: a utility toll call, an ERCOT
AS deployment (operator, ERCOT poll, utility or grid link) and a manual discharge target. Every 15 s
(`[delivery] interval_s`) it builds 30 s buckets of committed kW, kW commanded in guardian-signed batches, kW
delivered from telemetry and, on independently metered banks (the substation assets, plus
`[delivery] meter_bank_ids`), the meter reading. When a call ends it writes a final record to
`og.delivery_record` with a result, **PASS**, **PARTIAL** or **FAIL**, and reasons (`RAMP_TOO_SLOW`,
`SUSTAIN_BELOW_TARGET`, `NO_DELIVERY`, `VETOED`, `STOPPED`, `DATA_STALE`, `ENERGY_SHORT`), and a meter status
(`CORROBORATED`, `UNCORROBORATED`, `NO_METER`, `METER_STALE`). The final record is traced
(`DELIVERY_VERIFICATION`). This is observation only (K7): it never changes a command. This section is what to do; 8.1 has how a call
is judged and every threshold.

- **Live alerts while a call runs** (all critical, one per call, section 8): `ALR-DELIVERY-RAMP-LATE` (not at
  95% of target after the product's ramp time; the times and thresholds are in 8.1), `ALR-DELIVERY-SHORTFALL` (below target for 60 s after the ramp) and `ALR-DELIVERY-NONE` (signed
  commands ask for discharge but nothing measurable is delivered for 60 s). SHORTFALL and NONE also flag the
  obligation AT_RISK (`R-DELIVERY-MEASURED-SHORTFALL`); the flag clears when delivery recovers or the call
  ends. The live alerts clear by themselves on recovery and when the call ends.
- **At the end:** `ALR-DELIVERY-METER-MISMATCH` when the meter disagrees with battery telemetry by more than
  10% (`meter_tolerance_frac`; the record is UNCORROBORATED). It does not clear by itself.
- **What to do:** open the call's record and look at where the series part: commanded below committed means
  the guardian vetoed or the engine limited the call (check the verdicts); commanded but not delivered means
  the hubs did not follow (check hub health and PQ); a meter mismatch goes to the lead for a meter check.
  If a utility or ERCOT call keeps failing, tell the lead before the next call.
- **Where to read it:** on the Dispatch screen (section 3.3), the **Delivery** column of "AS awards &
  deployment" shows each deployed toll or AS call's result, a `METER` badge on a meter mismatch and delivered /
  committed kW; click it for the **Measured delivery** drawer (facts plus the Committed, Commanded, Delivered
  and Meter change chart). Manual discharge targets and the summary are API only (viewer role and up):
  `GET $OG/delivery/records` (filters: service, contract, call kind, result, utility, call ids, date range),
  `GET $OG/delivery/records/<call_id>` (with the per-bucket series) and `GET $OG/delivery/summary?days=7`
  (per contract per day: calls, PASS/PARTIAL/FAIL, compliance, meter mismatches, kWh). The end-to-end delivery
  checks are in `tests-e2e/functional/delivery_check.py`; the API is described in `docs/api/delivery.md`.
- **Customer-side reads:** the utility reads its own calls' records at
  `/og/api/customer/v1/utility/delivery-records[/{call_id}]` (`utility.read`), a customer its own contracts' at
  `/og/api/customer/delivery-records[/{call_id}]`; another's record answers 404 like a missing one.
- **Testing the meter check:** the SCADA simulator's `meter_mismatch` anomaly (`SCADA_METER_MISMATCH`) makes the
  bank meter read `battery_scale` x the batteries' power plus `offset_kw`; the scenario
  `delivery-meter-mismatch.yaml` applies it to `bank-sub-LZ_AEN-00`. Use it only on the simulator.
- The job is on by default (`[delivery] enabled = true`); if og-settle is down, no record is written and no
  delivery alert is raised or cleared.

- **Call rules that go with it (r3.4.3):** `delivery_measured` turns true only once a measured delivered
  value or a final verdict exists (a running call with no telemetry yet stays ACTIVE / UNMEASURED). The
  planned `granted_kw`, `granted_kwh` and `granted_description` fields are deprecated (removed in r3.5); read
  `delivered_*`. A utility may cancel or shorten only a call it issued itself: anything else is 403
  `R-CALL-NOT-ISSUER` (traced AUTHZ_DENY); an operator may stop any call. Ending a call that has already ended
  is 409 `R-CALL-ALREADY-ENDED`.

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
  Since r3.4.3 the hand-off to the guardian is bounded: one cycle's propose phase gives up after 2 s
  (`[allocator] propose_timeout_s`; the heartbeat read after 0.5 s), the banks still pending are reported as
  timed out and the rest go out. The engine then treats the guardian as unavailable, and holds, until the
  guardian writes a newer heartbeat, so a stalled guardian or database no longer freezes the 2 s cycle. The
  obligation lifecycle step (COMMITTED to DELIVERING, window end) also runs in the background since r3.4.3,
  bounded at 30 s, so it cannot delay dispatch either.
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

Every alert is a row with an id, a rule, a severity (`info`, `warning` or `critical`), a summary, and opened and
cleared times. `info` rows (since r3.4.3: firmware campaign progress, accepted utility and dispatch calls) are
notices, not problems. System Health's "Alerts" list is live; the Control room's "Open alerts" reflects page
load. The rule id is not a column: the summary text says which it is. **Acknowledging** records who looked
(section 6.8); it never clears an alert. An alert clears when its condition ends, and only its owner clears it:
the health evaluator (which runs inside og-settle every 5 s), og-engine, og-settle (including its delivery job),
og-guardian, og-safestop or (for the ERCOT AS poll alerts) og-feeds. If og-settle is down, no health or
delivery alert is raised or cleared and the degraded modes freeze.

| Rule | Severity | Raised when | Clears |
|---|---|---|---|
| `ALR-PROCESS-DOWN` | critical | A process's heartbeat (feeds, engine, guardian, safestop, api) is missing for more than 15 s. "Process {name} heartbeat missing" | When it heartbeats again |
| `ALR-SIM-OFFLINE` | critical | No fleet telemetry and no SCADA reading reached the database for 60 s. The engine writes both, so an og-engine outage also raises it | When readings arrive again |
| `ALR-FEED-STALE` | warning | A feed's latest value is older than its freshness window (section 7.1). "Feed ERCOT:np6-905-cd stale for over 2700s". Only the blocking feeds (price and, since R3, AS price) also set Feed stale | When the feed is fresh again |
| `ALR-SCADA-SILENT` | critical | No SCADA bank reading at all for over 60 s (never before the first reading); shown as "SCADA silent" | og-settle clears it when a reading arrives |
| `ALR-SCADA-SILENT-BANK` | warning | Since R3: one bank's own SCADA readings stopped for over 60 s while other banks may still report. "Bank {bank} SCADA silent for over 60s". Sets no mode | When that bank's reading arrives |
| `ALR-FEED-LGV-EXHAUSTED` | critical | A feed's circuit breaker is open (5 consecutive failures, or half of the last 10 polls) | When the breaker closes |
| `ALR-HUB-OFFLINE-RATIO` | warning / critical | Offline plus fault hubs in a zone exceed 5% (warning) or 20% (critical). A hub is offline after `[health] hub_offline_s` (60 s) without telemetry and stale after `hub_stale_s` (25 s); hubs report every 10 s. Since r3.4.1 the Fleet table, map and System Health all use these two settings (3.2) | When the ratio is 5% or less |
| `ALR-SCADA-OVERLOAD` | warning / critical | A bank's latest SCADA apparent power exceeds its kVA rating (warning) or 120% of it (critical) | When the reading is at or under the rating |
| `ALR-COMMAND-BAD-SIGNATURE` | critical | A hub on the bank rejected a command batch as BAD_SIGNATURE (forged or tampered, never applied) in the last 300 s (`[health] command_bad_signature_window_s`). "Bank {bank}: 1 command batch(es) rejected by hub(s) {hub} ..." | 300 s after the last such rejection |
| `ALR-ENERGY-SHORTFALL-RISK` | critical | A committed, delivering or shortfall obligation starting within 15 min lacks the energy above reserve to finish its window (for an AS award: its full product duration). The card turns AT_RISK | og-engine clears it when the obligation is no longer at risk |
| `ALR-RESERVE-BREACH` | critical | The K1 check has recorded any home discharging below its reserve | Only when the count is 0; **known gap:** the count is a running total, so once raised it stays |
| `ALR-CYCLE-P99`, `ALR-CYCLE-P99-APPROACHING` | warning | The engine's 2 s cycle p99 is over its 500 ms budget for 3 reads, or within 80-100% of it. Active: `[health] engine_metrics_url` is set | When it is back under |
| `ALR-GUARDIAN-TIMEOUT-RATE` | critical | More than 1% of guardian verdicts time out. **Known gap:** never raised with the shipped config: `[health] guardian_metrics_url` is not set (og-guardian serves `localhost:9103/metrics`) | When the rate drops |
| `ALR-SELECTOR-GATE-FAILED` | warning | A selector gate failed, including a solver timeout. "Selector gate {kind} failed (...)"; each failure adds a row | og-engine clears it when the same gate next succeeds |
| `ALR-OBLIGATION-STUCK-SELECTED` | critical | An obligation stayed SELECTED for more than 120 s and could be neither committed nor rejected | When it is no longer stuck |
| `ALR-SETTLE-STALLED` | critical | og-settle's settlement job has not completed for more than 180 s | og-settle clears it when the job succeeds |
| `ALR-TRACE-VERIFY-FAILED` | critical | The 5-minute trace check found a stream whose hash chain does not verify. **Known gap:** a new row appears every 5 minutes while the failure lasts | When a run finds no failure |
| `ALR-SCOPE-CONSERVATIVE` | warning | Guardian escalation: a bank or zone went CONSERVATIVE (section 7.2) | og-guardian clears it when the scope returns to normal |
| `ALR-SAFE-STOP-REQUESTED` | critical | Guardian escalation: three consecutive conservative ticks; a safe stop is requested for a person to review | og-guardian clears it when the scope returns to normal |
| `ALR-CLOCK-QUALITY` | critical | The guardian's clock is more than 200 ms off NTP (G-20). While it lasts, every verdict is TIMEOUT and nothing is signed | og-guardian clears it when the clock is back in limit |
| `ALR-CALIBRATION-BUDGET` | warning | The guardian held a remote calibration (fleet budget, concurrency or suspected systemic drift, G-25). Dormant while `[assets] drift_enabled = false` | No automatic clear |
| `ALR-CALIBRATION-PROTOCOL` | warning | A calibration acknowledgement did not match the issued command, or the hub rejected it. Dormant like the above | No automatic clear |
| `ALR-XFMR-UNMAPPED` | warning | og-guardian checked a batch with a hub that has no service-transformer mapping, so G-27 checks it as a group of one. Since r3.4.1 the topology seed maps every hub (D-36: a 600 kVA home bank has 12 transformers of 50 kVA), so a fresh install or a backfilled server raises none; a new row means a hub was added without topology: tell the lead | No automatic clear |
| `ALR-BANK-UNMAPPED-TOPOLOGY` | warning | og-guardian commanded a bank with no feeder mapping, so G-06, G-28 and G-32 are not evaluated for it (vetoed instead when `fail_closed_missing_topology` is on) | No automatic clear |
| `ALR-SUBSTATION-UNMAPPED-TOPOLOGY` | warning | Since r3.4.1: og-guardian skipped G-29 for a bank because it has no substation mapping or its substation has no limit row. One row per bank | No automatic clear |
| `ALR-DEVICE-RATING-MISMATCH` | warning | A hub's own report (device info) gives a rating or location that differs from the seed (3.2) | When a later report matches the seed |
| `ALR-TRACE-QUARANTINED` | critical | Since r3.4.1: the database refused a trace row for its content (not an outage); the row went to the journal's quarantine file and the replay continued. The file is the evidence: tell the lead | No automatic clear |
| `ALR-STOP-PUBLISH-DEAD-LETTER` | critical | Since r3.4.1: og-safestop gave up publishing a stop or release message after its maximum attempts; other scopes carry on. Check the scope on Fleet and propose the stop again if it is still needed | No automatic clear |
| `ALR-ANCHOR-PUBLISH-FAILED` / `ALR-ANCHOR-SECONDARY-FAILED` | critical / warning | The periodic K11 trace anchor could not be published at all, or only its secondary copy failed | When the next anchor publishes |
| `ALR-UTILITY-CALL` | info | A utility call was accepted, cancelled or shortened through the utility API or the grid link (6.10). `ALR-DISPATCH-CALL` is the same notice for other non-operator origins | No automatic clear |
| `ALR-UTILITY-CALL-REFUSED` | warning | A utility call was refused, with its reason code (6.10), including a cancel or shorten of a call the utility did not issue (403 `R-CALL-NOT-ISSUER`). `ALR-DISPATCH-CALL-REFUSED` is the same for other non-operator origins | No automatic clear |
| `ALR-FIRMWARE-CAMPAIGN-STARTED` / `-COMPLETED` | info | A firmware campaign started or completed | No automatic clear |
| `ALR-FIRMWARE-HUB-FAILED` | warning | A hub's firmware update failed terminally | No automatic clear |
| `ALR-FIRMWARE-CAMPAIGN-HALTED` | critical | A firmware campaign stopped at its failure threshold | No automatic clear |
| `ALR-ERCOT-AS-REFUSED` | warning | Since r3.4.2 (D-35): og-feeds refused an ERCOT AS dispatch instruction (404/409/422 and a reason code) and answered ERCOT REJECT. One row per instruction; a re-delivered duplicate adds none (6.5) | No automatic clear |
| `ALR-ERCOT-AS-POLL-FAILED` | warning | Three ERCOT AS instruction polls in a row failed | og-feeds clears it on the next good poll |
| `ALR-ERCOT-AS-PROCESSING-FAILED` | critical | Since r3.4.3: one ERCOT AS instruction raised an error while being applied (for example a database error). It is not acknowledged, so ERCOT re-sends it and it is retried every poll; the other instructions in the batch still run. Also counts toward POLL-FAILED and STALE. One row per instruction | og-feeds clears it when that instruction is next processed successfully |
| `ALR-ERCOT-AS-POLL-STALE` | critical | No ERCOT AS instruction poll has succeeded for 60 s: a deployment may be missed | og-feeds clears it on the next good poll |
| `ALR-DELIVERY-RAMP-LATE` | critical | Since r3.4.3 (D-38, 8.1): a running discharge call (toll call, AS deployment or manual discharge target) is past its product's ramp time and its measured power has never reached 95% of the called kW. "Call {id} not at target after N s (ramp R s): delivered X kW of Y kW" | og-settle's delivery job clears it once delivery reaches the target, or when the call ends |
| `ALR-DELIVERY-SHORTFALL` | critical | Since r3.4.3: a running call's measured power has stayed under 95% of the called kW for 60 s (`[delivery] shortfall_alert_s`), counted from when it reached the target, or from the end of its ramp time if it never did. Its obligation is AT_RISK meanwhile. "Call {id} below target for N s: delivered X kW of Y kW" | The delivery job clears it when delivery is back at target, or when the call ends; AT_RISK clears with it |
| `ALR-DELIVERY-NONE` | critical | Since r3.4.3: guardian-signed commands ask the call's hubs to discharge, but the measured delivery has stayed at or under 5% of the called kW for 60 s (`none_alert_s`). Its obligation is AT_RISK meanwhile. "Call {id} commanded for N s with no measurable delivery" | The delivery job clears it when delivery resumes, or when the call ends; AT_RISK clears with it |
| `ALR-DELIVERY-METER-MISMATCH` | critical | Since r3.4.3: at the end of a call, the independent meter on its banks disagrees with the batteries' telemetry by more than 10% of the energy, floored at 25 kW (the record is UNCORROBORATED). "Call {id}: independent meter disagrees with battery telemetry by N%; delivery UNCORROBORATED" | No automatic clear: the delivery job leaves it open for an operator, and the console and API can only acknowledge it (6.8) |
| `ALR-TRACE-VERDICT-WRITE-FAILED` | warning | A verdict's `GUARDIAN_VERDICT` trace row could not be written (the verdict stands; the audit row is missing) | No automatic clear |

An open alert keeps the severity it opened with: a zone that goes from 6% to 25% offline stays a warning until
it clears and re-opens.

### 8.1 Delivery alerts (D-38)

Since r3.4.3 a discharge call is judged on the power its hubs actually reported (telemetry), never on its grants:
a grant the guardian vetoes still counts as granted. og-settle's delivery job does this every 15 s for every toll
call, ERCOT AS deployment (whatever its origin) and operator manual discharge target, and it alone raises and
clears the four `ALR-DELIVERY-*` alerts above. r3.4.2 and older raise none of them.

**How a call is judged** (`[delivery]` in the orchestrator config; defaults shown):

- **Intervals:** 30 s from the call's start (`bucket_s`), each read 30 s after it ends (`telemetry_lag_s`), so a
  verdict lags real time by about 30 to 60 s. An interval without telemetry is unmeasured, never counted as 0.
- **Reach:** measured discharge must reach 95% of the called kW (`target_frac`) within the product's ramp time,
  `[delivery.ramp_time_s]` by contract variant: ECRS, RRS and TOLLING 600 s, REGUP and REGDN 300 s, NSPIN 1800 s,
  a manual target (MANUAL) 120 s, anything else 600 s. Hubs ramp, so a large step takes minutes (6.2).
- **Sustain:** from then on (from the end of the ramp time if it never got there), at least 95% of the measured
  intervals (`sustain_pass_pct`) stay at or above that level.
- **Result:** IN_PROGRESS while the call runs. At its end: FAIL when nothing was measured, when the average was
  at most 5% of the called kW, or when under half the called kWh was delivered; PARTIAL for any other reason
  (late, not sustained, over 10% of the intervals unmeasured, at least half the command cycles vetoed, ended
  early); otherwise PASS. The final result is traced (stream `delivery`).
- **Meter:** on a bank with an independent meter (a substation battery set's bank, plus `meter_bank_ids`), the
  meter's change over the call is compared with the batteries'. More than 10% apart (`meter_tolerance_frac`,
  floored at 25 kW, `meter_floor_kw`) is UNCORROBORATED. Manual targets are never metered.
- **AT_RISK:** while SHORTFALL or NONE is open, the call's obligation is AT_RISK (reason
  `R-DELIVERY-MEASURED-SHORTFALL`). It clears when delivery recovers or the call ends. The obligation's SHORTFALL
  state (7.3) stays the engine's.
- The check only observes. It never changes dispatch or stops a call (K7), a firm call keeps its best effort
  (7.3), and settlement bills measured energy.

**Where to look**

- The alert's summary names the call id: a deployment's id, or a manual target's trace id.
- `GET /og/api/delivery/records/<call id>` (viewer role): the call's record with its 30 s series. Each point has
  `c` committed, `m` commanded (guardian-signed), `d` delivered (none = no telemetry), `p`/`v` proposed and
  vetoed command cycles, `md`/`bd` meter and battery change; the record adds `result`, `reasons`,
  `time_to_target_s`, `sustained_pct`, `lowest_kw` and `meter_status`.
- `GET /og/api/delivery/records` lists records newest first; filter with `service_type`, `contract_id`,
  `call_kind`, `result`, `utility_id`, `call_ids`, `since`, `until`, `limit`. `GET /og/api/delivery/summary?days=7`
  gives, per contract per day, the calls, PASS/PARTIAL/FAIL, meter mismatches and delivered vs committed kWh.
- A utility's call status and the grid link report the measured kW: RAMPING under 90% of the call's target
  (`[dispatch.calls] ramping_fraction`), DELIVERING at or above it, ACTIVE while unmeasured.

**What to check**

- **RAMP-LATE:** open the record (Dispatch: click the call's **Delivery** value for the Measured delivery drawer; its chart draws the committed, commanded, delivered and meter-change series). `m` rising while `d` lags means the hubs are slow or stalled; `m` flat means
  nothing signed reaches them (`reasons` has VETOED when at least half the cycles were vetoed). In Dispatch, set
  the Ledger scope to a call bank: **Real-time grants & substitutions** shows its grants. In Fleet, filter to that
  bank: are the hubs' P (kW) moving? Hub detail shows health and last command. Look for a safe stop, or a utility
  L2 limit or block on the bank (7.1, 7.3).
- **SHORTFALL:** delivery got there and fell back. Look for what changed on those banks: hubs going stale or
  offline (System Health's hub counts, `ALR-HUB-OFFLINE-RATIO`), an L2 limit or block, homes near reserve
  (`ALR-ENERGY-SHORTFALL-RISK`). The obligation stays AT_RISK until it recovers.
- **NONE:** commands are signed but nothing moves. Check the hubs' health and last command, and whether telemetry
  reaches the database at all (`ALR-SIM-OFFLINE`). Tell the lead at once.
- **METER-MISMATCH:** the meter and the batteries disagree on how much was delivered. Compare `md` and `bd` in the
  record's series, and check the bank's SCADA alerts (`ALR-SCADA-SILENT-BANK`, `ALR-SCADA-OVERLOAD`). Settlement
  bills battery telemetry, so tell the lead. Acknowledge the alert once investigated; it does not clear by itself.
- No delivery alert is a reason for a safe stop (K7: a measured shortfall never stops a firm obligation). Tell the
  lead about any call that is late, short or dead, with its call id.

## 9. Known gaps in this release

| Gap | What to do meanwhile |
|---|---|
| A manual target on a bank with no obligation grant never moves the hub (G-09) and raises `ALR-SAFE-STOP-REQUESTED` for the bank and zone (6.2) | Command hubs on delivering banks; cancel a target that does not move; do not engage the offered stop |
| A 503 on a manual or bulk confirm always shows the badge NOT RECORDED, even when the detail says "outcome unknown ... it may be live" (6.2) | Read the detail; check `GET $OG/fleet/manual-targets` or `target X kW` in the Fleet table before proposing again |
| Fleet table refreshes only P and telemetry age (every 10 s); it pages through the whole fleet (Rows 25 or 50) | Reload for SoC, Activity and Health |
| AS deploy form: for an award whose product has no known duration it still offers up to 240 min, which og-api refuses (409) | Deploy one award, within its product's window; og-api refuses an overlapping deployment |
| The ERCOT AS poller and the grid link ship disabled | The release manager enables them (`deploy/RUNBOOK.md`); until then ERCOT deployments are entered with **Deploy** |
| Only Feed stale (the price and AS price feeds) is enforced; SCADA silent is shown only | Treat the other banners as a call to act (7.1) |
| The AS price feed is polled once a day; one missed poll can keep Feed stale on for up to about a day | Check `ALR-FEED-STALE` for `ERCOT:np4-188-cd`; tell the lead |
| Escalations, Control-room banner and alerts reflect page load | Reload; System Health's banner and alerts are live |
| Best-effort shortfall is labelled "Delivered short" | Watch the grants, not the card |
| Topology alerts (`ALR-XFMR-UNMAPPED`, `ALR-BANK-UNMAPPED-TOPOLOGY`, `ALR-SUBSTATION-UNMAPPED-TOPOLOGY`) do not clear by themselves | After the lead fixes the mapping (`deploy/scripts/topology_backfill.sh`), acknowledge; new rows mean a new gap |
| The Dispatch Delivery column covers deployed AS and toll rows only; manual discharge targets and the per-contract summary have no console view (r3.4.3) | Use `GET $OG/delivery/records?call_kind=MANUAL_TARGET` and `GET $OG/delivery/summary` (6.13, 8.1) |
| `ALR-DELIVERY-METER-MISMATCH` never clears: the delivery job leaves it for an operator, and the console and API can only acknowledge it | Acknowledge it once investigated (8.1) |
| A Non-Spin contract whose variant is spelled `NONSPIN` or `NON_SPIN` gets the 600 s default ramp time, not `NSPIN`'s 1800 s (`[delivery.ramp_time_s]` is keyed by the variant) | Read a RAMP-LATE on such a call within its first 30 minutes as possibly early; tell the lead |
