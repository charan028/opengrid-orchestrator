# opengrid.ui

Server-rendered HTMX + Alpine.js + ECharts + Leaflet (all CDN, no build step), mounted by
`opengrid.api.create_app()` under `[ui].base_path` (`/og`, 02b S1.4/S7/S8).

## Ownership (BUILD.md S4)

- **ui-a** (this file's author): the base contract below -- `templates/base.html`, `templates/_partials/`,
  `static/og.css`, `static/og.js`, `routes/__init__.py` -- plus the Control room, Fleet monitoring &
  control and Health screens.
- **ui-b**: `templates/{dispatch,markets,profitability,billing_audit}*.html` and
  `routes/{dispatch,markets,profitability,billing_audit}.py`, built against the same base contract.

## Base contract for ui-b

**`base.html` blocks** (extend it, override what you need):

- `title` -- `<title>` text.
- `header_title` -- the `<h1>` in the page header.
- `header_actions` -- right-aligned controls/badges next to the header title.
- `content` -- the page body.
- `scripts` -- per-page `<script>` (chart init, `og.sse(...)` subscriptions).

Every `TemplateResponse` context should include `role` (`"operator"` or `"viewer"`, from
`opengrid.ui.role.role_of(request)`) and `is_operator` (`opengrid.ui.role.is_operator(request)`) so
`base.html` and any write-action block can hide itself for a viewer. An optional `degraded: str | None`
key renders a warning banner when a screen's upstream API call failed (`opengrid.ui.api_client`).

**Left navigation** (rendered by `base.html` from `opengrid.ui.templating.NAV_SCREENS`, fixed paths, do
not change without the lead's approval): Control room `/og/`, Fleet `/og/fleet`, Dispatch `/og/dispatch`,
Markets `/og/markets`, Health `/og/health`, Profitability `/og/profitability`, Billing & audit
`/og/billing`.

**Partials** (`templates/_partials/`, `{% include %}` or `{% with ... %}{% include ... %}{% endwith %}`
to pass params):

- `kpi_tile.html` -- params `label`, `value`, `unit`, `severity` (`good`/`caution`/`critical`/`neutral`),
  plus staleness params below.
- `status_badge.html` -- params `status`, `label`. Colour **and** icon **and** text, never colour alone
  (UI-UX spec S5, colour-blind safety).
- `data_table.html` -- params `columns` (`[{key, label, numeric, html}]`), `rows` (`[dict]`),
  `empty_message`, `row_id_key` (adds a `.clickable-row` + `data-row-id` for drill-down rows). A column
  with `html: true` renders its cell unescaped -- only use it with server-rendered partial output (e.g.
  `opengrid.ui.render.render_status_badge`), never with raw user input.
- `confirm_dialog.html` -- the two-step confirmation control (02b S7.3): params `dialog_id`,
  `trigger_label`, `title`, `summary`, `confirm_url`, `confirm_label`, `variant`
  (`primary`/`danger`/`safestop`), `target`, plus `expires_in_s` (shows a live countdown and disables the
  confirm button at 0), `open_default`/`show_trigger` (a UI route renders this fragment as the direct
  result of a step-1 propose call with `open_default=true, show_trigger=false` so the dialog appears
  already open, instead of behind its own trigger button, per the data path above). `variant="safestop"`
  is the visually distinct, hard-to-hit scoped safe-stop button style (`.btn-safestop` in `og.css`).
  Opening moves focus to Cancel; closing (Escape or Cancel) returns focus to the trigger button, or,
  when rendered already open, to the element that had focus when it appeared (the step-1 submit button).
- `stale_badge.html` -- params `age_s`, `since_iso` (ISO-8601, preferred: lets `og.js` tick the age live
  client-side), `stale_after_s`. **Every value on every screen must include one of these** (BUILD.md UI
  brief); `tests/unit/ui/test_static_staleness.py` enforces it statically.

**JS helpers** (`static/og.js`, loaded once by `base.html`, exposed as `window.og`):

- `og.sse(url, handler, opts?)` -- subscribes to an SSE endpoint (02b S7.2), calls `handler(data, event)`
  per message (JSON-parsed when possible), supports `opts.onError`/`opts.onOpen`.
- `og.chart(el, option)` -- inits/updates an ECharts instance on `el` (selector or element), resizes on
  window resize.
- `og.formatAge(ageS)`, `og.initStaleBadges(root?)` -- client-side tick for `[data-since]` elements.
- `og.toggleTheme()` -- flips `data-theme` on `<html>` between `dark` (default) and `light`, persisted to
  `localStorage` per-viewer only.

**Data path.** Screen routes fetch their first-paint data from `opengrid.api`'s own REST endpoints via
`opengrid.ui.api_client.get_json(path)` (a plain HTTP call to `og-api`, never an import of
`opengrid.api` internals -- the UI and API are separate agents' work that meet only at that HTTP
boundary). Live updates after first paint are the browser's job: subscribe with `og.sse()` directly to
the `/og/api/stream/*` paths (02b S7.2) and update the screen's own widgets (KPI tile text, table rows,
`og.chart()` instances) from the JSON payload -- the SSE payloads are generic `{"event": "message",
"data": <json>}` frames (`opengrid.api.sse.poll_stream`), not pre-rendered HTML, so this is plain JS, not
`htmx-ext-sse` swaps.

Mutating actions are two-step confirmations *mediated by the UI's own routes*, not `hx-post` calls
straight from the template to the API (that was the original, incorrect design -- a screen route now
sits between the browser and `opengrid.api` for every write): a `<form>` posts to a UI-owned
`.../propose` route (e.g. `POST /og/fleet/safestop/propose`, `POST /og/fleet/command/propose`), which
calls the real API endpoint server-side via `opengrid.ui.api_client.post_json` (`POST /og/api/safestop`,
`POST /og/api/fleet/command`) and renders `_partials/confirm_dialog.html` populated with the *real*
`proposal_id`/`summary`/`expires_in_s` the API returned, already open. Only that dialog's own confirm
button then `hx-post`s to a second UI-owned route (`POST /og/fleet/safestop/{proposal_id}/confirm`, ...),
which relays the confirmation (`POST /og/api/.../{proposal_id}/confirm`) and renders a pass/veto/
timeout/expired result fragment (`status_badge.html` + text, never colour alone). `opengrid.ui.api_client`
is the one shared HTTP client for both reads and writes (`get_json`/`post_json`/`get_bytes`); no screen
keeps its own private copy or a bare `httpx.AsyncClient`. See `routes/fleet.py` and `templates/fleet.html`
for the reference implementation, and `routes/billing_audit.py`'s `run_chain_verify` for a
single-step-but-still-server-mediated write (chain-verify has no proposal step since it is read-only in
effect).

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\ui -q
```

Tests build a standalone `FastAPI()` app with `opengrid.ui.build_router()` mounted at `/og` and a
`TestClient` against it, monkeypatching `opengrid.ui.api_client.get_json` to return recorded JSON
fixtures (`tests/unit/ui/fixtures/*.json`) instead of calling a live `og-api` process.

`ruff check`/`ruff format --check` and `mypy` (default strictness) are required for this package
(BUILD.md S5a): `python -m ruff check src/opengrid/ui` / `python -m mypy src/opengrid/ui`.
