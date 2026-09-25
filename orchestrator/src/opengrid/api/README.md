# `opengrid.api`

Process `og-api` (02b-mvp-s-spec-platform.md §7). FastAPI app bound to `127.0.0.1:8080`, reached only
through the Apache reverse proxy (`deploy/README.md`). Owns the read models behind every UI screen,
the SSE streams, and the operator-only mutating endpoints (fleet command, safe stop, scenario trigger,
customers/contracts/opportunities CRUD, retention policy).

## Interface

- `opengrid.api.create_app() -> FastAPI` -- the fixed public entry point (`orchestrator/INTERFACES.md`).
- `python -m opengrid.api [--config path]` -- process entry point, runs `uvicorn`.

## Layout

- `app.py` -- builds the app: lifespan (config, DB pool, store, trace store, proposal store,
  `opengrid.contracts.configure(...)`), exception handlers, router registration, `opengrid.ui` mount
  (if present).
- `auth.py` -- `X-Remote-User` -> role (`operator`/`viewer`) via `[api.roles]` config, plus the
  loopback-only gate for `/og/api/health`.
- `store.py` -- `PgStore`: every read model as a plain query against tables owned by other modules
  (`opportunity`, `hub`/`hub_state`, `feed_obs`, `forecast`, `pnl`, `invoice_line`, `performance`,
  `trace`, `heartbeat`, `alert`, `command_batch`/`verdict`, `stop_event`, ...), plus the handful of
  writes `api` owns directly (retention policy, alert ack, `operator_action`, the manual-command
  `command_batch` header, and `pg_notify` for the safestop request channel). Contract/customer/
  opportunity CRUD is *not* here -- it delegates to `opengrid.contracts` (see below).
- `trace_backend.py` -- a `opengrid.trace.TraceBackend` implementation for `api`'s own trace writes
  (every mutating endpoint records `operator_action` + `trace`); does not duplicate hash-chain logic,
  only the Postgres I/O adapter `opengrid.trace.store.TraceStore` already expects.
- `proposals.py` -- the in-memory two-step confirmation store (60 s TTL) for manual fleet commands and
  safe-stop engagement (02b §7.3).
- `sse.py` -- polling-generator helper wrapping `sse_starlette.EventSourceResponse` (its `ping`
  parameter supplies the required comment heartbeat).
- `routers/` -- one module per endpoint group / UI screen (`health`, `fleet`, `markets`, `dispatch`,
  `profitability`, `billing`, `contracts`, `retention`, `safestop`, `scenario`).

### Cross-process write paths (no signing key lives in `og-api`)

`api` never signs anything and never configures a `GuardianService`/`SafestopService` (K3/K8 keep those
singletons in their own processes only):

- **Manual fleet command** (`routers/fleet.py`): confirm writes the K10 decision pre-image as a
  `decision_type="RT_ALLOCATION"` trace row shaped exactly as `opengrid.guardian.repo.PgProposalPort`
  reads it back, plus a `command_batch` header row, then polls `og.verdict` for the independently
  running `og-guardian` process's PASS/VETOED/TIMEOUT result. No verdict within the poll window is a
  `503`, never an assumed pass.
- **Safe stop** (`routers/safestop.py`): PROPOSE/CONFIRM are sent as `pg_notify` messages on
  `opengrid.safestop.pg_backend.REQUEST_CHANNEL` (the exact protocol that module's own docstring
  documents for "og-api (once built)"); confirm then polls `og.stop_event` for `og-safestop`'s ENGAGE
  row. `release()` always fails closed from `og-safestop`'s stop-only key (K8); since no agent has
  built the guardian's Tier-2 co-sign path yet, the release endpoint records the attempt and reports
  `501` rather than fabricating a signature.
- **Contracts/opportunities** (`routers/contracts.py`, `routers/dispatch.py`): call
  `opengrid.contracts.{admit,get_contract,list_contracts,list_customer_ids,create_contract,
  set_contract_status}` directly. Unlike guardian/safestop, `opengrid.contracts` holds no signing key,
  so `api`'s lifespan configures its own `PgContractsRepo`/`TraceStore` pair for it (BUILD.md §1 "no
  duplicated functions" -- one write implementation, reused, not re-derived).

## Response shapes are pinned to what `opengrid.ui` already consumes

`opengrid.ui` (ui-a/ui-b) was built in parallel against 02b §7.1's endpoint list without a byte-level
schema, so several read endpoints' exact shape is fixed by what their route modules actually parse
(each cited below), not just the prose in the spec table:

