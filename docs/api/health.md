# Health, degraded modes and guardian escalation

The health payload is served two ways, with the same body:

| Endpoint | Who | Notes |
|---|---|---|
| `GET /og/api/health` | Loopback only (the deploy-time liveness probe) | Ignores identity headers; any non-loopback connection is refused |
| `GET /og/api/stream/health` | Viewer | SSE, a new event every 2 s when the payload changes |

## Payload fields

| Field | Meaning |
|---|---|
| `status`, `as_of` | Always `ok` when the API answers; the time the payload was built |
| `degraded_modes` | The degraded modes in force right now (below). Empty list: normal operation |
| `processes` | Per process: last heartbeat `ts`, `pid`, `status`. A heartbeat older than 15 s is down |
| `feeds` | Per product: `last_value_at`, `quality` (`GOOD` or `STALE` when the breaker is open), `breaker_open`, `consecutive_failures` |
| `hub_health_counts` | Hubs by health (`online`, `stale`, `offline`, `fault`) |
| `alerts`, `open_alert_count` | Open alerts |
| `fleet_mw` | Sum of the fleet's current output |
| `active_commitments`, `net_margin_usd` | Live commitments; today's net margin |
| `reserve_breaches`, `double_sold_kwh` | Measured K1 and K2 checks (`og.invariant_check`), not constants |

## Degraded modes

`degraded_modes` is read from `og.degraded_mode_state`, which the health evaluator (`opengrid.health`) writes every cycle
(`02b` §6.5). A read failure is logged and reported as an empty list, never as a crash.

| Mode | Raised when | What the system does |
|---|---|---|
| `NO_NEW_COMMITMENTS` | A market feed crosses STALE | No new commitments are made; existing ones are kept (K13) |
| `HOLD_LOCAL_AUTONOMY` | og-engine is down | Hubs hold their last setpoint, then fall back to local autonomy (K7) |
| `HOLD` | og-guardian is down or its verdicts time out | No new commands execute; the engine holds the last grants (K7: TIMEOUT is not VETO) |
| `DIST_DEFERRAL_OPEN_LOOP` | SCADA readings for a bank go silent | That bank's `DIST_DEFERRAL` PI loop runs open-loop instead of acting on a stale reading |

## Guardian escalation (K7)

The guardian owns three alerts; it raises and clears them itself (the health evaluator never auto-clears them):

| Alert | Meaning |
|---|---|
| `ALR-SCOPE-CONSERVATIVE` | More than 5% of a scope's batches were vetoed in a tick, so the scope runs CONSERVATIVE |
| `ALR-SAFE-STOP-REQUESTED` | The veto rate stayed high for three ticks. The guardian **requests** a safe stop: an unconfirmed proposal an operator must confirm through the two-step flow. It never engages a stop by itself |
| `ALR-CLOCK-QUALITY` | The guardian's own clock check (G-20) is failing, so it answers TIMEOUT rather than sign |

Both escalation alerts clear once the veto rate recovers, or after a run of ticks in which the scope sends no
commands at all (no evidence left either way). Acknowledging an alert
(`POST /og/api/alerts/{alert_id}/ack`) records who saw it; it does not clear it.
