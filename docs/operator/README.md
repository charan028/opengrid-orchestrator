# OpenGrid Orchestrator: operator guide

For the control-room operator. It explains what each screen shows, how the two-step actions work, what
every alert means and what to do about it, how the system degrades when something is down, why a
committed delivery never moves to a better price, and where the audit trail is.

The UI is at `https://base.tocy-net.net/og/`. Two accounts exist. **operator** can read and act.
**viewer** can only read; every button that changes anything is hidden for viewers. Credentials come
from the lead. All times in the UI are UTC unless a screen says otherwise.

## 1. Reading any screen

Three conventions hold on every screen.

- **Every value shows its age.** A small badge next to each number or table says how old the data is
  and turns to a stale state once it passes that screen's threshold. Trust a value only while its badge
  is fresh.
- **Live or polled.** The header badge says `live` when the screen is receiving a push stream, and
  `reconnecting` when it has lost it. Markets and Profitability say `poll: 30s` instead; they refresh
  themselves every 30 seconds.
- **A yellow "degraded" banner** at the top means the screen could not reach the API for its first
  paint. The screen still renders with whatever it has. Check the Health screen.

Status is never colour alone. Every badge carries a word and an icon, so it reads the same on a
monochrome display.

## 2. The seven screens

### Control room (`/og/`)

The one screen to leave open. Seven KPI tiles, the fleet map, the market ticker, and open alerts.

| Tile | Meaning | Should be |
|---|---|---|
| Fleet power | Total discharge right now, MW | Tracks the sum of committed deliveries |
| Fleet energy | Energy available above reserve, MWh | Positive; falls through the delivery window |
| Active commitments | Obligations in COMMITTED or DELIVERING | Matches the Dispatch pipeline |
| Today's net margin | Revenue minus costs and penalties, USD | Positive |
| Reserve breaches | Times a hub went below its home reserve floor | **Zero. Always.** |
| kWh sold twice | Energy promised to two buyers at once | **Zero. Always.** |
| Commitment switches | A committed delivery moved to another buyer | **Zero. Always.** |

The last three tiles are the safety invariants. They turn critical on any non-zero value. If one of them
is not zero, stop and call the lead; do not wait for an alert.

The fleet map colours each hub green for online, amber for stale or offline, red for fault. The alerts
table is the same list as on the Health screen. Operators can acknowledge an alert by id here.

### Fleet monitoring and control (`/og/fleet`)

Every hub in one table: bank, zone, health, state of charge, current power, age of last telemetry. Filter
by zone, bank, or health state. Click a row, or press Enter on it, to open the drill-down: lease epoch,
lease expiry, last command id, and a telemetry sparkline.

Hub health states:

| State | Meaning | Default threshold |
|---|---|---|
| online | Telemetry arrived recently | Under 4 s old |
| stale | Telemetry is late but the hub is probably fine | 4 s to 30 s old |
| offline | No telemetry; the hub is excluded from dispatch | Over 30 s old |
| fault | The hub reported a fault code; excluded regardless of age | Immediate |

This screen also holds the two operator actions, **Scoped safe stop** and **Manual command**. See
section 3.

### Dispatch and commitments (`/og/dispatch`)

The commercial state of the fleet.

- **Opportunity pipeline.** A five-column board: OFFERED, SELECTED, COMMITTED, DELIVERING, FULFILLED or
  SHORTFALL. Each card is one obligation for one customer with its service, tier, and committed kW. An
  amber border means the obligation is at risk of shortfall. The header shows how many customers are
  being served at once. Several at once is normal.
- **Ledger timeline.** Pick a bank. The stacked area chart shows committed capacity per obligation over
  time with free headroom on top. New opportunities can only take headroom; they can never eat into a
  committed band.
- **Latest selector plan.** The most recent optimiser run: mode, gate, horizon, solver status and gap.
  A `rule` mode means the LP solver was unavailable and the deterministic fallback chose instead.
- **Commitment-lock events.** Every time a committed quantity was reduced, with its reason code. Read
  section 5 before worrying about this table.
- **Real-time grants and substitutions.** The last allocator cycle's per-hub grants, and any hub that
  was swapped in for one that dropped.

### Markets and feeds (`/og/markets`)

ERCOT price, load, wind, solar and ancillary-service series, the P10/P50/P90 forecast band, and a
freshness table for every feed. Each feed row shows its mode (live or simulated), age, consecutive
failures, and whether its circuit breaker is open. A breaker that is open means the feed has failed
repeatedly and the system is running on its last good value. Polls every 30 seconds.

### Health (`/og/health`)

The system's own vital signs.

- **Degraded-mode banner** at the top when one applies. See section 4.
- **Processes.** All seven: feeds, engine, guardian, safestop, sim, settle, api. A process is DOWN after
  three missed heartbeats, 15 seconds by default.
