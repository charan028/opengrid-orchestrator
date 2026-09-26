# Work Board: MVP-S (target: Saturday 2026-09-26 18:00 CT)

Status: OPEN / CLAIMED (name) / IN REVIEW (PR #) / DONE. One owner per WP. The owned paths are exclusive while the WP
is claimed. Only the **Lead** deploys and merges to `main`.

## Team

| Who | Lane | Work packages |
|---|---|---|
| **Lead** (repo owner + build agents) | Integration, deployment, server, live verification, diagrams | L1–L4, D0, DOC3 |
| **rpagaria2000** | Correctness and test depth | Q1, Q2, P1, DOC2 |
| **fancyviper007** | User-facing quality, performance tooling, demo | U1, Q3, DEMO, DOC1 |

## Lead (integration + deployment; server access)

| WP | Scope | Status |
|---|---|---|
| L1 | Deploy the opportunity intake; prove the live path (live prices → commitments for several customers → guardian PASS → acks → settle) | IN PROGRESS (lead) |
| L2 | Security fixes: CSRF, safestop keygen seed, Mosquitto test ACL | IN PROGRESS (lead) |
| L3 | Merge PRs, run the gate + server integration tests, deploy, live verification; A1–A11 smoke | ONGOING (lead) |
| L4 | Soak (1 h) and kill-each-process chaos runs on the server (uses Q3's tooling) | OPEN (lead) |

## Contributor work packages (no server access needed)

| WP | Scope | Owned paths | Needs | Acceptance | Status |
|---|---|---|---|---|---|
| **D0** | **Local dev stack**: docker-compose with Postgres 17 + Mosquitto (same ACL model) + the 4 simulators, `make dev-up`, migrations, seed | `dev/` | Docker | `make dev-up` then `pytest tests/integration` runs locally | IN PROGRESS (lead) |
| Q1 | End-to-end functional suite, part 1: commitment lock and multi-customer scenarios (TS-04/05 from `04-mvp-s-test-plan.md`), run against the dev stack | `tests-e2e/functional/dispatch/` | D0 | Each scenario is a pytest test with a TS id; green on the dev stack | CLAIMED (rpagaria2000) |
| Q2 | End-to-end functional suite, part 2: guardian negative tests, safe stop, anomaly responses (TS-06/07) via `ogsim.control` | `tests-e2e/functional/safety/` | D0 | As Q1 | CLAIMED (rpagaria2000) |
| P1 | Property tests for K1–K13 across modules (Hypothesis), filling the gaps listed in `qa/review-*.md` | `orchestrator/tests/property/` | none | One property test per K, all green | CLAIMED (rpagaria2000) |
| DOC2 | API reference (from OpenAPI) + integration how-to (switch live ↔ simulator, the ERCOT token flow) | `docs/api/` | none | Generated + curated | CLAIMED (rpagaria2000) |
| U1 | UI validation and polish: a scripted walkthrough (Playwright or equivalent) of all 7 screens + scenario panel; accessibility (keyboard, contrast, labels); fix defects in UI templates/routes | `orchestrator/src/opengrid/ui/`, `tests-e2e/ui/` | D0 | Walkthrough green; a11y checklist in the PR | CLAIMED (fancyviper007) |
| Q3 | Performance and chaos tooling: a 10k-hub load profile, cycle-latency capture, a kill-each-process script with expected-behaviour assertions (TS-N/C) | `tests-e2e/perf/`, `tests-e2e/chaos/` | D0 | Runs locally at 2k; the lead runs it on the server (L4) | CLAIMED (fancyviper007) |
| DEMO | Demo script + scenario pack: a 20-step sign-off walkthrough (test plan §5) using `integration-sims/scenarios/` | `docs/demo/`, `integration-sims/scenarios/demo-*.yaml` | D0 | Runs start to finish on the dev stack | CLAIMED (fancyviper007) |
| DOC1 | Operator guide: start/stop, screens, the two-step actions, safe stop, what each alert means, degraded modes | `docs/operator/` | none | Reviewed by the lead | CLAIMED (fancyviper007) |
| DOC3 | Architecture and flow diagrams: architecture, the 2 s dispatch cycle, commitment lifecycle, deployment, data model | `docs/diagrams/` | none | SVG/HTML diagrams consistent with the code | CLAIMED (lead) |

## Suggested order (each developer)

- **rpagaria2000:** P1 first (no dependencies; start immediately), then Q1 → Q2 once D0 lands, and DOC2 in gaps.
- **fancyviper007:** DOC1 and U1's walkthrough plan first (read the UI code and `02b` §8), then U1 → Q3 → DEMO once
  D0 lands.

## Coordination rules

- Branch `wp/<id>-<slug>`, PR to `main`, one WP per PR where possible. Small PRs merge faster.
- If you need a change outside your owned paths, put it in the PR description; the lead routes it.
- The lead merges and deploys continuously. Rebase on `main` before opening a PR.
- The live system is at `https://base.tocy-net.net/og/` (credentials from the lead). Use it to look, not to test
  destructive actions; destructive tests run on the dev stack.
