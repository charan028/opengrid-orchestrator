# `opengrid.api`

Process `og-api` (02b-mvp-s-spec-platform.md §7). FastAPI app bound to `127.0.0.1:8080`, reached only
through the Apache reverse proxy (`deploy/README.md`). Owns the read models behind every UI screen,
the SSE streams, and the operator-only mutating endpoints (fleet command, safe stop, scenario trigger,
customers/contracts/opportunities CRUD, retention policy).

## Interface

- `opengrid.api.create_app() -> FastAPI` -- the fixed public entry point (`orchestrator/INTERFACES.md`).
- `python -m opengrid.api [--config path]` -- process entry point, runs `uvicorn`.

## Layout

- `app.py` -- builds the app: lifespan (config, DB pool, store, trace store, proposal store), exception
  handlers, router registration, `opengrid.ui` mount (if present).
- `auth.py` -- `X-Remote-User` -> role (`operator`/`viewer`) via `[api.roles]` config, plus the
  loopback-only gate for `/og/api/health`.
- `store.py` -- `PgStore`: every read model as a plain query against tables owned by other modules
  (`contract`, `opportunity`, `obligation`, `hub`/`hub_state`, `feed_obs`, `forecast`, `pnl`,
  `invoice_line`, `trace`, `heartbeat`, `alert`, ...), plus the handful of writes `api` owns directly
  (contract CRUD, retention policy, alert ack, `operator_action`).
- `trace_backend.py` -- a `opengrid.trace.TraceBackend` implementation for `api`'s own trace writes
  (every mutating endpoint records `operator_action` + `trace`); does not duplicate hash-chain logic,
  only the Postgres I/O adapter `opengrid.trace.store.TraceStore` already expects.
- `proposals.py` -- the in-memory two-step confirmation store (60 s TTL) for manual fleet commands and
  safe-stop engagement (02b §7.3).
- `sse.py` -- polling-generator helper wrapping `sse_starlette.EventSourceResponse` (its `ping`
  parameter supplies the required comment heartbeat).
- `routers/` -- one module per endpoint group / UI screen (`health`, `fleet`, `markets`, `dispatch`,
  `profitability`, `billing`, `contracts`, `retention`, `safestop`, `scenario`).

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
