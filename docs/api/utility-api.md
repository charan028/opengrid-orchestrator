# Utility customer API (decision D-33)

A utility issues and follows its own toll calls under `/og/api/customer/v1/utility/`. Austin Energy is the only
enabled utility today. A toll is a `REGULATED_CAPACITY` obligation whose contract variant is `TOLLING` (D-29). It
is held at 0 kW until the utility calls it, it can be called for discharge only, and it runs at most for the
product's 90 minutes inside the daily reservation window (`[contracts.tolling]`, 16:30-18:00 America/Chicago).

Hand-maintained. `reference.md` and `openapi.json` are generated and currently list no `/og/api/customer/...` route.
This page describes r3.4.3: the call status reports measured delivery (D-38), a utility may cancel only the calls
it issued, and a timestamp without a UTC offset is refused with 422. What differs in r3.4.2 is listed under
[Differences in r3.4.2](#differences-in-r342). References are `file:line`:

- the call status, measured delivery and the delivery records at `e28db35` (integ/delivery-verify-r343);
- the issuer-only cancel, the ended-call cancel, timezone-aware timestamps and the traced read denials at
  `15e1dcd` (integ/utility-api-r343);
- everything else at `897965f` (main, r3.4.2).

Paths are relative to `orchestrator/` unless they start with `deploy/`.

## Identity, role and utility_id

1. **Apache** authenticates the account (Basic Auth). For `/og/api/customer/` it admits only the customer
   accounts and `og-util-aen` (`deploy/apache/opengrid.conf:39-42`). It then forwards `X-Remote-User` together with
   the proxy secret `X-OG-Proxy-Auth`. og-api believes the name only with that secret. Without the header, or
   without a matching secret, og-api answers `401` (`src/opengrid/api/auth.py:87-122`).
2. **Role.** An identity gets the role `utility` when it is a key of `[api.roles.utility]`. Operator, viewer and
   customer mappings are checked first (`src/opengrid/api/auth.py:68-84`).
3. **utility_id.** The same table maps the account to the `og.utility.utility_id` it acts for. A value that does not
   match `^[A-Z][A-Z0-9_]{1,63}$` maps to nothing (`src/opengrid/customer_api/utility_identity.py:33,42-51`).
4. **Enabled utilities.** `[api.utility_api].enabled_utilities` is a fail-closed allow-list: an absent key means
   no utility (`utility_identity.py:54-58`).
5. **Policy.** `config/authz.toml:95-111` allows `utility.read` (`me`, `obligations`, the `calls` reads and the
   delivery records), `utility.call` and `utility.cancel` to the role `utility` only. The policy denies by default.
   Every refusal of a utility action, reads included, is traced as `AUTHZ_DENY` on stream `authz_deny:<user>`
   (`utility_identity.py:62-103` and `config/authz.toml:99-102` at `15e1dcd`).
6. **Mounting.** The routes exist only while `[api.customer_api].enabled` is true. The code default is false; the
   shipped config sets true for D-33 (`src/opengrid/api/app.py:198-212`). While it is off, every path below answers
   `404`.

Shipped configuration (an excerpt with shortened comments; `config/orchestrator.toml:304-328`):

```toml
[api.roles.utility]
"og-util-aen" = "AUSTIN_ENERGY"
"og-util-lcra" = "LCRA"          # D-37: mapped, not enabled; no Apache account exists
"og-util-rayburn" = "RAYBURN"    # D-37: mapped, not enabled; no Apache account exists

[api.utility_api]
enabled_utilities = ["AUSTIN_ENERGY"]

[api.customer_api]
enabled = true

[dispatch.calls]
max_calls_per_hour = 30
max_calls_per_day = 200
ramping_fraction = 0.9
```

The utility dependency answers `403` before it looks at any resource (`utility_identity.py:61-85`):

| `detail` | Cause |
|---|---|
| `No role mapped for identity '<user>'` | The account is in no role table |
| `utility role required` | Another role (operator, viewer, customer) or a policy deny |
| `no utility_id mapped for this identity` | A malformed utility_id in `[api.roles.utility]` |
| `the utility API is not enabled for this utility` | The utility_id is not in `enabled_utilities` (LCRA, RAYBURN today) |

A utility identity gets `403` on the operator and viewer endpoints. The `api.read` and `api.write` rules have no
`utility` role (`config/authz.toml:18-29`).

A client that calls og-api directly, without Apache (tests, the dev stack), sends the same two headers
(see [auth-and-actions.md](auth-and-actions.md)):

```
X-Remote-User: og-util-aen
X-OG-Proxy-Auth: <OG_API_PROXY_SECRET>
```

## Isolation: 404, never 403

Every read and every call is scoped to the caller's `utility_id`. Nothing in a request can name another utility.

- An obligation of another utility, or a free-market one, answers `404` with `R-CALL-NOT-FOUND` and
  `"no such obligation"`, exactly like an unknown id (`src/opengrid/calls/service.py:132-137`).
- A call recorded against another utility answers `404` with `"no such call"` on read and on cancel, exactly like
  an unknown id (`service.py:336-345`).
- `obligations` and the call history are filtered by `utility_id` in SQL (`src/opengrid/calls/pg_store.py:48-59`,
  `pg_store.py:281-299`).
- A reach into another utility's obligation or call is also traced as `AUTHZ_DENY` on stream
  `authz_deny:<user>`, with action `dispatch.call` (`service.py:141-149`). The caller cannot tell it from a
  missing id.

Reads are scoped to the utility_id, not the account. So the history also lists calls that others recorded against
your utility, for example an operator's call on your toll from the Dispatch screen (origin `OPERATOR`), and you can
read each of them (`service.py:174`, `service.py:340`). Cancelling or shortening is limited to the issuer: a call you
did not issue (another origin or another account) answers `403` `R-CALL-NOT-ISSUER`, is traced as `AUTHZ_DENY`
(action `dispatch.call.cancel`) and stays as it is. An operator may end any call (`service.py:349-366,385` at
`15e1dcd`).

## Endpoints

All paths are under `/og/api/customer/v1/utility` (`src/opengrid/customer_api/utility_routes.py:48-49`). Errors that
come from the call core have the body `{"detail": {"reason_code", "detail", "call_id"?}}`. The `call_id` is present
when the refusal was recorded (`src/opengrid/api/call_errors.py:12-16`). A schema error answers `422` in FastAPI's
list format, `{"detail": [...]}`.

### `GET /me` (utility.read)

`200 {"user": "og-util-aen", "utility_id": "AUSTIN_ENERGY", "api_version": "v1"}` (`utility_routes.py:106-108`).

### `GET /obligations?days=2` (utility.read)

`days` is 1-14 (default 2; outside that range: `422`). The response lists your tolling obligations whose window
starts between today 00:00 America/Chicago and `days` days later, in any state, ordered by window start
(`utility_routes.py:111-132`, `pg_store.py:48-59`). `today` is the first of them whose window starts today, or
`null`.

```json
{
  "utility_id": "AUSTIN_ENERGY",
  "obligations": [
    {
      "obligation_id": "<uuid>", "utility_id": "AUSTIN_ENERGY",
      "service_type": "REGULATED_CAPACITY", "variant": "TOLLING", "state": "COMMITTED",
      "committed_kw": 24000.0, "max_call_minutes": 90,
      "window_start": "<ISO 8601, 16:30 CT>", "window_end": "<ISO 8601, 18:00 CT>",
      "held_kw": 0.0, "active_call_id": null
    }
  ],
  "today": {"obligation_id": "<uuid>", "...": "same fields"}
}
```

Values are illustrative. `max_call_minutes` is the obligation's product-rule duration. `held_kw` is `0.0` while no
call is active (D-29). While a call is active, `held_kw` is that call's target (signed, negative) and
`active_call_id` is its id. "Active" means deployed, not cancelled and not yet ended, so a call that is scheduled
but not started also counts (`utility_routes.py:89-103,121-125`). Only `COMMITTED`, `DELIVERING` and `SHORTFALL`
obligations can be called (`src/opengrid/calls/rules.py:41`).

### `POST /calls` (utility.call)

The body (`utility_routes.py:63-73`):

| Field | Type | Rule |
|---|---|---|
| `kw` | number, required | Signed: +charge / -discharge. A call is discharge only, so `kw` must be `< 0`. Its magnitude is at most the obligation's committed kW (0.001 kW tolerance) |
| `duration_minutes` | integer, required | 1-240 by schema. The obligation's product rule is the real cap (TOLLING 90) |
| `idempotency_key` | string 1-200, required | Unique per account. Resending it with the same request returns the original call |
| `start_at` | ISO 8601 with a UTC offset | Default now. A start in the past begins now and keeps its own end (`start_at + duration`) (`rules.py:91-99`) |
| `obligation_id` | UUID | Default: your deployable toll whose window covers the start (`pg_store.py:36-46`) |
| `reason` | string 1-200 | Default `"utility toll call"`. It is stored with the prefix `utility call: ` (`service.py:238-241`) |

Every timestamp must carry a UTC offset (`Z` or `-05:00`): `start_at` here, `end_at` on cancel and `since` on the
history. The request schema declares them timezone-aware, so a timestamp without an offset answers `422` in
FastAPI's list format ("Input should have timezone info"), before anything is recorded (`utility_routes.py:71,79,167`
at `15e1dcd`).

