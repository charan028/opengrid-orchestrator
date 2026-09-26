# A5 — Platform and Operations: resolution of the review findings

Owner: Platform/SRE lead · 2026-09-25 · Document changed: `02-architecture/06-platform-and-operations.md` v0.3 → **v0.4**
(the only file edited). Inputs: `00-brief.md` v0.2, `00-decision-register.md` v0.2 (authoritative), the four reviews
(`01`…`04`), read-only consistency checks of `01`, `02`, `05`, `07`, `03-security/02`, `01-product/02`, `05-testing/01`
and `/03`.

**Method.** Every finding was treated as a claim to be proven. Figures quoted by the reviewers were re-derived from the
v0.3 text (and, for vendor facts, from the vendor's documentation) before anything was changed; where a claim was only
partly right, the row says which part and why the change was still made or not. The register wins over every review
proposal; where a review proposal conflicts with a register resolution, the proposal is rejected with the register item
named. Nothing is dropped (D0a): components move, change shape or change build tag; no customer type or capability leaves
scope.

**Re-checked arithmetic (summary).**
- *Appendix A.1 (ARC-005):* 9,824 + (1…98 EMQX) + (1,120…2,400 `scada-gateway`) + 680 (Keycloak) + (150…300 chaos) +
  (0…608 standbys) = **11,775–13,910 MiB = 112.2–132.5% of 10,496** — correct; minus 1,024 (`agent-sim` 2 × 448 + `grid-sim`
  128) = 10,751–12,886 — correct. EMQX 40–50 KiB × 10,000 + 250 MiB = 640.6–738.3 MiB — correct. Keycloak's vendor guide
  (fetched): "base memory usage for a Pod including caches of Realm data and 10,000 cached sessions is 1250 MB", limit
  (1,250 − 300) ÷ 0.7 ≈ 1,360 MB — correct (1,192 / 1,297 MiB).
- *Appendix A.2 (ARC-007):* `oom_score_adj` = 1000 − ⌊1000 × request ÷ 15,360⌋: EMQX 959, NATS 975, guardian 982,
  `device-gateway` 988, `agent-sim` 971, PostgreSQL 867, planner 975, Keycloak 967 — correct; scores ≈ 1000 × RSS ÷ 11,008 +
  adj: EMQX ≈ 1,023 > `agent-sim` ≈ 1,007 — correct (the planner ≈ 1,084 and Keycloak ≈ 1,031 rank higher still).
- *Appendix A.3:* demand 3.18–7.05 vCPU — correct as summed. New model (generator off the node, no command traces on the
  node): LP-E10 2.62–5.47 vCPU (+ ≤ 2 vCPU solve).
- *Appendix C (ARC-006):* COMMANDS 1,073.7 MB ÷ 0.45 MB/s = 2,386 s ≈ 40 min; AUDIT 536.9 MB ÷ 790 MB/day ≈ 16.3 h; EVENTS
  12–60 min; TELEMETRY 1,610.6 MB ÷ 0.69 MB/s ≈ 0.65 h — correct. Correction: `max_bytes` counts logical message bytes, so
  the "with S2 3×" variants describe disk use, not time to cap (MB-08 confirms on the pinned version).
- *ARC-039:* 1,000 cmd/s × 5 spans × 200 B = 1 MB/s ≈ 86.4 GB/day — correct.
- *ARC-040:* 50 × 4 × 96 = 19,200 new series/day, ≈ 576,000 over 30 d — correct; but NFR-517 limits *active* series and
  the head holds only ≈ 2,400 of them — the harm is index churn and 30-day query cost (partly verified).
- *ARC-051:* 2³¹ ÷ 120 revisions/s ≈ 1.79 × 10⁷ s ≈ 207 days — correct.
- *ARC-054:* 1,095 day-ahead + 35,040 intraday + 105,120 SCED solves at V-20's targets ≈ 73–163 CPU-hours — consistent
  with the reviewer's 100–150.
- *New budget (v0.4 §1.8):* `demo` requests 9,648 MiB (91.9%), limits 10,744 MiB (≤ 10,944 ceiling); `node-10k` 10,544 MiB
  (100.5%) with every component; 10,064 MiB (95.9%) with window levers; CPU 4,785 m (87.0%).

## 1. Findings that cite 06 (and findings assigned to 06 by the lead)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| ARC-001 | Yes — v0.3 §9.1 installed the node 09-30, ran 10k windows from 10-07 and cut over 10-26 with a production cluster started 10-05; no judging date | Fixed | 06 §9.1, §9.3, §9.4, §7.2 | J = 2026-10-21 on the node (Q22 default); production cutover after J; plan B on E6; the build-order ADR and tags are `01-product`'s (R21) |
| ARC-002 | Yes — 06 §4.1/§4.7 sized "control partitions" per bank/zone | Fixed | 06 §2.2–§2.6, §4.1, §4.7 | 06 part: fleet allocator + hash-keyed execution shards and shard groups (R30); the partitioning ADR is `01`'s |
| ARC-003 | Yes — 06 assumed per-partition chains while `01` kept a global chain tip | Fixed | 06 §4.8 | 06 part: per-stream chains, one trace per command batch with its Merkle root, COPY per batch (R22) |
| ARC-004 | Yes — three guardian budgets (100/250/350 ms); a single guardian on the node | Fixed | 06 §4.7, §3.5, §3.7, ALR-532, RB-511 | V-35 (≤ 250 ms per batch of ≤ 2,000); TIMEOUT ≠ VETO (R31); the DM-07 split is `03`'s |
| ARC-005 | Yes — Appendix A.1 re-derived: 11,775–13,910 MiB (112–133%); 10,751–12,886 without the generator | Partly fixed | 06 §1.8, §1.9.1, §1.10, §1.11 | Re-baselined per R35: `demo` 9,648 MiB (91.9%); `node-10k` 10,544 (100.5%) does not fit with every component → decision rule (MB gate, levers, PQ-6 RAM, or E6). Values stay caps until MB-01…12 measure them |
| ARC-006 | Yes — Appendix C re-derived (40 min / 16 h / 12–60 min / 0.65 h); S2 affects disk only | Fixed | 06 §4.5 | one volume model and caps; WorkQueue/Interest for submissions, commands, acks, audit, calls; DiscardNew only on AUDIT and CALLS, each with a producer fallback; names stay in `02` |
| ARC-007 | Yes — adj and score ranking re-derived (EMQX ≈ 1,023 > `agent-sim` ≈ 1,007) | Fixed | 06 §1.3, §1.8.2 (B1–B3), ADR-505, PT-13 | limit-sum rule (Σ all limits ≤ 10,944 MiB) chosen over Guaranteed on the shared node: Guaranteed needs CPU limits (throttling) and ranks host services before the control path in a host-wide OOM (conflicts with R35); forced OOM test |
| ARC-008 | Yes — four topologies for the sole signer; 06 had 1 (node) / 3 active-active (prod) | Fixed | 06 §1.8, §2.3, §2.6, §7.5 | one count per environment: node 2 per shard group (active + warm standby), `ci` 1, production 2 per shard group; the state-to-store mapping is `01`'s guardian-HA ADR (stores offered: PostgreSQL, `nats-ctl` KV with CAS) |
| ARC-009 | Yes — "≥ highest seen" admits a stale leader; a KV revision can repeat after an unsynced crash (NATS default 2-min fsync; Jepsen NATS 2.12.1) | Fixed | 06 §2.6, ADR-506, ADR-512 | equality with the live lease; epoch from a Postgres sequence + shard id; `nats-ctl` with `sync_interval: always`; fail-static from the renewal send time on the monotonic clock |
| ARC-010 | Yes — node NATS RPO 24 h and PITR roll counters and latches back | Fixed | 06 §3.2, §3.4, §3.6, RB-522, PT-16 | R36: counters = max(hub-reported, restored) + margin; latched SCADA states in Postgres and re-read; signed RESTORE record; DR case |
| ARC-011 | Yes — 06 §2.7/§9.3 relied on a signed endpoint update absent from `02` | Fixed | 06 §2.7, §9.3 | 06 part: references `02`'s single device contract (R33); `02` owns the catalogue |
| ARC-012 | Yes — 06 sized int8 Wh registers that `02`'s telemetry lacked | Fixed | 06 §4.1, §4.5 | sizing inputs follow R33 telemetry; dedupe key (hub_id, boot_id, seq) as the JetStream Msg-Id |
| ARC-019 | Yes — 06 §1.7 (mTLS 8443 to the guardian) contradicted 06 §4.5 (SUBMISSIONS stream) | Fixed | 06 §1.7, §4.5, §8.3 | one path: SUBMISSIONS WorkQueue → guardian → COMMANDS; stops from the guardian or the SSA on retained scope topics; one permission table |
| ARC-021 | Yes — 06 §4.8 stated its own hash formula | Fixed | 06 §4.8 | 06 no longer restates a formula; `01` §8.3 owns it (R22: header over every column, RFC 8785 JCS) |
| ARC-022 | Yes — DEKs rode in backups; 8-week dumps exceed V-19's 45 days | Fixed | 06 §3.3 | 06 part: per-subject keys in a store never in database backups (R38); retention documented against V-19 |
| ARC-023 | Yes — 06's append-only rule was right; `01`'s upsert contradicts it | Fixed | 06 §7.6, §4.8 | insert-only versioned settlement lines (R37), `numeric(18,6)` (V-39); `01` §8.2 to change |
| ARC-024 | Yes — v0.3 fallback could export during a stop | Fixed | 06 §3.5, §8.3 | V-07 gating, retained scope stops, DV-14 exemption, the SSA (R16). The escrow proposal is rejected per register R16 (escrowed stops expire in a long guardian outage and do not contain a compromised guardian) |
| ARC-025 | Yes — no per-service µs model; 01 used 5 s event cadence, 06 used 2 s | Fixed | 06 §4.1, §4.2, §4.7, §1.11 | one cadence (V-32); per-service demand model with (A) unit costs until MB-03…09; lease renewal is O(shard groups) (V-06) |
| ARC-026 | Yes — v0.3 lease keys followed topology partitions | Fixed | 06 §2.4, §2.6 | shards keyed on hash(hub_id) (R30); moves are planned changes with the two-phase handover |
| ARC-027 | Yes — 06 ADRs unratified; `01` names Redis, MinIO, "no Traefik" | Partly fixed | 06 §0 (ADR-500…515) | proposed for `01` §17's single ADR log; ratification is `01`'s |
| ARC-028 | Yes — a reboot takes down mail, MariaDB, Apache and `fdmp`; a Chaos Mesh daemon needs privileged/hostPID, which 06's own admission policy rejects | Fixed | 06 §6.7, §1.10, §7.2 | no shared-host reboot; node loss on E6; API-level injectors driven from the LAN host |
| ARC-029 | Yes — the co-located generator measures contention | Partly fixed | 06 §1.10, ADR-508, NFR-561 | 06 part: generator, fault proxy and chaos runner off the node, validity rules; virtual time and the G3 trace-hash criterion are `05-testing`'s |
| ARC-030 | Yes — 06 planned windows without durations | Partly fixed | 06 §9.1, §7.2 | platform windows planned from durations (W0–W3, DR); PT-07 trials 5/5/1; the per-case duration column is `05-testing/03`'s |
| ARC-031 | Yes — v0.3 sent backups to a production account that does not exist, so the RPO for host loss was unbounded | User decision Q4 | 06 §3.3, ADR-514, §9, §7.2 | interim write-once bucket specified (prefixes, lock modes, credentials) and needed by 2026-09-28; plan B = E6; the user must choose the account and provider |
| ARC-032 | Yes — v0.3 §9.2 chained production to node records and would lock synthetic settlement in 7-year compliance WORM | Fixed | 06 §9.2, §9.4, §3.3 | genesis record citing the test chain's final anchor; node data archived as evidence; never imported |
| ARC-033 | Yes — 06 had 60 rules with many P1; ALR-526 fired on every failover; ALR-532 on every restart of a single guardian | Fixed | 06 §5.6, §6.1 | 06 keeps 6 paging rules; 40 retired "→ ALR-nnn in 05"; Q15 hours; never-inhibit rule for safety/audit; ALR-532 single-replica behaviour; the combined ≤ 25 depends on `05` |
| ARC-035 | Yes — 06 runbooks were index rows | Partly fixed | 06 §6.3.1 | full bodies for the six paging runbooks plus resume and restore; the rest labelled stub; `05`'s P1 runbooks are `05`'s |
| ARC-036 | Yes — ≈ 10 single-replica dependencies; step-ca on the guardian restart path | Fixed | 06 §3.7, §8.3, ADR-507 | dependency matrix with degrade modes; pre-issued key certificates; product NFR-207 already scopes node vs production |
| ARC-037 | Yes — 10,000 × (50 + 10…50) µs ≈ 0.6–1.0 s of GIL-bound CPU | Fixed | 06 §4.7, §1.8, MB-04 | priority queues by class, Merkle-batch signing, one worker on the node (pool size configurable), V-35 per batch |
| ARC-038 | Yes — per-command OPA calls in the budget | Fixed | 06 §4.7, §1.8 | one OPA evaluation per batch; the OPA ADR is `01`'s |
| ARC-039 | Yes — ≈ 86 GB/day at LP-E10 against a 4-GiB volume | Fixed | 06 §5.4, §5.1 | head sampling 2% (1–5%), 100% of errors, vetoes, stops and approvals; production sizing; traces off on the node |
| ARC-040 | Partly — the series arithmetic is right, but the harm is index churn and 30-day query cost, not the active-series limit | Fixed | 06 §5.1, §5.2, §7.1 | no time-valued labels (also removed `expires_at`); CI cardinality test |
| ARC-042 | Yes | Fixed | 06 §1.8 | `contracts-rt` and `contracts-batch` budgeted separately (R43) |
| ARC-043 | Yes — ordering differed between `01`, `03` and 06 | Fixed | 06 NFR-530, §4.8, SLO-10 | pre-image persisted before signing; durable = Postgres or the signed local journal (R22) |
| ARC-044 | Yes — three trace-volume models an order of magnitude apart | Fixed | 06 §4.1, §4.8 | one model: `03`'s compaction figure as the upper bound for audit sizing |
| ARC-045 | Yes — anchor cadences 60 s / 5 min / 15 min / hourly; an in-cluster MinIO anchor | Fixed | 06 §3.3, ALR-547, NFR-529 | V-23 cadence, off-node write-once bucket, RFC 3161, incremental verification |
| ARC-046 | Yes — command-event volumes missing from 06 §4.3 | Fixed | 06 §4.1, §4.3, §4.4 | 3,000 events/s at LP-E10; PT-04 raised to 18,000 rows/s |
| ARC-047 | Yes — `01` §14.2 differs and 06 repeated `01`'s old total | Partly fixed | 06 §1.8, §1.3 | 06 re-baselined and is the only table; R14 closes when `01` §14.2 points here |
| ARC-048 | Yes — one dispatcher and one guardian on the node | Fixed | 06 §1.8, PT-14, NFR-555 | warm standbys budgeted; ≤ 10 s failover demonstrated |
| ARC-051 | Yes — ≈ 207 days to overflow a 32-bit epoch | Fixed | 06 §2.6 | epoch from a Postgres sequence, `bigint` (V-40) |
| ARC-052 | Yes — 10,000 reconnects at 200/s take 50 s, longer than the 30-s event lease | Fixed | 06 §3.6, §4.6, PT-03 | broker restart → V-07 autonomy for part of the fleet, stated as expected; bans only for authentication failures (V-21) |
| ARC-053 | Yes — 4.86 vCPU of requests cannot schedule on a 4-vCPU runner | Fixed | 06 §1.9, §6.7, §7.1 | `ci` profile (≤ 2,400 m, ≤ 7 GiB), CI-valid chaos rows, pipeline time budget |
| ARC-054 | Yes — ≈ 73–163 CPU-hours per full-year replay | Fixed | 06 §7.8 | tiered gate (A: golden week + envelope; B: full year) with its CI cost |
| ARC-056 | Yes — one shared `fleet-state` on the node | Fixed | 06 §2.1, §3.7 | 06 part: production estimator replica for the guardian; per-hub checks against hub-reported values are `01`/`03`'s |
| ARC-058 | Partly — only webhooks that intercept Pods (policy-controller) can block pod re-creation; cert-manager and CNPG webhooks block their own CRDs | Fixed | 06 §3.6, §1.7 | start order; `system-cluster-critical`; scoped rules; CEL policies in the API server; no policy-controller on the node |
| ARC-059 | Yes — per-key TTL and delete markers need nats-server ≥ 2.11 (NATS docs) | Fixed | 06 §2.5, §2.6, ADR-512 | version pinned; `sync_interval: always` is server-wide, hence `nats-ctl` |
| ARC-060 | Yes — 80 vs 150; transaction pooling vs asyncpg prepared statements | Fixed | 06 §2.5, §1.8 | node 100 (direct pools ≤ 80); production 200 behind PgBouncer ≥ 1.21 with `max_prepared_statements = 200` |
| ARC-063 | Yes | Fixed | 06 ADR-513, §1.8 | routing by the deterministic `command_id`; Valkey in production only; R43 wording flagged to `01` |
| RT-006 | Partly — a host spike can OOM Burstable pods first; Guaranteed pods (adj −997) would be selected only after host processes, so "evicts Guaranteed guardian/EMQX" is not the kernel's ranking | Partly fixed | 06 §1.3, ALR-502, DASH-516, ADR-505 | host-level PSI monitored; the proposed kubepods memory floor (CTL-148) is rejected per register R35; accepted demo residual with the migration date; production never co-tenants |
| RT-010 | Yes | Fixed | 06 §8.3 | demo key custody residual stated (SoftHSM2 PIN in a Secret); production HSM/KMS; epoch authority independent (R16) |
| RT-016 | Yes | Fixed | 06 §7.1, §6.6, NFR-541 | SLSA L3 for the guardian and safe-stop images on every path; two reviewers incl. CODEOWNERS |
| RT-017 | Yes | Fixed | 06 §1.12, ALR-561, RB-529, NFR-553 | monitored gate G1–G4; Q21's default enforced |
| GRD-021 | Yes — the on-call default is the project lead and no QSE desk is modelled | Partly fixed | 06 §6.1 | 06 part: a 24×7 QSE desk in the production on-call model, simulated for the demo; call types and precedence are `02`/`03`'s |
| GRD-050 | Yes | User decision Q6 | 06 §2.2, §2.1 | option A (physical sites, private interconnect, site-failover drills) vs option B (third-party QSE interface, ≤ 2 s SLA) recorded, dependent on Q6 |
| JDG-002 | Yes | Fixed | 06 §9.1, §3.4, PT-17, NFR-556 | judged demo before the cutover; portability proof + one restore drill before J |
| JDG-006 | Yes — 93.6%, 1.6× overcommit, `agent-sim` evicted first | Partly fixed | 06 §1.8, §1.9, PT-15, ADR-511 | `demo` 91.9% with limits inside the ceiling; `agent-sim` off the node (supersedes "raise above og-low", R35); 24-h soak; backups at 03:00; the freshclam change is rejected for the platform (never touch the host) → PQ-7 is the host owner's choice |
| JDG-015 | Yes | Fixed | 06 ADR-511, §1.8 | demo set per R35; reading stated (CNPG operator, Prometheus stack, `nats-ctl`); egress proxy kept for CTL-082 |
| JDG-020 | Yes | Fixed | 06 DASH-521, §1.11, §4.9 | 06 part: performance-evidence dashboard and bench inputs; the console strip is `04-ui`'s |
| JDG-021 | Yes | Fixed | 06 MB-04, §4.7 | week-1 guardian spike as MB-04; batch OPA and Merkle signing measured |
| JDG-029 | Yes | Fixed | 06 §7.4, §7.3, NFR-557 | `PROFILE=demo` with seeded fixtures (`make seed`), `make demo` on k3d, quick start, operator quick reference |

## 2. Register items assigned to 06

| Item | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| R2 | Yes — 15,360 − 3,072 − 1,280 − 512 = 10,496 MiB | Fixed | 06 §1.3, §1.8 | rebuilt per R35 |
| R14 | Yes | Partly fixed | 06 §1.8 | 06 side done; `01` §14.2 still carries its own table |
| R16 (platform part) | n/a | Fixed | 06 §1.7, §1.8, §3.5, §8.3, ALR-562, RB-531, NFR-558 | `og-safestop`, 2 replicas, stop-only hierarchy, EMQX ACL, canary, trigger route |
| R21 | n/a | Fixed | 06 §9.1, §10, §7.2 | J before cutover; build tag on every NFR; windows from durations |
| R22 / V-23 | n/a | Fixed | 06 §3.3, §4.8, ALR-547, RB-517, ADR-514, ADR-515 | 60-s checkpoints, ≤ 5-min anchors with RFC 3161, 10-s journal heads; journal volumes; the bucket itself waits on Q4 |
| R30 (platform part) | n/a | Fixed | 06 §2.2–§2.6, §4.7 | shards and shard groups in placement, leases and CPU |
| R31 | n/a | Fixed | 06 §1.8, §2.3, §2.6, §8.3, §8.4, ALR-532 | replicas: node 2 per shard group, `ci` 1, production 2 per shard group; CA off the restart path; V-08 lifetimes |
| R32 | n/a | Fixed | 06 §2.6, ADR-506, ADR-512 | durable epoch, equality, `sync: always` on `nats-ctl`, pinned NATS |
| R34 | n/a | Fixed | 06 §4.5 | one volume model; caps; alerts at 50%/80% via 05's ALR-169; RB-528 |
| R35 | n/a | Partly fixed | 06 §1.3, §1.8–§1.11 | all specified; values are caps until MB-01…12; `node-10k` fit is conditional (§1.9.1) |
| R36 | n/a | Fixed | 06 §3.4, §3.6, §9.2 | resync, RESTORE record, genesis, evidence archive |
| R41 / V-25 | n/a | Fixed | 06 §5.6 | 6 paging rules here; the combined ≤ 25 needs `05` to cut to ≤ 19 |
| R43 (platform part) | n/a | Fixed | 06 §1.8, ADR-513 | split budgeted; Valkey wording flagged |
| R45 | n/a | Fixed | 06 §5.1–§5.4, §7.1 | sampling, labels, cardinality test |
| R46 | n/a | Fixed | 06 §6.7 | no reboot, API-level injectors, `ci` profile and CI-valid chaos rows |
| R47 / R10 | n/a | Fixed | 06 §7.8 | tiered gate with CI cost |
| R9 | n/a | No change needed | 06 §4.4 | retention tiers kept; production-only write-once storage |
| R15 | n/a | No change needed | 06 §7.7 | time-boxed operational pauses unchanged |
| V-05 / V-06 / V-07 | n/a | Fixed | 06 §3.5, NFR-527 | replaced "`valid_until` ≤ 5 min" and the old keep-alive text |
| V-08…V-11 | n/a | Fixed | 06 §8.4, §6.5, §1.5 | 24-h workload certs (was 30 d), 90-d device certs (was 1 year), guardian and safe-stop keys |
| V-21 | n/a | Fixed | 06 §3.6, §4.2, §4.6 | applied per broker node (interpretation to confirm) |
| V-22 | n/a | Fixed | 06 NFR-552, ALR-552 | $25/day and $200/month |
| V-24 | n/a | Fixed | 06 §1.7 | application namespaces exact; four platform namespaces proposed |
| V-35 | n/a | Fixed | 06 §4.7, PT-02 | was 350 ms |
| Q4 | n/a | User decision Q4 | 06 §3.3, §9, §7.2 | account and provider needed by 2026-09-28 |
| Q15 | n/a | User decision Q15 | 06 §5.6, §6.1 | default applied: Mon–Fri 08:00–18:00 CT plus windows |
| Q21 | n/a | Fixed | 06 §1.12 | default (LAN-only) enforced as a monitored gate |
| Q22 | n/a | User decision Q22 | 06 §9 | default 2026-10-21 on the node applied |
| Q24 | n/a | User decision Q24 | 06 §1.10 | default (this workstation) applied with clock-offset rules |
| K10 | n/a | Fixed | 06 §7.2, §9.1 | E5/E6 plan and owners; node windows |

## 3. New IDs (06 v0.4)

- **ADR:** ADR-511 (`demo` values profile), ADR-512 (`nats-ctl`), ADR-513 (hot state without Valkey on the node), ADR-514
  (interim evidence bucket + RFC 3161), ADR-515 (per-replica audit journals).
- **NFR:** NFR-553 (co-location gate), NFR-554 (kubepods OOM containment), NFR-555 (warm-standby failover on the node),
  NFR-556 (portability proof), NFR-557 (evaluator install path), NFR-558 (Safe-Stop Authority availability), NFR-559
  (micro-benchmark gate), NFR-560 (paging budget), NFR-561 (performance-evidence validity).
- **Alerts:** ALR-560 (memory budget), ALR-561 (co-location gate, pages), ALR-562 (Safe-Stop Authority, P1 branch pages).
- **Runbooks:** RB-528 (stream filling), RB-529 (co-location gate), RB-530 (memory budget), RB-531 (Safe-Stop Authority).
- **Tests:** PT-13 (forced kubepods OOM), PT-14 (warm-standby failover), PT-15 (demo soak), PT-16 (restore resync), PT-17
  (portability proof), PT-18 (micro-benchmark gate). **Micro-benchmarks:** MB-01…12. **Dashboard:** DASH-521.
- **Platform questions:** PQ-6 (raise the guest's RAM to ≥ 23 GiB), PQ-7 (freshclam during the demo window, host owner).
- Stable IDs kept: NFR-500…552, ALR-500…559 (40 marked "Retired → ALR-nnn in 05", none deleted), RB-500…527, SLO-01…12,
  PT-01…12, DASH-501…520, ADR-500…510.

## 4. Unresolved cross-document issues (for the other owners)

1. **01:** §14.2 keeps a second resource table (≈ 4.86 vCPU, 9.92 GiB, Redis, MinIO, 4 dispatchers, 2 guardians) — must point
   to 06 §1.8 (R14 open until then); §14.1 uses `opengrid-*` namespaces (V-24); R43/`01` name Valkey for acknowledgement
   correlation while the node profiles have none (ADR-513); ratify ADR-500…515 in §17; map every guardian state item to
   Postgres or `nats-ctl` KV (R31); place the audit-writer (AUDIT consumer, checkpoint signer, anchorer), budgeted in 06 as
   its own `og-core` deployable.
2. **02:** the lease bucket's `sync: always` exists only as a server-level setting — the bucket must live on `nats-ctl`
   (ADR-512); adopt 06 §4.5's classes and caps; keep per-hub twin updates off JetStream.
3. **03-security:** §19.1 namespace names differ from V-24; §19.5 says no MQTT port is exposed because `agent-sim` runs
   in-cluster (superseded by R35/Q21); §19.6 (CTL-080) requires Guaranteed QoS on the node, which 06 replaces with the
   limit-sum rule (reasons in 06 §1.3; production keeps Guaranteed); CTL-037's trigger path now runs Apache → Traefik →
   `safe-stop`; CTL-148 (kubepods floor) conflicts with R35.
4. **05:** P1 set to cut from ≈ 55 rules to ≤ 19 so the combined budget holds; ALR-169 thresholds 50%/80% on AUDIT and
   CALLS; ALR-205 threshold to SLO-09's 99.5%; ALR-052 pages only when a stale command reached a hub (V-25); ALR-228 to remove
   "signing unavailable" (ALR-532 covers it); ALR-161's inhibition must exclude `safety`/`audit`; §2.4 stream table and
   §2.14 memory table to follow 06 §4.5/§1.8; `max_connections` 150 → 100 (node) / 200 (production); §6.2 Chaos Mesh on the
   shared node → API-level injectors; C4 node reboot → E6; C12 12,000 hubs → a connection-refusal test.
5. **07:** one adapter per link, active only, on the node at 384 + 128 MiB per link pending MB-02 (07 §7.5 plans 512 + 256);
   ordering state (COMMAND_SEQ, SBO locks, cursors) on `nats-ctl`; latched restrictive states in Postgres (R36).
6. **05-testing:** E6 becomes a single-node replica VM and the plan-B host; E5 runs after J; add PT-13…18; the node-reboot
   cases (TC-NFR-016, TC-CHAOS-004) move to E6; TC-PERF-014 trials 5/5/1; a duration column for every E4 case.
7. **03-decision-engine:** §9.3 anchors hourly; V-23 requires 60-s checkpoints and ≤ 5-min off-node anchors.
8. **01-product:** NFR-210 ("≥ 10,000 measured against the single node") depends on 06 §1.9.1 — if the node route fails,
   the 10,000-hub evidence comes from E6 running the same single-node profile.
9. **Register:** add the platform namespaces to V-24; confirm V-21 per broker node; record PQ-6 and PQ-7; confirm ADR-511's
   reading of R35's demo set (CNPG operator, Prometheus's Alertmanager/node-exporter/kube-state-metrics, `nats-ctl`, egress
   proxy kept for CTL-082); Q4 is needed this week for the interim bucket.
