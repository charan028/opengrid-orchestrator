# Auth, two-step actions, streams and errors

## Authentication and roles

Apache terminates HTTP Basic Auth and forwards the authenticated name in the `X-Remote-User` header, after
stripping any value a client sent. Loopback is not a trust boundary on the base server (simulators and other
local processes can reach `og-api`), so `og-api` believes `X-Remote-User` only when the request also carries
`X-OG-Proxy-Auth` equal to the shared secret `OG_API_PROXY_SECRET`. Apache sets that header (after unsetting
any client value); nothing else is given the secret. No secret configured, no header, or a wrong one: `401`.
The role is derived from the identity alone (`[api.roles]`, below); no client-supplied role header is trusted.

| Role | Can do | How an identity gets it |
|---|---|---|
| `viewer` | Every `GET` and SSE endpoint | The Apache account named `viewer`, or a name in `[api.roles.viewer]` |
| `operator` | Everything a viewer can, plus every write | The Apache account named `operator`, or a name in `[api.roles.operator]` |

Every endpoint needs the header except `GET /og/api/health`, which is the deploy-time liveness probe. It ignores the
header and accepts only connections that originate from loopback.

**Operator-only endpoints** (all others are viewer-readable):

| Endpoint | Purpose |
|---|---|
| `POST /og/api/fleet/command` and `.../{proposal_id}/confirm` | Manual hub or bank setpoint (two-step) |
| `POST /og/api/safestop` and `.../{proposal_id}/confirm` | Engage a scoped safe stop (two-step) |
| `POST /og/api/safestop/{scope}/{scope_id}/release` and `/og/api/safestop/release/{proposal_id}/approve` | Release a stop (two-person) |
| `POST /og/api/contracts`, `PATCH /og/api/contracts/{contract_id}` | Create or change a contract |
| `POST /og/api/opportunities` | Create an opportunity |
| `PUT /og/api/retention` | Change a trace retention policy |
| `POST /og/api/alerts/{alert_id}/ack` | Acknowledge an alert (does not clear it) |
| `POST /og/api/scenario/{name}` | Trigger a simulator scenario |
| `POST /og/api/admin/seed-fleet-topology` | Re-run the idempotent hub and bank seed |

Non-mapped identities get `403`; a missing identity header or a missing/wrong proxy secret gets `401`.

## CSRF (browsers)

State-changing requests (anything except `GET`, `HEAD`, `OPTIONS`, `TRACE`) pass two checks:

1. **Origin allowlist.** If the request carries `Origin` or `Referer`, its host must be loopback or listed in
   `[api].csrf_allowed_hosts`. Otherwise `403`.
2. **Double-submit token.** The server sets an `og_csrf` cookie (`SameSite=Strict`). A caller that holds that cookie must
   echo its value in an `X-CSRF-Token` header (or a `csrf_token` form field). A mismatch is `403`.

The HTMX screens echo the token automatically. A script or CLI that never received the cookie (it authenticates with
Basic Auth alone) is not subject to the token check, but the Origin check still applies if it sends an `Origin` header.
To call a write endpoint from a browser-like client, send one `GET` first, keep the cookie, and echo it.

## Two-step actions

A manual command or a safe stop never takes effect on the first call. Step 1 stores a proposal, step 2 confirms it, and
only then does the guardian or safe-stop service act. The API itself never signs anything (K3, K8).

```
POST /og/api/fleet/command            -> 202 {proposal_id, summary, expires_in_s}
POST /og/api/fleet/command/{id}/confirm
```

- A proposal lives **60 seconds** and is consumed by a successful confirm. It is held in memory in a single `og-api`
  process, so a restart also expires it; propose again.
- Confirm writes the decision to the audit trace first, then waits for the independent guardian's verdict.

| Confirm result | Meaning |
|---|---|
| `200` | Guardian `PASS`; body has `outcome`, `vetoed_rule_ids`, `trace_id` |
| `409` | Guardian did not pass (`VETOED`, `PARTLY_VETOED` or `TIMEOUT`); the same body is in `detail` |
| `503` | No verdict within the wait window: `og-guardian` may not be running. Never treated as a pass |
| `410` | The proposal expired |
| `404` | Unknown proposal, or a proposal of the other kind |

Safe stop follows the same two steps (`POST /og/api/safestop`, then `/{proposal_id}/confirm`), returning `503` if no
`og-safestop` ENGAGE row appears in time.

Release needs **two different operators** (K8). The stop-only key in `og-safestop` can never release, so only the
guardian signs a release, and only after a second operator approves it:

```
POST /og/api/safestop/{scope}/{scope_id}/release        -> 202 {proposal_id}   (operator A; nothing released yet)
POST /og/api/safestop/release/{proposal_id}/approve     -> 200 released, 202 still pending
```

- The approver must not be the requester (`403`).
- Approval writes one `og.operator_action` (`SAFE_STOP_RELEASE`, tier `TIER2`) recording both operators. The guardian
  checks it (allow-list, distinct operators, age, scope still engaged) before signing, and `og-safestop` relays it.
- `202` from approve means the guardian has not released yet. It may refuse, for example on an empty allow-list, and
  the API never fakes a release.

## Server-sent event streams

Each stream polls its read model on a fixed cadence and emits an SSE `message` event whose `data` is JSON. Nothing is
sent on a tick where the read model did not change. A comment heartbeat goes out every `[api].sse_heartbeat_s` (15 s)
so proxies keep the connection open.

| Stream | Cadence | Feeds |
|---|---|---|
| `GET /og/api/stream/control-room` | 2 s | Control room KPIs |
| `GET /og/api/stream/dispatch` | 2 s | Dispatch pipeline |
| `GET /og/api/stream/health` | 2 s | Process and feed health |
| `GET /og/api/stream/alerts` | 2 s | Alert list |
| `GET /og/api/fleet/stream` | 1 s | Sampled fleet state |

Reconnect on drop; the first event after connecting carries the current state.

## Error codes

| Code | Raised when |
|---|---|
| `401` | `X-Remote-User` missing |
| `403` | Identity has no role, `operator` role needed, health probe from a non-loopback host, a CSRF check failed, or a release approved by its own requester |
| `404` | Unknown hub, bank, contract or proposal (any `LookupError`) |
| `409` | Admission or reservation refused: `detail.reason_code` holds the code (for example `R-COMMIT-LOCK-INFEASIBLE`); or a guardian veto on confirm |
| `410` | A proposal expired |
| `422` | Body or query failed validation, including a schema-validation failure |
| `503` | A required dependency or process is unavailable (no verdict, no stop event, not configured) |

Reason codes are defined in `orchestrator/src/opengrid/core/reasons.py`; the commitment-lock ones are explained in
`docs/orchestrator/07-delivery/00-invariants.md` (K13).