Responses (`utility_routes.py:135-160`):

| Status | When |
|---|---|
| `201` | New call accepted. Body: the call status (below) |
| `200` | Same account, same `idempotency_key`, same request: the original accepted call, `"replayed": true`, nothing new deployed (`service.py:95-111`) |
| `404` `R-CALL-NOT-FOUND` | Unknown or another utility's obligation, or (with `obligation_id` omitted) no deployable toll covers the start |
| `409` `R-CALL-NOT-DEPLOYABLE` | The obligation is not a toll |
| `409` `R-CALL-STATE` | The obligation is not `COMMITTED`, `DELIVERING` or `SHORTFALL` |
| `409` `R-CALL-NO-PRODUCT-DURATION` | Its product rule has no duration, so the call cannot be capped |
| `409` `R-CALL-DURATION-CAP` | Longer than the product (TOLLING 90 min) |
| `409` `R-CALL-OVER-COMMITTED` | `-kw` exceeds the committed kW |
| `409` `R-CALL-OUTSIDE-WINDOW` | Starts outside `[window_start, window_end)`, or ends after `window_end` |
| `409` `R-CALL-WINDOW-PASSED` | The call's end is not in the future |
| `409` `R-CALL-OVERLAP` | Another uncancelled call on the obligation overlaps (no chaining). Checked under a per-obligation lock (`pg_store.py:224-260`) |
| `409` `R-CALL-IDEMPOTENCY-CONFLICT` | The key was already used by this account for a different request; `call_id` is the original call |
| `422` `R-CALL-CHARGE-REFUSED` | `kw >= 0` |
| `422` (schema) | A missing or malformed field, including a `start_at` without a UTC offset. Not recorded |
| `429` `R-CALL-RATE-LIMIT` | Over `[dispatch.calls]` limits (below) |
| `403` | Identity (table above) |