- **Feed freshness**, **Hub health by zone**, **Cycle latency** p50 and p99 in milliseconds. The budget
  for p99 is 500 ms.
- **Alerts** with acknowledge. Section 6 explains each rule.

### Profitability (`/og/profitability`)

Per obligation per day: revenue, energy cost, degradation cost, penalty, net. Filter by service and day.
Two extra views matter.

- **Forgone upside (lock).** Money the fleet could have made by breaking a commitment for a better price,
  and deliberately did not. A non-zero value here is the commitment lock working, not a loss to fix.
- **LP vs rule baseline.** What the optimiser earned against what the simple rule selector would have.

### Billing and audit (`/og/billing`)

Invoice lines per contract and obligation with a CSV export, measurement-and-verification compliance,
and the trace explorer. The **Run chain verify** button re-hashes the audit trail and reports whether
every link holds. See section 7.

## 3. The two-step actions

Nothing that changes the fleet happens on one click. Both actions follow the same shape.

1. Fill in the form and press **Propose (step 1 of 2)**. Nothing happens to the fleet yet. The API
   returns a proposal with a plain-language summary of exactly what will be done.
2. A dialog opens with that summary and a countdown. Read the summary. If it is what you meant, press
   the confirm button before the countdown reaches zero. If not, press Cancel or Escape.
3. The result appears as a badge and a sentence. Never assume success from silence.

Keyboard: the dialog opens with focus on Cancel. Tab moves to confirm. Escape closes and returns focus
to the button you started from.

### Manual command

Fleet screen, bottom panel. Sets a power setpoint in kW on one hub or one bank, with a reason. The
proposal lives 60 seconds. On confirm, the command goes to the guardian like any automatic dispatch,
and the guardian's verdict is what you see.

| Result | Meaning | What to do |
|---|---|---|
| PASS | The guardian signed it; hubs are executing | Watch the hub on the Fleet screen |
| VETOED | The guardian refused; rule ids are listed | Read the rule ids. Usually reserve floor, bank kVA, or stale hub data. Do not retry with a bigger number |
| TIMEOUT | No verdict within the wait window | Check the Health screen for the guardian process |
| EXPIRED | You confirmed after 60 s | Propose again |

A manual command is a proposal like any other. It cannot bypass the guardian and it cannot take
capacity from a committed obligation.

### Scoped safe stop

Fleet screen, top panel, visually distinct button. Stops **new dispatch** for a scope: the whole fleet,
one zone, or one bank. It never force-discharges below reserve and it does not cut power to homes.
Hubs ramp down over 30 s for a bank, 60 s for a zone, 120 s for the fleet.

The confirm window is 30 seconds. A single proposal on its own never stops anything, and a confirm after
the window is ignored; propose again.

Safe stop runs in its own process with its own key and depends on nothing else. It works when the engine
and guardian are both down. That is the point of it.

**Release is not built.** The stop-only key cannot sign a release. Releasing a safe stop needs a
two-person signing path that does not exist yet, so the release endpoint returns "not implemented" and
records the attempt. Until it is built, releasing a stop is a task for the lead on the server.

When to use it:

- a utility instruction to limit or block a bank;
- a SCADA overload alert that is not clearing on its own;
- any sign of a reserve breach, double-sold energy, or a commitment switch;
- anything you do not understand that is getting worse.

Use the smallest scope that covers the problem. A bank stop costs one bank's revenue; a fleet stop costs
all of it.

## 4. Degraded modes

The Health screen banner names the mode. The system keeps running in every one of them; what changes is
what it allows itself to do.

| Mode | Trigger | Behaviour | What to do |
|---|---|---|---|
| NO_NEW_COMMITMENTS | A price or load feed went stale | Existing commitments keep delivering on last good values. Nothing new is committed | Check Markets for which feed. If the breaker is open, the lead may need to rotate a key or switch to the simulator |
| HOLD | Guardian down, or verdicts timing out | Hubs hold their last signed setpoint until the lease expires, 30 s by default | Check the guardian process. If it does not come back, hubs will go local on their own |
| HOLD_LOCAL_AUTONOMY | Engine down | Same hold, then each hub falls back to serving its own home | Restart the engine (lead). Commitments in flight will show shortfall |
| DIST_DEFERRAL_OPEN_LOOP | SCADA silent for a bank under a distribution-deferral contract | The bank keeps its planned deferral without live load feedback | Check the SCADA feed. Consider a bank safe stop if load is unknown for long |

Modes combine. Two banners at once is possible and not a bug.

## 5. Why a better price does not move a committed delivery

Once an obligation is COMMITTED, that capacity belongs to that customer for that interval. A new
opportunity paying ten times more competes only for uncommitted headroom. This is the commitment lock,
invariant K13, and the guardian enforces it independently of the engine: any batch that would reduce a
committed allocation is vetoed, whatever the price.

The Profitability screen shows the money this leaves on the table as **Forgone upside**. That number is
expected to be non-zero on volatile days. It is the cost of being a counterparty customers can rely on.

