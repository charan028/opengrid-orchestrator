# Lane: UX, performance tooling and demo (assignee: fancyviper007)

You and your AI agent own work packages **DOC1, U1, Q3, DEMO** from `WORKBOARD.md`. Edit only their paths. Only
the lead deploys and merges to `main`.

## 0. Setup (30 min)

1. Clone `https://git.tocy-net.net/Tocy-Net/opengrid-orchestrator` (HTTPS with your Gitea account).
2. Python 3.13. From the repo root:
   `python -m venv .venv && .venv/bin/pip install -e orchestrator[dev] -e integration-sims[dev]`
   (Windows: `.venv\Scripts\pip`). If the extras are not defined, install from each `pyproject.toml`. Docker is
   needed for the dev stack in `dev/`.
3. Check the baseline: `cd orchestrator && make check`.
4. Give your AI agent these files first: `CONTRIBUTING.md`, `BUILD.md`, `orchestrator/INTERFACES.md`,
   `docs/orchestrator/07-delivery/00-invariants.md`, `docs/orchestrator/07-delivery/02b-mvp-s-spec-platform.md`
   (§7 API, §8 UI), `docs/orchestrator/07-delivery/04-mvp-s-test-plan.md` (§4 non-functional, §5 demo script),
   `WORKBOARD.md` and this file.
5. You may LOOK at the live system at `https://base.tocy-net.net/og/` with the **viewer** credentials from the
   lead. Never run destructive tests there.

## 1. DOC1: operator guide (start now; no dependencies)

- Path: `docs/operator/`.
- For a control-room operator:
  - what each of the 7 screens shows;
  - the two-step actions (manual command, scoped safe stop): when to use them and what the countdown means;
  - what each alert means and what to do (read `orchestrator/src/opengrid/health/` for the rules);
  - the degraded modes (feed stale → no new commitments; engine down → hubs hold, then go local);
  - the commitment lock and why a better price does not move a committed delivery;
  - where the audit trail is and how to verify it.
- Screenshots come from the live system (viewer role) or the dev stack.

## 2. U1: UI validation and polish (after the dev stack `dev/` lands)

- Paths: `orchestrator/src/opengrid/ui/` (you own it now) and `tests-e2e/ui/`.
- A scripted walkthrough (Playwright for Python) of all 7 screens plus the fleet two-step flows, against the dev
  stack. It checks:
  - every value shows its age;
  - the viewer role hides write actions;
  - live updates arrive within 2 s;
  - Markets and Profitability poll every 30 s;
  - the confirm dialogs work with the keyboard.
- Accessibility: labels, WCAG AA contrast (check the og.css tokens), focus order, Escape closes dialogs.
- Fix defects you find in templates/routes. The UI talks to the API only over HTTP (`api_client.py`); API changes
  go in your PR description for the lead.

## 3. Q3: performance and failure tooling

- Paths: `tests-e2e/perf/`, `tests-e2e/chaos/`.
- Perf: drive the fleet sim at 2k (local) and 10k (the lead runs it on the server). Capture the allocator cycle p99
  (target < 500 ms at 2k) from `/metrics` (Prometheus format) and UI event-to-screen latency (≤ 2 s); output a
  short report.
- Chaos: a script that kills each of the 7 og-* processes in turn (and Postgres/Mosquitto) and asserts the expected
  behaviour from test plan §4 (e.g. og-guardian down → hubs hold the last setpoint until lease expiry; og-safestop
  independent of og-engine). Parametrise it so it runs against the dev stack (docker) or the server (systemd, run
  by the lead).

## 4. DEMO: demo script and scenario pack

- Paths: `docs/demo/` and `integration-sims/scenarios/demo-*.yaml`.
- A 20-step sign-off walkthrough (test plan §5), covering in order:
  1. live feeds;
  2. the fleet;
  3. several customers committed;
  4. a price spike during delivery (the lock holds);
  5. a SCADA overload → DIST_DEFERRAL response + alert;
  6. zone comms loss → substitution;
  7. a forged command rejected;
  8. a scoped safe stop;
  9. profitability and billing;
  10. audit verify.
- Each step: the action, what to show on which screen, and the expected result. Scenario YAMLs make it repeatable.

## Rules that matter

- No duplicated functions (`tools/dupcheck.py`); no `ogsim` ↔ `opengrid` imports.
- No secrets anywhere: env-variable names only.
- Small PRs, one WP each, branch `wp/<id>-<slug>`. Put the `make check` output in the PR.