The checks are in `rules.py:106-194` and `service.py:62-138`. The order only decides which reason you see first.
Every refusal is recorded as a `REFUSED` call and carries its `call_id`, except the rate limit and the idempotency
conflict. Resending the key of a recorded refusal returns the same refusal with its stored status
(`rules.py:197-207`).

The same function serves the operator's `POST /og/api/dispatch/as-deployments`, the grid link and the ERCOT poller.
The checks are identical for every origin (`service.py:1-11`). An accepted call runs these steps:

1. It is traced (`DISPATCH_CALL` on stream `dispatch_call:<user>`) before it takes effect.
2. In one transaction it writes the `og.as_deployment` row (source `UTILITY`, `requested_by` = the account), the
   `ACCEPTED` ledger row in `og.dispatch_call` and an `og.operator_action` audit row (`UTILITY_CALL:<obligation_id>`)
   (`pg_store.py:224-260`).
3. It raises the operator alert `ALR-UTILITY-CALL` (`service.py:244-313`).

While the call runs, the allocator caps the obligation's discharge at `-kw` (`src/opengrid/engine/gateways.py:347-354`).

### `GET /calls?since=&limit=100` (utility.read)

`{"calls": [...]}`: every call recorded against your utility, accepted and refused, newest first by ledger time
(`created_at`). `since` (with a UTC offset) defaults to 30 days ago. `limit` is 1-500 (`utility_routes.py:163-173`).
The rows are ledger records without `state`; read one call for its state.

### `GET /calls/{call_id}` (utility.read)

`200` with the call status, or `404` `R-CALL-NOT-FOUND` (`"no such call"`) for an unknown call or another
utility's call (`utility_routes.py:176-192`).

### `POST /calls/{call_id}/cancel` (utility.cancel)

The body is required: `{}` cancels now, and `{"end_at": "<ISO 8601 with a UTC offset>"}` shortens the call
(`utility_routes.py:76-79,195-214`; `service.py:369-420` at `15e1dcd`). The checks run in this order:

