# Lane: Correctness and test depth (assignee: rpagaria2000)

You and your AI agent own work packages **P1, Q1, Q2, DOC2** from `WORKBOARD.md`. Edit only their paths. Only the
lead deploys and merges to `main`.

## 0. Setup (30 min)

1. Clone `https://git.tocy-net.net/Tocy-Net/opengrid-orchestrator` (HTTPS with your Gitea account).
2. Python 3.13. From the repo root:
   `python -m venv .venv && .venv/bin/pip install -e orchestrator[dev] -e integration-sims[dev]`
   (Windows: `.venv\Scripts\pip`). If the extras are not defined, install from each `pyproject.toml`.
3. Check the baseline: `cd orchestrator && make check`. It must pass before you start.
4. Give your AI agent these files first: `CONTRIBUTING.md`, `BUILD.md`, `orchestrator/INTERFACES.md`,
   `docs/orchestrator/07-delivery/00-invariants.md`, `docs/orchestrator/07-delivery/04-mvp-s-test-plan.md`,
   `WORKBOARD.md` and this file.

## 1. P1: property tests for K1–K13 (start now; no dependencies)

- Path: `orchestrator/tests/property/` (new). Use Hypothesis.
- One or more property tests per invariant, named `test_k<n>_<what>` and referencing the test-plan ID in the
  docstring.
- Gaps to fill are listed in `qa/review-*.md` and `qa/security-review.md` (e.g. K1 reserve floor under sustained
  discharge; K2 one buyer across obligations; K6 command freshness; K11 chain verifies after random prune; K13 lock
  under random call arrivals and prices).
- Test the public interfaces only (`opengrid.core`, ledger, allocator, guardian checks, trace store). Do not modify
  product code; if you find a bug, put a failing test in your PR, mark it `xfail(strict=True)` with the reason, and
  describe it in the PR.
- Done when: every K1–K13 has at least one property test, `make check` is green, and the PR lists which Ks gained
  coverage.

## 2. Q1: end-to-end dispatch and commitment lock (after the dev stack `dev/` lands)

- Path: `tests-e2e/functional/dispatch/`. Run against `make dev-up` (see `dev/README.md`).
- Scenarios (test plan TS-04/05), each one pytest test:
  - several customers get commitments in one gate;
  - a better-paying call arrives during delivery → the committed obligation is unchanged (K13);
  - each L0/L1/L2 exception reduces a commitment only with a reason code;
  - substitution when a hub drops;
  - a re-nomination point allows re-selection;
  - partial take by product rules (min/increment/block);
  - the AS release stays off by default.
- Drive the inputs through the simulators' control plane (`http://localhost:8091`, REST) and the API
  (`/og/api/...`); assert on the API and database state.

## 3. Q2: end-to-end safety and anomaly responses (after `dev/`)

- Path: `tests-e2e/functional/safety/`.
- Scenarios (TS-06/07):
  - a forged or unsigned command is rejected by hubs;
  - a stale lease → hold → local autonomy;
  - a scoped safe stop works with og-engine stopped (K8);
  - SCADA overload → alert + DIST_DEFERRAL response;
  - a stale feed → no new commitments but existing ones continue;
  - zone comms loss → substitution/shortfall with a reason;
  - a price spike doesn't break the lock.

## 4. DOC2: API reference and integration how-to

- Path: `docs/api/`.
- Generate the reference from OpenAPI: run og-api locally and fetch `/og/api/openapi.json`, then render it to
  Markdown.
- Curate: the auth model (roles, two-step confirms), SSE streams, error codes.
- Integration how-to: switching between live ERCOT/EIA/NWS and the market simulator (config only), and the ERCOT
  token flow (`response_type=id_token`; see `docs/orchestrator/07-delivery/05-integrations-guide.md`).

## Rules that matter

- No duplicated functions (`tools/dupcheck.py`); no `ogsim` ↔ `opengrid` imports.
- No secrets anywhere: env-variable names only.
- Small PRs, one WP each, branch `wp/<id>-<slug>`. Put the `make check` output in the PR.
