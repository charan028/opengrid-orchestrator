# tests-e2e/ui -- browser walkthrough of the 7 operator screens (WP U1)

Playwright (Chromium) tests for `opengrid.ui`: the scripted walkthrough of all 7 screens (02b S8), role
gating, polling/SSE mechanics, the two-step confirm dialogs, and accessibility basics including a WCAG AA
contrast check computed from `static/og.css`'s own colour tokens.

## Run locally (fixture mode, no stack needed)

One-time setup (the `opengrid` package is not installed; everything runs with `PYTHONPATH`):

```bash
orchestrator/.venv-local/bin/python -m pip install playwright pytest-playwright
orchestrator/.venv-local/bin/python -m playwright install chromium
```

Then, from the repository root:

```bash
PYTHONPATH=orchestrator/src orchestrator/.venv-local/bin/python -m pytest tests-e2e/ui -q
```

`conftest.py` starts a uvicorn server in a background thread on a free loopback port, mounts
`opengrid.ui.build_router()` at `/og`, and monkeypatches `opengrid.ui.api_client.get_json`/`post_json` to
serve the recorded fixtures from `orchestrator/tests/unit/ui/fixtures/`. `/og/api/stream/*` is stubbed as
a keepalive-only SSE response, so the four tests that need a stream to actually deliver an update are
skipped in this mode (`4 skipped`). The page loads htmx, Alpine.js, ECharts and Leaflet from jsDelivr, so
the browser needs outbound network access; map tiles are aborted by the fixtures.

## Run against the dev stack

```bash
OG_UI_BASE_URL=http://localhost:8080 PYTHONPATH=orchestrator/src \
  orchestrator/.venv-local/bin/python -m pytest tests-e2e/ui -q
```

With `OG_UI_BASE_URL` set the fixture server is not started; the SSE "update within 2 s" tests run, and the
two confirm-step tests (which would really send a manual command / engage a safe stop) are skipped
instead. The propose-only dialog tests still run: a proposal that is never confirmed simply expires.

Lint (same rules as `orchestrator/pyproject.toml`, via `tests-e2e/ui/ruff.toml`):

```bash
cd orchestrator && .venv-local/bin/python -m ruff check src/opengrid/ui ../tests-e2e/ui \
  && .venv-local/bin/python -m ruff format --check src/opengrid/ui ../tests-e2e/ui
```

## Files

| File | What it covers |
| --- | --- |
| `conftest.py` | fixture server / live URL switch, `operator_page` and `viewer_page` (set `X-OG-Role`) |
| `screens.py` | the 7 screen paths and `<h1>`s, live/polling screen lists, `goto_ok` |
| `test_walkthrough.py` | every screen: 200, `<h1>`, nav `aria-current`, every dated value ticked to `age: Ns`, hub drill-down age |
| `test_roles.py` | viewer sees no write action and gets 403 on a forged POST; operator sees all of them |
| `test_polling_and_live.py` | `hx-trigger="every 30s"` on Markets/Profitability; SSE subscription + `live` badge; update-within-2 s (live stack) |
| `test_confirm_dialogs.py` | manual command + scoped safe stop: focus on Cancel, Tab to confirm, Escape/Cancel return focus, result badge |
| `test_a11y.py` | structure, labels, unique ids, modal dialog role, WCAG AA contrast per theme |
| `contrast.py` / `test_contrast.py` | pure WCAG luminance/contrast/`color-mix` helpers and the `data-theme` token parser |

## Accessibility checklist (for the PR description)

Result of `test_a11y.py` in fixture mode after the fixes listed below.

| Item | Result | Notes |
| --- | --- | --- |
| Every `<input>`/`<select>` has an associated label (wrapping or `for=`) | PASS | all 7 screens, operator role (shows every form) |
| Exactly one `<h1>` per page | PASS | `base.html` `header_title` block |
| Every `<table>` has header cells | PASS | `_partials/data_table.html` always emits `<th scope="col">` |
| `<html lang>` present | PASS | `lang="en"` |
| Exactly one `<main>` and a labelled `<nav>` | PASS | |
| Ids unique on every page, including after a confirm result renders | PASS | fixed: confirm button now `hx-swap="outerHTML"` (the result fragments carry the target's id) |
| Dialog has a modal dialog role, `aria-modal="true"`, `aria-labelledby` resolving to its title | PASS | role is `alertdialog` (the confirm-flavoured subclass of `dialog`) |
| Dialog opens with focus on Cancel; Tab reaches the confirm button | PASS | |
| Escape and Cancel close the dialog and return focus to the trigger | PASS | fixed: an already-open dialog (no trigger of its own) now returns focus to the step-1 submit button instead of `<body>` |
| WCAG AA 4.5:1 for every text token on every surface, dark theme | PASS | fixed: `--status-critical` `#e34948` -> `#ee6160` (was 4.15 on `--panel-2`, 4.05 on the safe-stop fill) |
| WCAG AA 4.5:1 for every text token on every surface, light theme | PASS | lowest pair: `--status-caution` on `--bg` = 4.59 |
| Focus ring >= 3:1 against every surface (WCAG 1.4.11), both themes | PASS | fixed: light theme now defines `--focus-ring: #1861c7` (inherited `#7cc4ff` was ~1.7:1) |
| Status conveyed by colour + icon + text, never colour alone | PASS (by construction) | `_partials/status_badge.html`; not re-asserted here, covered by `tests/unit/ui/test_partials.py` |

Known gaps, deliberately not addressed in this WP: the confirm dialog does not trap Tab inside itself
(Tab from the confirm button leaves the dialog); non-text contrast of decorative panel borders is below
3:1 in both themes (borders are not the only boundary cue, so 1.4.11 does not require it).