| Status | When |
|---|---|
| `404` `R-CALL-NOT-FOUND` | Unknown, or another utility's call |
| `403` `R-CALL-NOT-ISSUER` | A call on your toll that you did not issue (an operator's, the grid link's, or another account's). Traced as `AUTHZ_DENY`; nothing changes |
| `409` `R-CALL-ALREADY-ENDED` | The call was refused, is already cancelled, or has already ended |
| `409` `R-CALL-CANNOT-EXTEND` | `end_at` is after the call's current end |
| `422` (schema) | `end_at` without a UTC offset |
| `200` | Cancelled (no `end_at`, or one not after now, or one not after the call's start): `cancelled_at` is set, `state` `COMPLETED`. Shortened (a later `end_at`, not after the call's current end): `end_at` moves, and the state is unchanged until then |

The end is traced (`DISPATCH_CALL_END`) before it takes effect and raises `ALR-UTILITY-CALL` ("cancelled" or
"shortened"). The allocator returns the obligation to its 0 kW hold on its next cycle.

### `GET /delivery-records?since=&until=&result=&limit=100` (utility.read)

`{"utility_id": ..., "records": [...]}`: the measured-delivery records of the calls on your obligations, newest first,
without their per-bucket series. `limit` is 1-500. `result` filters on the verdict (`IN_PROGRESS`, `PASS`,
`PARTIAL`, `FAIL`) (`customer_api/delivery_routes.py:48-61` at `e28db35`).

### `GET /delivery-records/{call_id}` (utility.read)

One record with its per-bucket series (committed, commanded, delivered and meter kW). The record of a utility call
is keyed by the call's `deployment_id`. Another utility's record answers `404` exactly like a missing one
(`delivery_routes.py:36-45,64-68` at `e28db35`, `delivery/store.py:131`).

## The call status

`POST /calls`, `GET /calls/{id}` and the cancel answer all return the ledger record plus its status
(`calls/models.py:173-213` at `e28db35`):

```json
{
  "call_id": "<uuid>", "outcome": "ACCEPTED", "origin": "UTILITY", "principal": "og-util-aen",
  "reason": "utility call: utility toll call", "start_at": "...", "end_at": "...", "duration_minutes": 60,
  "reason_code": null, "detail": null, "deployment_id": "<uuid>", "obligation_id": "<uuid>",
  "utility_id": "AUSTIN_ENERGY", "kind": "UTILITY_CALL", "requested_kw": -20000.0, "committed_kw": 24000.0,
  "idempotency_key": "aen-2026-09-27-1", "trace_id": "<uuid>", "created_at": "...", "cancelled_at": null,
  "replayed": false, "target_kw": -20000.0,
  "state": "DELIVERING",
  "delivered_kw": -19650.0, "delivered_kwh": 812.4,
  "delivery_measured": true, "delivery_state": "IN_PROGRESS", "delivery_reasons": [],
  "meter_status": "CORROBORATED", "delivery_as_of": "...",
  "granted_kw": -20000.0, "granted_kwh": 830.0, "granted_description": "planned; removed in r3.5; use delivered_*",
  "as_of": "..."
}
```

Values are illustrative.

| Field | Meaning |
|---|---|
| `state` | See the table below |
| `delivered_kw` | Measured (D-38): the delivered kW of the latest evaluated 30 s bucket, signed (< 0 = discharge). `null` before the start, until a bucket with telemetry has been evaluated, when the latest bucket had no telemetry, and for a refused call |
| `delivered_kwh` | Measured discharged energy so far, >= 0. `0.0` until something is measured; `null` for a refused call |
| `delivery_measured` | `true` only once a measured value exists: a delivered kW in the latest evaluated bucket, or a final verdict (`calls/models.py`, `MeasuredDelivery.is_measured`). A record alone is not enough: a running call whose first buckets had no telemetry stays `false`, with `state` `ACTIVE` and `delivery_state` `UNMEASURED` |
| `delivery_state` | `UNMEASURED` without a record or while `delivery_measured` is `false`. Otherwise the verification result: `IN_PROGRESS` while the call is verified, then `PASS`, `PARTIAL` or `FAIL` in the final record (`core/delivery.py:39-43`) |
| `delivery_reasons` | Reasons for a result short of `PASS`, provisional while `IN_PROGRESS`: `RAMP_TOO_SLOW`, `SUSTAIN_BELOW_TARGET`, `NO_DELIVERY`, `VETOED`, `STOPPED`, `DATA_STALE`, `ENERGY_SHORT`. Empty without a record |
| `meter_status` | The independent meter check on metered banks (a substation asset's bank, or `[delivery].meter_bank_ids`): `CORROBORATED`, `UNCORROBORATED`, `NO_METER` or `METER_STALE`. `null` without a record |
| `delivery_as_of` | The end of the last evaluated bucket. `null` without a record |
| `granted_kw`, `granted_kwh`, `granted_description` | **Deprecated; removed in r3.5.** The allocator's planned/granted kW and kWh, computed as in r3.4.2 (below) and never metered. `granted_description` reads "planned; removed in r3.5; use delivered_*" (`calls/models.py:26` at `e28db35`). **Move to `delivered_*`** |
| `as_of` | The server time of this status |

`delivery_reasons`, `meter_status` and `delivery_as_of` come from the call's record as soon as og-settle has one,
even while `delivery_measured` is still `false`.

The measured fields come from `og.delivery_record` (migration 0050). og-settle's delivery job writes it
(`[delivery]`: `interval_s` 15, `bucket_s` 30, `telemetry_lag_s` 30; `delivery/job.py:206-263` at `e28db35`). A
30 s bucket aligned to the call's start is evaluated only once it is 30 s old. The first measured `delivered_kw`
therefore appears about a minute into a call, and until then the call is `ACTIVE`. The job keeps verifying a call
until its final record, up to `lookback_s` (1 h) after the call ends.

The state (`calls/service.py:444-484` at `e28db35`):

| `state` | When |
|---|---|
| `ACCEPTED` | Accepted, not started yet. Nothing is measured |
| `ACTIVE` | Running without a measured kW in the latest bucket (`delivered_kw` `null`, `delivery_measured` `false`): no record yet, no bucket with telemetry evaluated yet, or telemetry has gone stale. A call can fall back to `ACTIVE` from `RAMPING` or `DELIVERING` |
| `RAMPING` | Running; measured discharge below `ramping_fraction` × the call's target (`[dispatch.calls]`, 0.9) |
| `DELIVERING` | Running; measured discharge at or above `ramping_fraction` × the target |
| `COMPLETED` | Past the effective end (`end_at`, or `cancelled_at` if earlier), or cancelled before its start |
| `REFUSED` | Never deployed; see `reason_code` and `detail` |

"Running" means from `start_at` until the effective end. A client that lists the states it knows must accept
`RAMPING` and `DELIVERING`, which are new in r3.4.3.

### Differences in r3.4.2

At `897965f`:

- **Call status** (`calls/models.py:22-25,52-61,170-193`, `calls/service.py:444-457`):
  - A running call is always `ACTIVE`: `RAMPING` and `DELIVERING` are never returned.
    `[dispatch.calls].ramping_fraction` is validated and passed to `status_of`, but not used.
  - `delivery_measured` is always `false`, and `delivery_state` is always `"UNMEASURED"`.
  - There is no `delivered_kw`, `delivered_kwh`, `delivery_reasons`, `meter_status` or `delivery_as_of`.
  - `granted_kw`, `granted_kwh` and `granted_description` are the only kW and kWh fields. `granted_kw` is signed:
    the negated sum of the obligation's granted discharge over its banks in the latest allocator cycle, reported
    only if that cycle is at most 10 s before now (or before the call's end once it has ended). `granted_kwh`
    integrates those grants, with gaps capped at 10 s (`calls/pg_store.py:110-129`). These are planned/granted
    values, never metered: a grant the guardian vetoes still counts. `granted_description` reads
    "planned/granted, not measured; measured delivery arrives in r3.4.2".
- **No delivery records:** `GET /delivery-records` does not exist.
- **Cancel** (`calls/service.py:348-396`):
  - A utility may also cancel or shorten a call on its toll that it did not issue, for example an operator's.
    There is no `R-CALL-NOT-ISSUER`.
  - A cancel after the call has ended answers `409` `R-CALL-CANNOT-EXTEND`, not `R-CALL-ALREADY-ENDED`.
- **Timestamps:** `start_at`, `end_at` and `since` are plain datetimes (`utility_routes.py:71,79,167`), so the
  schema accepts them without an offset and nothing answers `422` for that. A `start_at` without an offset fails
  later, inside the handler (`calls/models.py:108-115`).
- **Denials** (`utility_identity.py:61-85`): a refused `utility.read` is not traced, and neither is the `403` for a
  malformed utility_id mapping or for a utility that is not enabled. Only a refused `utility.call` or
  `utility.cancel` of a non-utility identity is traced, because only those rules are `audited`.

A client that must work with both releases should read `delivered_kw` and `delivered_kwh` when the keys are
present. Otherwise it falls back to `granted_*`, knowing those values are planned, not measured.

## Limits

`[dispatch.calls]` limits each account ("principal"), for every origin alike: operators, the grid link, the ERCOT poller and this API
(`rules.py:56-79,186-194`, `service.py:78-87`).

- The shipped limits are at most 30 calls in any rolling hour and 200 in any rolling day. A count is every ledger
  row the account created, accepted or refused (`pg_store.py:217-222`).
- An idempotent replay returns before the check and does not count.
- A call over the limit is refused with `429 R-CALL-RATE-LIMIT`. It is traced and alerted
  (`ALR-UTILITY-CALL-REFUSED`), but not recorded, so it does not use up the budget.
- The config must satisfy `1 <= max_calls_per_hour <= max_calls_per_day`.
- Reads are not limited.

## What operators see

- **Alerts.** `ALR-UTILITY-CALL` (info) fires for every accepted call and for every cancel or shorten.
  `ALR-UTILITY-CALL-REFUSED` (warning) fires on a refusal. An open, unacknowledged alert with the same rule and
  scope is not repeated, so an account's refusals raise one alert until it is acknowledged
  (`service.py:294-333`, `pg_store.py:317-329`).
- **Dispatch screen.** The toll's row in "AS awards & deployment" reads `deployed`, "by UTILITY · og-util-aen ·
  -N kW". This starts when the call is accepted (a scheduled call too) and lasts until it ends or is cancelled.
  **Stop deploy** ends it early. An operator may end any call; the utility may end only the calls it issued.
- **API.** `GET /og/api/dispatch/as-deployments` lists the active deployments with `source` and `requested_by`.
  `GET /og/api/dispatch/calls?utility_id=AUSTIN_ENERGY` is the call ledger, refusals included
  (`src/opengrid/api/routers/dispatch.py:133-152`).
- **Trace.** The stream `dispatch_call:og-util-aen` holds `DISPATCH_CALL`, `DISPATCH_CALL_REFUSED` and
  `DISPATCH_CALL_END`. Out-of-scope attempts are on the stream `authz_deny:og-util-aen`.
- **Measured delivery (r3.4.3).** For viewers, og-api serves `GET /og/api/delivery/records?utility_id=AUSTIN_ENERGY`,
  `GET /og/api/delivery/records/{deployment_id}` (with the per-bucket series) and `GET /og/api/delivery/summary`
  (`api/routers/delivery.py` at `e28db35`). The job also raises `ALR-DELIVERY-RAMP-LATE`, `ALR-DELIVERY-SHORTFALL`,
  `ALR-DELIVERY-NONE` and `ALR-DELIVERY-METER-MISMATCH`. A live shortfall flags the obligation `AT_RISK`
  (`R-DELIVERY-MEASURED-SHORTFALL`) until it recovers (`delivery/job.py:265-311` at `2251392`).
  On the Dispatch screen the toll's row shows the result in the **Delivery** column (result badge, a `METER` badge
  when the meter disagrees, delivered / committed kW); clicking it opens the "Measured delivery" drawer with the
  committed, commanded, delivered and meter-change chart. See [delivery.md](delivery.md).
- **Delivery records for the utility.** `GET /og/api/customer/v1/utility/delivery-records[/{call_id}]`
  (`utility.read`) returns the utility's own calls' delivery records; another utility's record is `404` like a
  missing one (`customer_api/delivery_routes.py`, [delivery.md](delivery.md)).

## Code

| Topic | Where |
|---|---|
| Routes | `src/opengrid/customer_api/utility_routes.py` |
| Identity and utility_id | `src/opengrid/customer_api/utility_identity.py` |
| Delivery records (r3.4.3) | `src/opengrid/customer_api/delivery_routes.py`, `src/opengrid/delivery/` |
| Call core (checks, ledger, status) | `src/opengrid/calls/` (`service.py`, `rules.py`, `models.py`, `pg_store.py`) |
| Unit tests | `tests/unit/customer_api/test_utility_api.py`, `tests/unit/calls/` |
| End-to-end | `tests-e2e/functional/r3/test_utility_api.py` |
| The Austin Energy simulator's client | `integration-sims/src/ogsim/utility_aen/channels/customer_api.py` |