Only four reason codes may reduce a committed quantity, and every one of them writes a row to the
Commitment-lock events table on the Dispatch screen. L0, L1 and L2 are the safety tiers; they outrank
every commercial tier T1 to T4.

- **R-COMMIT-LOCK-OVERRIDE-L1**: the envelope. Home reserve floor, hub power limit, bank kVA rating, ramp
  limits. Physics and the customer's own reserve come first.
- **R-COMMIT-LOCK-OVERRIDE-L2**: a utility or ISO instruction to limit, block, or emergency-stop a bank
  or zone. Never traded, whatever the price.
- **R-COMMIT-LOCK-OVERRIDE-L0**: the top safety tier above both, as defined in the invariants document.
- **R-COMMIT-LOCK-INFEASIBLE**: the committed quantity can no longer be delivered. Hubs went offline or
  faulted and no substitute was available.

A fifth path, releasing ancillary-service capacity, exists in the code but is switched off for this
release. A row with any other reason, or the **Commitment switches** tile being non-zero, is a defect.
Report it.

A re-nomination point is different. It is a moment written into the contract where the selector may
re-plan the remaining intervals. It shows on the Dispatch board as a DELIVERING card that stays
DELIVERING with a re-nomination reason, and it never reduces the interval being delivered right now.

## 6. Alerts

Alerts are raised once per condition and clear on their own when the condition ends. Acknowledging one
records who saw it; it does not clear it.

| Alert | Severity | Meaning | What to do |
|---|---|---|---|
| ALR-RESERVE-BREACH | critical | A hub was dispatched below its home reserve. Must be zero | Bank or fleet safe stop. Call the lead. This is a K1 violation |
| ALR-PROCESS-DOWN | critical | One of the seven processes missed three heartbeats | Check the Health processes table. See section 4 for what the system does meanwhile. The lead restarts it |
| ALR-GUARDIAN-TIMEOUT-RATE | critical | Over 1% of guardian verdicts are timing out | Hubs will start holding. Check guardian and database load. Lead |
| ALR-FEED-LGV-EXHAUSTED | critical | A feed's circuit breaker is open and its last-good-value window has run out | No new commitments will be made. Lead decides between key rotation and switching to the simulator |
| ALR-SCADA-OVERLOAD | warning at 100%, critical at 120% | A bank's SCADA load is over its kVA rating | Watch for a DIST_DEFERRAL response reducing the bank. If load keeps climbing, bank safe stop |
| ALR-HUB-OFFLINE-RATIO | warning over 5%, critical over 20% | Too many hubs in one zone are offline or faulted | Likely a comms or hub-software problem in that zone. Substitution covers small numbers; large numbers mean shortfall |
| ALR-FEED-STALE | warning | A feed has not delivered in its freshness window | Check Markets freshness. Usually self-clears. If it does not, expect NO_NEW_COMMITMENTS |
| ALR-CYCLE-P99 | warning | The 2 s allocator cycle's p99 exceeded 500 ms for three cycles running | Check the Health latency chart. Sustained: the lead looks at engine and database |

## 7. The audit trail

Every decision the system makes is a trace record: selections, commitments, re-nominations, exceptions,
shortfalls, commands, operator actions, feed changes, alerts. Records are hash-chained per stream, so a
record cannot be altered or removed without breaking every link after it.

On the Billing and audit screen:

1. Filter by trace class and time window, or leave them blank.
2. Read the trace explorer. Each row is one decision with its reason codes.
3. Press **Run chain verify**. The result says `passed`, how many records were checked, and the first
   broken link if any.

A failed verify is an incident. Note the first broken record id and call the lead. Do not acknowledge
or act on anything else until it is understood.

Your own actions are in the same trail. Every propose and confirm is an operator-action trace record
carrying your identity, which is why the two-step summary matters: you are signing off on exactly that
text.

## 8. Start, stop, and logs

Operators do not start or stop processes; the lead does, on the server as root. For reference:

```bash
systemctl status opengrid.target ogsim.target   # all six orchestrator units, all four simulators
systemctl restart og-engine                     # any single unit
journalctl -u og-guardian -f                    # tail one process
```

Restarting the engine or guardian puts the fleet into HOLD for the duration, then it recovers on its
own. Restarting og-safestop loses any un-confirmed safe-stop proposal; propose again.

## 9. Real, simulated, derived

| Data | Source |
|---|---|
| ERCOT prices, load, wind, solar; EIA; NWS weather | Real, live public APIs |
| Hub telemetry, SCADA bank load, utility instructions | Simulated by `ogsim` |
| Anomalies: price spikes, comms loss, overloads, forged commands | Injected on demand from the simulator control plane |
| Forecasts, plans, grants, verdicts, P&L, invoice lines | Derived by the orchestrator |

The Markets freshness table labels each feed live or simulated, so you can always tell which you are
looking at.
