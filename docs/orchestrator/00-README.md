# OpenGrid Orchestrator — Specification Set

The orchestrator is the application that receives signals from any client and service type, arbitrates calls by
priority, commitments and profitability, dispatches Base Power's home-battery fleet (and mobile units), integrates with
utility and ERCOT SCADA, bills what it delivers, and records a full, tamper-evident trace of every decision and why.
This folder is the specification for review **before any code is written**.

## Reading order

| # | Document | What it answers | Size (as written) |
|---|---|---|---|
| 1 | [00-brief.md](00-brief.md) | Scope, principles, customer types, dispatch profiles, defaults, conventions — read first | — |
| 2 | [00-decision-register.md](00-decision-register.md) | Decisions made, conflicts resolved, **open questions for you (Q1–Q21)** | — |
| 3 | [01-product/01-vision-scope-personas.md](01-product/01-vision-scope-personas.md) | Problem, why, KPIs, scope, release plan, judged demo, personas, end-to-end narratives | 16 personas, 21 KPIs |
| 4 | [01-product/02-functional-requirements.md](01-product/02-functional-requirements.md) | What the system must do | 318 FRs in 22 areas, 32 NFRs |
| 5 | [01-product/03-epics-and-user-stories.md](01-product/03-epics-and-user-stories.md) | Epics and stories with acceptance criteria, release map | 23 epics, 149 stories |
| 6 | [02-architecture/01-system-architecture.md](02-architecture/01-system-architecture.md) | C4 views, runtime scenarios, data, consistency, HA, deployment, scaling, ADRs | 19 ADRs, 34 NFRs, 19 diagrams |
| 7 | [02-architecture/02-domain-model-and-interfaces.md](02-architecture/02-domain-model-and-interfaces.md) | Domain model, state machines, device / utility / market / internal / public interfaces | ~45 entities, 10 state machines |
| 8 | [02-architecture/03-decision-engine.md](02-architecture/03-decision-engine.md) | The brain: dispatch profiles, arbitration, forecasting, planning, real-time control, traces, M&V and settlement | 135 FR-DE |
| 9 | [02-architecture/04-external-data-integration.md](02-architecture/04-external-data-integration.md) | External APIs (ERCOT, EIA, NWS, …): limits, errors, validation, staleness | 73 FR-ING, 9 sources / 35 products |
| 10 | [02-architecture/05-failure-modes-and-recovery.md](02-architecture/05-failure-modes-and-recovery.md) | Failure-mode catalogue: detection, response, recovery, alerts, runbooks, chaos tests | 280 failure modes, 225 alerts, 68 runbooks |
| 11 | [02-architecture/06-platform-and-operations.md](02-architecture/06-platform-and-operations.md) | k3s on 192.168.5.35, production topology, HA/DR, capacity, observability, CI/CD, secrets | 53 NFRs, 60 alerts, 28 runbooks, 12 SLOs |
| 12 | [02-architecture/07-scada-integration.md](02-architecture/07-scada-integration.md) | SCADA/EMS/DERMS integration: point lists, control authority, command safety, gateway, security, commissioning | 92 FR-SCADA, 52 SCADA failure modes |
| 13 | [03-security/01-threat-model.md](03-security/01-threat-model.md) | Assets, adversaries, trust boundaries, STRIDE, threat catalogue, attack trees, abuse cases | 161 threats |
| 14 | [03-security/02-security-architecture.md](03-security/02-security-architecture.md) | Identity, authorization, guardian, command integrity, SCADA security, audit, privacy, AI-agent security | 146 controls, 103 FR-SEC, 77 detections |
| 15 | [04-ui/01-ui-ux-specification.md](04-ui/01-ui-ux-specification.md) | Operator console: screens, workflows, design system, real-time behaviour, accessibility, usability criteria | 16 screens, 139 UI requirements |
| 16 | 05-testing/01-test-strategy.md | Test levels, harness (agent-sim, grid-sim), environments, coverage, gates | in progress |
| 17 | 05-testing/02-test-cases-functional.md | Functional, integration, end-to-end, UI and usability tests | in progress |
| 18 | 05-testing/03-test-cases-nonfunctional.md | Performance, reliability, chaos, DR, security and red-team tests | in progress |
| 19 | 05-testing/04-traceability-matrix.md | Requirement / failure mode / threat → test coverage (generated) | after 16–18 |
| 20 | 06-reviews/ | Independent adversarial and red-team reviews and the resolution log | after 19 |

Related business-case document (outside the orchestrator scope):
`business-case/01-reviewer-claims-verification.md` (kept outside this repository) — primary-source
fact-check of the independent reviewers' claims about the business cases.

## Status

Draft for the user's review. Nothing has been built or installed. Numbers from the independent business reviews are
labelled "reviewer proposal — unverified" and used as design targets until real contracts set them.
