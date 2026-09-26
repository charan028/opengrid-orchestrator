# Contributing to the OpenGrid Orchestrator

> **First read `docs/team/NOTICES.md`.** It lists requirement changes that override this file (latest: 2026-09-25 evening: continuous energy checks, Base hardware 39.2 kWh / 11 kW with 20% 2-unit homes, and upcoming service profiles and power quality).

This document is for people and their AI agents. The build lead (integration + deployment) owns `main`, the base
server (192.168.5.35) and all deployments. Contributors work in branches and pull requests on Gitea.

## 1. Read before writing any code

1. `BUILD.md`: architecture, the two independent products (orchestrator vs integration-sims), directory ownership,
   the **code-quality gate (§5a)** and safety rules (§6).
2. `orchestrator/INTERFACES.md`: public module interfaces. Change them only through the lead.
3. `docs/orchestrator/07-delivery/00-invariants.md`: K1–K13. Any violation blocks a merge.
4. `WORKBOARD.md`: pick an open work package (WP). Only work inside the paths your WP owns.

**Give your AI agent all four files as context.** They are written to be agent-readable.

## 2. Workflow

1. Claim a WP in `WORKBOARD.md` via a PR, or tell the lead. One owner per WP.
2. Branch from `main`: `wp/<WP-id>-<short-slug>` (e.g. `wp/Q1-e2e-commitment-lock`).
3. Work only in the WP's owned paths. If you need a change elsewhere, write it in the PR description; the lead
   routes it to the owner.
4. Run the local gate before pushing:
   ```bash
   cd orchestrator && make check                 # ruff, format, mypy, dupcheck, unit tests, coverage
   cd integration-sims && python -m pytest -q    # if you touched the sims
   ```
   Integration tests use the local dev stack (`dev/docker-compose.yml`: Postgres, Mosquitto, simulators); see
   `dev/README.md`.
5. Open a PR to `main` on `https://git.tocy-net.net/Tocy-Net/opengrid-orchestrator`. The description must list:
   - the WP id;
   - what changed;
   - the tests added (test-plan IDs `TS-nn-nn`);
   - the `make check` result;
   - anything needed outside your paths.
6. The lead reviews and runs the full gate plus integration tests on the server, merges, **deploys** and verifies
   live. Do not deploy, and do not push to `main`.

## 3. Rules that are easy to break (read them)

- **No duplicated functions.** Shared logic lives only in `opengrid.core`; `tools/dupcheck.py` enforces it.
- **The orchestrator and the simulators share no code.** `ogsim` never imports `opengrid` and vice versa. They meet
  only at `interfaces/` (JSON Schema / OpenAPI).
- **Commitment lock (K13):** a committed delivery is never reassigned to a better-paying opportunity. Only
  L0/L1/L2/infeasibility may interrupt it.
- **Several customers at once is normal.** Never assume one obligation at a time.
- **Secrets:** never in code, tests, logs, PRs or chat. Refer to env-variable names. Never commit `config.txt` or
  `*.env`.
- Tests are named after test-plan IDs. Use no sleeps in tests (inject clocks).

## 4. Contacts

- Build lead (integration, deployment, server, `main`): the repository owner.
- Questions about scope or spec: `docs/orchestrator/07-delivery/` first, then the lead.