- `GET /og/api/health` -- `processes` is a dict keyed by process name (not a list), `feeds`/`alerts`
  are full lists, and it carries the Control room's own KPIs (`fleet_mw`, `active_commitments`,
  `net_margin_usd`, `reserve_breaches`, `double_sold_kwh`, `commitment_switches`, `as_of`) as
  top-level fields -- `opengrid.ui.routes.health`/`control_room` read all of this from one call.
- `GET /og/api/fleet/hubs` -- `{"items": [...]}`; `GET /og/api/fleet/hubs/{id}` -- a flat hub+state
  dict (`hub_id`/`bank_id`/`zone`/`soc_kwh`/`p_kw`/`health`/`lease_epoch`/...), not the nested
  `{"hub", "state", "telemetry_sparkline"}` an earlier revision of this router returned --
  `opengrid.ui.routes.fleet`/`templates/_partials/hub_drilldown.html`.
- `GET /og/api/dispatch/opportunities` -- `Obligation` rows (not `Opportunity` rows): the Kanban's
  committed/delivering/fulfilled stages are `Obligation.state`, `Opportunity.state` never reaches them
  (02a S1.4 vs S1.5) -- `opengrid.ui.routes.dispatch.pipeline_view` reads `committed_qty_kw`/`tier`/
  `at_risk`/`last_reason_code`, all `Obligation` fields.
- `GET /og/api/ledger/{bank_id}/timeline` -- `{"reservations", "grants", "commitments",
  "bank_capacity_kw"}`, not a bare reservation list -- `opengrid.ui.routes.dispatch.dispatch_page`.
- `GET /og/api/billing/invoice-lines` -- `{"lines": [...], "performance": [...]}` for `format=json`
  (the M&V performance rows for the same period, embedded per ui-b's own documented assumption in
  `billing_audit.py`); `format=csv` is unchanged, still the raw invoice-line CSV.
- `GET /og/api/trace/events` -- filters on query param `class` (mapped internally to `event_class`).
- `POST /og/api/trace/verify` -- JSON body `{"class", "from", "to", "stream_id"}` (all optional; a hash
  chain is verified whole-stream, so `class`/`from`/`to` are accepted but not used to filter what gets
  checked -- see the endpoint's docstring), response `{"passed", "checked", "first_broken"}`.

`GET /og/api/markets/series`, `/dispatch/opportunities` (list vs. `{"items": [...]}`), and
`/profitability/summary` were left as plain lists: `opengrid.ui`'s own parsing already accepts either
shape for those three (`raw.get("items", raw) if isinstance(raw, dict) else raw or []`), so no change
was needed there.

**Known gap, not `api`'s to fix:** `opengrid.core.models.engine.Grant`/`Reservation.bank_id` are typed
`UUID` (matching `og.reservation`/`og.grant`'s `uuid` columns), while the fleet twin's bank identifiers
are text codes like `"bank-01"` (`og.bank.bank_id TEXT`, 02b S4.2) -- confirmed against real Postgres:
querying `og.reservation` with a text `bank_id` like `"bank-01"` raises
`psycopg.errors.InvalidTextRepresentation`. `PgStore._fetch_by_uuid_bank_id` guards this by returning
an empty timeline for a non-UUID `bank_id` instead of a `500` (a real bank will simply show no ledger
rows today); once the two identifier spaces are reconciled -- migrations are architect-owned, the
ledger/allocator agent owns `og.reservation`'s writes -- this guard becomes unreachable and can be
deleted.

## Auth

Every endpoint requires `X-Remote-User`, mapped to a role via `role_for_identity()` (defaults: the
identity `operator`/`viewer` map to themselves; `[api.roles.operator]`/`[api.roles.viewer]` in
`orchestrator.toml` can list additional named accounts). `viewer` reads; only `operator` writes.
`/og/api/health` is the one exception -- it is gated on the connection being loopback instead, so
`deploy/scripts/deploy.sh`'s direct, unauthenticated poll works.

## Testing

- Unit (`tests/unit/api/`): `TestClient`, no DB -- `app.dependency_overrides` replaces `get_store`,
  `get_trace_store`, `get_config`, `get_proposals` with fakes. Covers role enforcement, every REST
  endpoint's shape, the two-step confirm flow (propose -> confirm/expire/wrong-kind), and SSE framing.
- Integration (`tests/integration/api/`): real Postgres `og_t_api` (via `tools/remote.ps1 -Ws api`),
  migrated schema, exercises `PgStore`/`PgTraceBackend` against real rows.

```
D:\Projects\OpenGrid\opengrid-orchestrator\.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\api -q
```
