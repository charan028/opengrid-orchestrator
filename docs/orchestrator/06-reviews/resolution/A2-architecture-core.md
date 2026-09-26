# Resolution A2 — Architecture core (`01-system-architecture.md`, `02-domain-model-and-interfaces.md`)

Status: v1.0 · 2026-09-25 · Owner: principal architect (system architecture and domain model) · Scope: every review
finding whose Location cites `01` or `02`, and every register item assigned to them (`../../00-decision-register.md`
v0.2). Documents changed: `02-architecture/01-system-architecture.md` → **v0.6**; `02-architecture/02-domain-model-and-interfaces.md`
→ **v0.3**. Both carry a "Changes in this version" table.

**Method.** Each finding was treated as a claim: the quoted evidence was located in the v0.5/v0.2 text and the reasoning
checked before a fix was written; fixes follow the register (it wins over every document), and numbers the register owns
are cited as "register V-nn", not restated. No finding removed a customer or service type (D0a); none introduced a refusal
based on a service's value (D0b); no personal-data sharing was added (D5).

**Legend.** Verified: **Yes** (evidence and reasoning confirmed) · **Partly** (evidence confirmed, part of the reasoning or
scope belongs elsewhere) · **No**. Disposition: **Fixed** · **Partly fixed** · **Rejected** · **Deferred R2** · **User
decision Qn**. "01"/"02" = the two documents; "(cross-doc)" = the remaining work sits in another document and is listed in §4.

## 1. Findings whose Location cites 01 or 02

### 1.1 Architecture and SRE review (`01-review-architecture-sre.md`)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| ARC-002 | Yes — 01 v0.5 §5.4 "exactly one active (leader-elected) instance per partition (bank/zone group" vs `03` §8.3 components; counts 4 and ≈ 200 (01) vs 2/20 (`06`) vs ≈ 50/8 (`03`) | Fixed | 01 §5.4, §10, §11.1–§11.3, `ADR-021`; 02 §1.1 `PartitionAssignment`, §5 | One fleet allocator + hash-keyed execution shards; one shard table and call-routing table for every document; `03` §8.1/§8.3 and `06` §4.7 to adopt (cross-doc) |
| ARC-003 | Yes — 01 v0.5 §8.3 "chain tip … locked FOR UPDATE"; §8.1 one guardian transaction per candidate command | Fixed | 01 §5.5, §8.1, §8.3, `ADR-022`, `ADR-024` | One transaction (COPY) and one signature per batch; per-stream chains; scope-broadcast stops; commit-rate targets for TC-PERF-055 are `05-testing`'s |
| ARC-005 | Partly — the over-budget memory figures are `06`/`07`/Keycloak's; 01's contribution (hot-standby dispatcher and ≥ 2 guardians on the node, 01 §10, §13.1) confirmed | Partly fixed | 01 §2.1, §11.2, §13.1, §14.2, `ADR-030`, `ADR-071` verdict | Generator off the node, standby counts per R35, SCADA adapters active-only on the node; re-baselining `06` §1.8 and `07` §7.5 sizing is theirs (cross-doc) |
| ARC-006 | Yes — 02 v0.2 §5 "max_age=24 h, discard-oldest"; EVENTS 1 h vs `05`/`06` 30 d; 01 §14.3 "1.5 GiB" | Fixed | 02 §5 (normative table), §5.6 (name mapping); 01 §8.1, `ADR-034`, `NFR-044` | WorkQueue/Interest for command-path streams, Limits only for telemetry-like data; `05` §2.14 and `06` §4.5 must point to 02 §5 (cross-doc) |
| ARC-008 | Yes — 01 v0.5 §5.5 "N-way active-active (pure function …)"; the guardian holds floors, approvals, clocks, rate windows, latches | Fixed | 01 §5.5 (state inventory), §10, §11.2, `ADR-022` | Active/standby per shard group, fenced failover ≤ register V-02; one replica count per environment |
| ARC-009 | Yes — 01 v0.5 §10 epoch incremented in the KV value (can regress after a single-replica JetStream crash); 02 §3.3; hub floors per hub (`03-security` DV-07) | Fixed | 01 §10, `ADR-023`; 02 §3.2 (`epoch`, `gepoch`, `floors`), §3.3 | PostgreSQL generation + shard id, equality at commit, floors per (issuer class, shard); `06` §2.6 and `../03-security` §7.5/DV-07 to follow (cross-doc) |
| ARC-010 | Yes — 02 v0.2 §3.2 `status` could not report counters or floors | Fixed | 02 §3.2 `status` (`last_applied`, `floors`, `key_epoch`, scope `seq`); 01 §6.17, §13.3, `ADR-026`, `ADR-076` verdict | Latched restrictive states in PostgreSQL (R36); the TC-DR case is `05-testing`'s |
| ARC-011 | Yes — 02 v0.2 §3.1 "subscribe only to hub/{hub_id}/cmd and hub/{hub_id}/twin/desired" | Fixed | 02 §3.1–§3.3, §3.6; 01 `ADR-035` | Retained scope stops, lease heartbeat, key set, assignments, replay, meter, endpoint update, signed NACK, envelope as schema, AsyncAPI |
| ARC-012 | Yes — 02 v0.2 telemetry lacked registers, `boot_id`, quality; 01 §6.1 `Msg-Id = hub_id+seq`; no meter, AMI or baseline entity | Fixed | 02 §1.2 (`MeterBlock`, `AmiInterval`, `Baseline`), §3.2, §3.3; 01 §6.1, §8.2 | Dedupe key (hub_id, boot_id, seq); settlement reads only verified meter blocks |
| ARC-013 | Yes — no reservation entity in 02, no owner in 01 | Fixed | 02 §1.2 `Reservation`; 01 §5.4, §8.5, `ADR-021` | Single writer = allocator; committed before submission; guardian checks the ledger version; `03` §8.1 S11 (asynchronous ledger) to change (cross-doc) |
| ARC-014 | Yes — 01 §6.4 "enrolled hubs"; 02 had no `Enrollment` | Fixed | 02 §1.2 `Enrollment` (exclusivity group, effective range), §1.4 versioned `VrMembership` | Register Q6 default enforced through exclusivity groups |
| ARC-015 | Yes — 02 v0.2 §2.1 `STALE` after `SIGNAL_HOLD_MINUTES` (15) | Fixed | 02 §2.1 (two machines); 01 §6.6, `NFR-046` | Register V-29; see also §4 item R-3 on the `LATE` state |
| ARC-016 | Yes — 01 NFR-013 p99 5 s vs a 4-s freshness gate; 02 §3.4 skew 30 s; `03` 120-s target below R13's 180-s ramp | Fixed | 01 §9.1, §9.2 (normative table with owners), `NFR-012`, `NFR-013`, `NFR-039`; 02 §3.4 | `03` §3.4/FR-DE-013 still state 120 s — register V-34 governs (cross-doc) |
| ARC-017 | Yes — 02 v0.2 §3.5 "re-syncs to twin/desired (retained)" | Fixed | 02 §2.2, §3.1 (retired), §3.5; 01 `ADR-035` | Hubs act only on guardian-signed commands and signed scope states |
| ARC-018 | Yes — 01 v0.5 §8.1 `command_id` minted at signing; submissions without idempotency | Fixed | 01 §8.2, `ADR-023`; 02 §3.2 (`jti`), §3.3 | Submission id and derived command id; re-issue ≥ 2 s after the acknowledgement window; `05` §2.2's "U(0, 1 s)" to change (cross-doc); DV-14 stop exemption is `../03-security`'s |
| ARC-019 | Yes — 01/02 have the guardian publish; `03` S9 and `05` §2.2 have the dispatcher publish; `06` §1.7 HTTPS 8443 vs SUBMISSIONS vs request/reply | Fixed | 01 §8.1 (normative sequence diagram and NATS permission table); 02 §5, §5.1 | `05` §2.2 diagram, `06` §1.7 and `03` §8.1 to follow (cross-doc) |
| ARC-020 | Yes — 02 v0.2 §5 `qg-api-ws`, §5.2 "exactly one replica processes any given message" | Fixed | 02 §5, §5.3; 01 §5.2 | Durable per service for work, ephemeral per replica for broadcast, hash-partitioned `fleet-state` consumers, acknowledgements on shard subjects |
| ARC-021 | Yes — 01 v0.5 §8.3 formula omits actor, type and payload reference; audit table was a hypertable | Fixed | 01 §7.1, §8.3, `ADR-024`; 02 §1.6 | RFC 8785 JCS header hash; chain index in a plain table with UNIQUE constraints; `03` §9.3, `06` §4.8, `../03-security` §12.2 to adopt the formula (cross-doc) |
| ARC-022 | Yes — 01 v0.5 §7.3 DEK "held only in the pii schema", §7.1 backups | Fixed | 01 §7.3, `ADR-027`, `NFR-041`; 02 §1.3 `ErasureLedger`, §1.6 `SubjectKeyRef` | Wrapping keys outside every database backup; erasure ledger replayed after restores |
| ARC-023 | Yes — 01 §8.2 upsert; 02 `SettlementInterval` money as float | Fixed | 01 §8.2; 02 §1.2 `SettlementLine`, §1.7 | Insert-only, supersede links, `numeric(18,6)` (register V-39) |
| ARC-025 | Yes — 01 v0.5 §15 vendor benchmark, 20% at 5 s, commands "1–2 orders of magnitude below" | Fixed | 01 §15 (per-service demand model), `ADR-001` trigger | One event cadence (register V-03/V-32); priors [RE] until the R35 micro-benchmarks |
| ARC-026 | Yes — 02 v0.2 §5 topology-keyed subjects `hub.<partition>.*`, `cmd.<partition>.*` | Fixed | 01 §11.1, §11.4, `ADR-021`; 02 §1.1, §2.12, §5.6 | Feeder transfers change eligibility, not shard; `03` FR-DE-084 wording (cross-doc) |
| ARC-027 | Yes — 01 §17 had no ADR for partitioning, audit topology, guardian HA, batching, ledger, generator placement; named Redis, MinIO, "no Traefik" | Fixed | 01 §17.1 (`ADR-021`…`ADR-035`; amended `ADR-001`…`ADR-020`), §17.2 (verdicts on `ADR-500`…`510`, `ADR-071`…`078`) | README ADR count to update (cross-doc) |
| ARC-036 | Yes (01 part) — 01 v0.5 §8.1 made a synchronous database write per command part of signing | Partly fixed | 01 §5.5 (state inventory; step-ca off the restart path), §8.3 (local journal), §13.1, `ADR-022` | A dependency matrix per hop and scoping of product NFR-207 belong to `05`/`06` and `01-product` (cross-doc) |
| ARC-037 | Yes — 01 `ADR-001` Python; the guardian a single asyncio process | Fixed | 01 §5.5, §12, `ADR-022` | Priority queues by class, process-pool Merkle signing; per-class measurement is `05-testing`'s |
| ARC-038 | Yes — 01 `ADR-009` "embedded, not a network hop" (not possible natively in Python) | Fixed | 01 `ADR-009` (amended), `ADR-022` | One OPA evaluation per batch through the sidecar; OPA-WASM as the measured fallback |
| ARC-042 | Yes — 01 §5.6 "Four sub-domains, one service" | Fixed | 01 §4.1, §5.6, `ADR-028` | `contracts-rt` / `contracts-batch`; `06` §1.8 deployables (cross-doc) |
| ARC-043 | Yes — 01 writes the trace before signing, `03` S11 after publishing | Fixed | 01 §8.1, §8.3, `ADR-024`; 02 §1.2 `GuardianBatch` | Pre-image before signing, enrichment appended; `03` §8.1 and `06` NFR-530 wording (cross-doc) |
| ARC-044 | Yes — 01 v0.5 §5.4 "one ArbitrationDecision + DecisionTrace per tick per partition regardless" | Fixed | 01 §5.4, §8.3; 02 §1.2 `Decision` | `03` §9.3 compaction adopted; one trace-volume model in `06` (cross-doc) |
| ARC-045 | Yes — 01 v0.5 §8.3 anchor to an in-cluster MinIO; cadence "periodic" | Fixed | 01 §8.3, `ADR-018` (amended), `ADR-504` verdict | Off-node write-once bucket + RFC 3161 per register V-23; the interim bucket needs register Q4 |
| ARC-046 | Yes — guardian was the only (insert-only) writer while 12 states came from other components | Fixed | 01 §8.2; 02 §1.2 `CommandEvent`, §2.3 | Append-only observer events, current state as projection; volumes in `06` §4.3 (cross-doc) |
| ARC-047 | Yes — 01 §14.2 table disagreed with `06` §1.8 (dispatcher, guardian, EMQX, PostgreSQL, Redis/MinIO) | Fixed | 01 §14.2 (table removed → pointer), `ADR-020` superseded | R14 can be closed when `06` §1.8 is regenerated (register) |
| ARC-048 | Yes — 01 hot standby vs one dispatcher replica on the node | Partly fixed (01 part complete) | 01 §10, §11.2, §13.1, `ADR-030` | Warm standby per shard group on the node (R35); `06` budget and TC-NFR-001 to follow (cross-doc) |
| ARC-049 | Yes — 01 v0.5 §6.13 "approved automatically … still requires confirmation" | Fixed | 01 §5.10, §6.13, `ADR-029`; 02 §1.2 `ConstraintSet`, §1.5 | Confirmation always required; `03` §11.3 to follow (cross-doc) |
| ARC-050 | Yes — 02 v0.2 §5.1 versioned only NATS payloads; MQTT topics and commands carried no version | Fixed | 02 §3.6, §5.4; 01 §16, `ADR-006` | `v` field, MQTT 5 user properties, N/N-1 per firmware cohort, AsyncAPI |
| ARC-051 | Partly — `int` counters and string windows confirmed; the ≈ 200–250-day overflow holds only for `06`'s KV-revision epoch | Fixed | 02 §1.1 `HubKey`, §1.2, §1.7; 01 §7 | `bigint` everywhere (register V-40), `tstzrange`, hub surrogate key |
| ARC-055 | Yes (01/02 part) — 01 §5.11 `LARGE_LOAD` "drop to standby" contradicts `03` §2.6 (C-15); C-07, C-08, C-20, C-22, NF-Q20 touch 01/02 | Fixed (01/02 items) | 01 §5.11 (C-15), `ADR-007` (C-07), §14.1 (NF-Q20); 02 §3.2 (C-08), §2.1 (C-20), §2.3 (C-22) | Importing the remaining C-/NF-Q items into register §E was done by the register v0.2 |
| ARC-056 | Yes — 01 NFR-001/NFR-003 had the guardian re-validate "against the fleet-state twin" | Fixed | 01 `NFR-001`, §5.5, `ADR-022` | Hub-reported values from its own telemetry consumer; separate estimator replica in production; FMEA row is `05`'s (cross-doc) |
| ARC-057 | Yes — 01 §7.1 "space-partitioned by hash(hub_id) (8–16 partitions)" | Fixed | 01 §7, §7.1, `ADR-004` | Time-only chunks, `segmentby hub_sk` |
| ARC-059 | Yes — 01 §10 relies on per-key TTL semantics; NATS 2.11 introduced per-message TTLs (to verify in the spike) | Fixed | 01 §10, `ADR-003`, `ADR-023` | Version pinned; fail-static measured from the send time on the monotonic clock |
| ARC-061 | Yes — 01 §12 "lower-priority-class calls are deferred/rejected first" | Fixed | 01 §11.3, §12, `ADR-033`, `NFR-042`; 02 §2.4 | Clip or defer with the shortfall reported, never reject for capacity (R48) |
| ARC-063 | Yes — 01 §4.1 "stateless relay" vs §5.1 in-flight correlation | Fixed | 01 §4.1, §5.1, `ADR-028`; 02 §5.2 `og-ackcorr` | Stateless except correlation in Valkey (NATS KV in the demo profile) |
| ARC-064 | Yes — each erratum found: §6.1 "signature freshness" on unsigned telemetry; §4 SCADA-protocol label on internal links; §6.5 acknowledgements to the guardian; "02 §7" for the API; 02 command expiring 10 s after issue | Fixed (01/02 items) | 01 §4, §5.1, §5.8, §6.1, §6.5; 02 §3.2 | README ADR count is the README owner's (cross-doc) |

### 1.2 Grid, market and SCADA review (`02-review-grid-market-scada.md`; "DM" = 02)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| GRD-003 | Yes — 02 v0.2 §1.1 `Bank.rated_kw` compared with kVA ratings | Partly fixed (02 part complete) | 02 §1.1 (unit-typed ratings, `rating_basis`), §1.4 (unit on point maps); 01 §5.4, §5.11 | The control law on apparent power or per-phase current is `03`'s (cross-doc) |
| GRD-006 | Yes — 02 v0.2 §1.1 had no `phase` | Partly fixed (02 part complete) | 02 §1.1 (`phase` on service transformer and service point); 01 `NFR-005` | Per-phase regulation is `03`'s (cross-doc) |
| GRD-011 | Yes — 02 v0.2 §3.5 "do not export; hold current SOC", 5/30-min timers vs `03`'s 15-min fallback export | Fixed | 02 §2.2, §3.5 (register V-07; R25 counterparty stop path); 01 §13.1 | `03` §8.15(a) and `05` HUB-R05 must state register V-07 (cross-doc); firmware capability is register Q2 |
| GRD-014 | Partly — the Location cites `03`'s degraded-mode code DM-10, not the domain model; 01 had no ICCP/QSE-link-loss rule | Partly fixed | 01 §5.11 (`ERCOT_ENERGY` failure behaviour), §6.12 (hold last set point flat, QSE desk calls ERCOT, COP update) | `03` DM-10, `07` §3.2.4/§5.7 and `05` FM-MKT-011 carry R25's rule (cross-doc) |
| GRD-023 | Yes — no COP object in 02 v0.2 §4.3 | Fixed | 02 §1.2 `CurrentOperatingPlan`, §4.3; 01 §5.3, §6.2, §6.16, `ADR-032` | 168 h, resubmission on ≥ 1 MW or ≥ 10% and within 60 min (R17) |
| GRD-039 | Yes — 02 v0.2 §4.3 `"hold_hours": 4, "price_per_kw_yr": 41.2` | Fixed | 02 §1.2 `AsAward`, §4.3; 01 §5.11 | DAM hourly MW at MCPC, RT per SCED run, NCLR XML deployments; re-checked against `05-claims-verification.md` items 2, 3 and 14 (proxy offers, NCLR rules, UDSP ramp) and the revised R17 — applied in 02 §1.2 and §4.3 |
| GRD-055 | Yes — 02 v0.2 §2.1 15-min/60-min thresholds | Fixed | 02 §2.1; 01 `NFR-046` | `07` §2.4 counts map to `SILENT` (cross-doc) |

### 1.3 Red-team report (`03-red-team-report.md`)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| RT-002 | Yes — 01 v0.5 §5.5 and §8.4 themselves flagged the guardian's single-point availability as open | Fixed | 01 §5.12, `ADR-025` (Safe-Stop Authority, dispatch-key epoch authority), `ADR-022` (HA) | FR-SEC-205 wording is `../03-security`'s |
| RT-006 | Yes — 01 v0.5 §14.2 note: cgroup requests do not reserve capacity against host processes | Partly fixed | 01 §2.1, §14.2 (kept as an architecture rule; table removed) | Host-level floor (CTL-148) and monitoring are `06`/`../03-security`'s (cross-doc); accepted demo residual (RR-01) |
| RT-008 | Yes — 01 §5.4/§8.1: the dispatcher authors its own trace; nothing independent detects withholding | Fixed | 01 §5.5 (verdict bound to the decision's inputs hash), §5.6 (withholding detector in `contracts-rt`); 02 §2.5 | DET-018/CTL-137 extensions are `../03-security`'s |
| RT-013 | Yes — 01 v0.5 §10 checks "against the KV bucket's current value" with no rule for stale reads | Fixed | 01 §10 (stale or unreadable lease state fails closed), `ADR-023` | Equality at commit with a PostgreSQL generation |

### 1.4 Judging and product review (`04-review-judging-and-product.md`; "01-arch" = 01)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| JDG-006 | Yes — 01 v0.5 §14.2 requested ≈ 9.92 GiB of 10,496 MiB | Partly fixed (01 part complete) | 01 §14.2 (pointer to `06` §1.8; demo profile rules), `ADR-030` | `06` §1.9 demo profile values (cross-doc) |
| JDG-007 | Yes — 01 global chain tip vs `03` per-stream chains vs security's 5-min anchors | Fixed | 01 §8.3, `ADR-024` | K3 closed per R22 |
| JDG-009 | Yes — 01 v0.5 had no adapter interface and no shadow mode | Fixed | 01 §5.1, `ADR-031`, `NFR-043`; 02 §3.7 | Build tag MVP-B |
| JDG-012 | Yes — 01 NFR-008 "at least one control-loop tick" | Fixed | 01 `NFR-008` (register V-41); 02 §2.5 | KPI-13 and UI-OBL-02 belong to others |
| JDG-015 | Yes — 01 §4 and §14 listed ≈ 20 infrastructure components for the node | Partly fixed (01 part complete) | 01 §4 (Valkey production-only), §14.2 (demo values profile), `ADR-030` | `06` §1.9 profile (cross-doc) |
| JDG-018 | Partly — a perception risk; 01 §5.3/`ADR-005` and §5.10 are accurate descriptions | Partly fixed | 01 "Where the brain lives"; `ADR-016` (AI-off consequence) | README section, examples as tests and demo toggles belong to others |
| JDG-021 | Yes — 01 §5.5/§8.1 per-command OPA calls and inserts | Fixed | 01 §5.5, `ADR-009`, `ADR-022` | The week-1 measurement spike is `05`'s |

### 1.5 Disposition counts (findings citing 01 or 02)

| Review | Rows | Fixed | Partly fixed | Rejected | Deferred R2 | User decision |
|---|---|---|---|---|---|---|
| Architecture and SRE | 43 | 40 | 3 (ARC-005, ARC-036, ARC-048) | 0 | 0 | 0 |
| Grid, market, SCADA | 7 | 4 | 3 (GRD-003, GRD-006, GRD-014) | 0 | 0 | 0 |
| Red team | 4 | 3 | 1 (RT-006) | 0 | 0 | 0 |
| Judging and product | 7 | 4 | 3 (JDG-006, JDG-015, JDG-018) | 0 | 0 | 0 |
| **Total** | **61** | **51** | **10** | **0** | **0** | **0** |

Every "Partly fixed" row is complete for 01 and 02; the remainder — the finding's core fix — sits in another document (§5). Items whose provisioning
depends on a user decision (interim bucket, register Q4; firmware capabilities, register Q2) are designed and marked
"Fixed" with the question named in the note.

## 2. Register items assigned to 01 and 02

| Item | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| R14 (resource table) | Yes — ARC-047 | Fixed (01 part) | 01 §14.2, `ADR-020` superseded | Close when `06` §1.8 is regenerated |
| R16 (Safe-Stop Authority; closes K11) | Yes — RT-001/RT-002, ARC-024 | Fixed | 01 §5.12, §6.9, §6.15, §8.1, §8.4, `ADR-025`, `NFR-036`; 02 §2.11, §3.1, §3.2 (scope state) | Scope-state expiry exception to DV-08 is `../03-security`'s (cross-doc) |
| R17 (ERCOT instructions, NPC regulator, `IsoInstruction`, COP) | Yes — GRD-001/-002/-023/-039 | Fixed (01/02 parts) | 01 §5.3, §5.4, §5.7, §5.11, §6.3, §6.16, §11.3, `ADR-032`, `NFR-045`; 02 §1.2, §2.13, §4.3 | The NPC regulator law is `03`'s |
| R18 (unit-typed ratings, `phase`) | Yes — GRD-003/-006 | Fixed (02 part) | 02 §1.1, §1.4; 01 §5.4, §5.11 | Control law `03` |
| R20 (TEEEF statute fields; `MOBILE_DER`) | Yes — GRD-005/-018/-019 | Fixed (02 part) | 02 §1.1 `MobileDeployment`, §1.2 variants, §2.10; 01 §5.11 | Cold-load planning is `03`'s |
| R22 (audit chains; closes K3) | Yes — ARC-003/-021/-043/-045, JDG-007, RT-007 | Fixed | 01 §7.1, §8.1, §8.3, §8.5, `ADR-024`, `NFR-037`; 02 §1.2, §1.6, §5 (`AUDIT`) | Formula alignment in `03`, `06`, `../03-security` (cross-doc) |
| R23 (`DeviceAdapter`, `SHADOW`) | Yes — JDG-009 | Fixed | 01 §5.1, `ADR-031`, `NFR-043`; 02 §2.3 (`RECORDED`), §3.7, §6.2 | — |
| R25 (autonomy per V-07; QSE desk; counterparty stop path) | Yes — GRD-011/-014/-021 | Fixed (01/02 parts) | 02 §2.2, §3.5, §6.1 (QSE desk); 01 §6.12, §6.16, §5.8 | QSE-desk role code to be assigned by `../03-security` §5.1 (cross-doc) |
| R26 (autonomous grid support) | Yes — GRD-009/-020/-025/-031 | Fixed (02 part) | 02 §2.1 (`SETTINGS_DRIFT`), §3.2 (reason codes, `dp_auto_kw`, settings hash), §3.5 | Integrator freezing is `03`'s |
| R27 (territory roles, tolling, TDU and PJM variants) | Yes — GRD-015/-016/-024/-048/-049; JDG-017 | Fixed (02 part) | 02 §1.1 `Territory`, §1.2 `MarketRole`, `TariffTable`, `AderResource`, `Enrollment`, program variants; 01 §5.11 | Admission rules are `03`/`contracts-rt` behaviour, defaults `03` §2.6 |
| R30 (control partitioning) | Yes — ARC-002/-013/-026 | Fixed | 01 §5.4, §10, §11, `ADR-021`; 02 §1.1, §2.12, §5 | — |
| R31 (guardian HA, state, budget) | Yes — ARC-004/-008/-036/-037/-038/-056 | Fixed | 01 §5.5, §8.1, §12, §13, `ADR-022`, `NFR-035`; 02 §2.3, §2.6 | `03` §8.13–§8.14 TIMEOUT/VETO split (cross-doc) |
| R32 (epochs, fencing, idempotency) | Yes — ARC-009/-018/-051, RT-013 | Fixed | 01 §8.2, §10, `ADR-023`, `NFR-038`; 02 §1.2, §1.7, §3.2, §3.3 | — |
| R33 (one device contract; closes C-08, C-22) | Yes — ARC-011/-012/-017/-050 | Fixed | 02 §2.3, §3; 01 `ADR-035` | — |
| R34 (bus layout) | Yes — ARC-006/-019/-020/-044 | Fixed | 02 §5; 01 §8.1, `ADR-034`, `NFR-044` | Final `max_bytes` from `06` §4.5 |
| R35 (node budget, generator off the node) | Yes — ARC-005/-007/-029/-048 | Fixed (01 part) | 01 §2.1, §9.1, §13.1, §14.2, §15, `ADR-030`, `ADR-505` verdict | Measurements and `06` §1.8 are `06`'s |
| R36 (restore and genesis) | Yes — ARC-010, ARC-032 | Fixed | 01 §6.17, §13.3, `ADR-026`, `NFR-040`; 02 §1.4 `LatchedState`, §3.2 `status` | — |
| R37 (data model completions) | Yes — ARC-012/-013/-014/-023/-046/-051; GRD-021/-023 | Fixed | 02 §1 (all views), §1.7; 01 §7 | — |
| R38 (crypto-shredding) | Yes — ARC-022 | Fixed | 01 §7.3, `ADR-027`, `NFR-041`; 02 §1.3, §1.6 | Backup retention documentation is `06`'s |
| R39 (latency budget) | Yes — ARC-016 | Fixed | 01 §9.2, `NFR-039` | — |
| R40 (connectivity vs eligibility) | Yes — ARC-015, GRD-055, C-20 | Fixed | 02 §2.1; 01 §6.6, `NFR-046` | See §5 R-3 (`LATE`) |
| R43 (deployables) | Yes — ARC-042/-063 | Fixed | 01 §4.1, §5.1, §5.6, `ADR-028` | — |
| R48 (clip or defer; control-room channels) | Yes — ARC-061/-062 | Fixed | 01 §11.3, §12, `ADR-033`, `NFR-042`; 02 §2.4, §6.3 | — |
| R49 (AI constraint sets) | Yes — ARC-049 | Fixed | 01 §5.10, §6.13, `ADR-029`; 02 §1.2, §1.5, §6.2 | — |
| C-15 (`LARGE_LOAD` signal loss) | Yes — 01 §5.11 vs `03` §2.6 | Fixed | 01 §5.11 | `03` owns the profile |
| C-07 (signing algorithm) | Yes — 01 `ADR-007` Ed25519 primary | Fixed | 01 `ADR-007`; 02 §3.2 | ES256 only (register V-36) |
| K3 (audit-write failure) | Yes | Fixed via R22 | 01 §8.3 | Both: firm delivery continues on a signed, anchored local journal |
| K11 (stops wait for the guardian) | Yes | Fixed via R16 | 01 §5.12 | — |

## 3. Findings not citing 01/02 but closed there through assigned register items

These findings are owned by other documents; the rows record only the part that landed in 01 or 02, so they are
"Partly fixed" here and take their final disposition from their primary owner's table.

| ID | Register | Disposition | Where in 01/02 | Note |
|---|---|---|---|---|
| ARC-004 | R31 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §5.5, `ADR-022`, `NFR-035`; 02 §2.3, §2.6 | TIMEOUT ≠ VETO; `03` DM-07 and `05` §2.3 carry the rest |
| ARC-007 | R35 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §2.1, §14.2, `ADR-505` verdict | Guaranteed QoS or limits below the kubepods cap |
| ARC-024 | R16 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §5.12, `ADR-025` | Escrowed stops considered and not chosen (register R16) |
| ARC-029 | R35 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §9.1 | Virtual time for component tests only |
| ARC-031 | Q4 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §2.1, `ADR-504` verdict | Interim off-node bucket now |
| ARC-032 | R36 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §13.3, `ADR-026` | Production genesis record |
| ARC-034 | Q11, R44 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §17.2 (`ADR-072`, `ADR-073` verdicts) | Demo DNP3 stack by the week-1 spike |
| ARC-039, ARC-040 | R45 | Partly fixed (01/02 part complete; primary owner document remains) | 01 `ADR-012` (amended) | Sampling and label rules; sizing is `06`'s |
| ARC-052 | — | Partly fixed (01/02 part complete; primary owner document remains) | 01 `NFR-010` | Broker restart on the node means AUTONOMOUS |
| ARC-054 | R10, R47 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §16 | Tiered activation gate |
| ARC-062 | R48 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §12; 02 §6.3 | Control-room channels at 1 s |
| GRD-001, GRD-002, GRD-013, GRD-017, GRD-021, GRD-022, GRD-057 | R17 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §5.4, §5.11, §6.3, §6.16, `ADR-032`; 02 §1.2, §4.3 | NPC regulator, ERCOT-visible capability invariant, ALR/NCLR, QSE desk, qualified-MW caps |
| GRD-005, GRD-018, GRD-019 | R20 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §1.1, §2.10; 01 §5.11 | Statute-shaped TEEEF |
| GRD-009, GRD-020, GRD-025, GRD-031 | R26 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §2.1, §3.2, §3.5 | Reason codes, settings read-back and drift quarantine |
| GRD-015, GRD-016, GRD-024, GRD-048, GRD-049 | R27 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §1.1, §1.2 | Territory roles and program variants |
| GRD-033 | R28 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §1.1 `Feeder` | Zero net reverse flow by default |
| GRD-037 | R50 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §1.2 `Contract` | Each counterparty's measurement method recorded |
| RT-001, RT-018 | R16 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §5.12, §6.15 | Out-of-band trigger re-pointed at the SSA |
| RT-007 | R22 | Partly fixed (01/02 part complete; primary owner document remains) | 01 §8.3 | Producer-signed journal anchored every 10 s |
| JDG-028 | R33 | Partly fixed (01/02 part complete; primary owner document remains) | 02 §3.6 | Device contract published as a separate conformance package |

## 4. New IDs created

**ADRs (01 §17.1).** New: `ADR-021` control partitioning · `ADR-022` guardian HA, state, time budget and batch signing ·
`ADR-023` epochs, fencing and idempotency · `ADR-024` audit chains and audit-write failure · `ADR-025` Safe-Stop Authority ·
`ADR-026` restore, resynchronization and production genesis · `ADR-027` crypto-shredding keys outside backups · `ADR-028`
deployables · `ADR-029` AI constraint sets · `ADR-030` node budget and off-node generator · `ADR-031` `DeviceAdapter` and
`SHADOW` · `ADR-032` ERCOT instructions as hard constraints · `ADR-033` clip or defer · `ADR-034` message-bus layout ·
`ADR-035` one device contract. Amended in place: `ADR-001`, `-002`, `-003`, `-004`, `-006`, `-007`, `-008`, `-009`, `-010`,
`-012`, `-013`, `-014`, `-015`, `-016`, `-018`; superseded: `ADR-020`. Verdicts (01 §17.2): ratified `ADR-500`, `-501`,
`-502`, `-503`, `-508`, `-509`, `-510`, `-073`, `-074`, `-075`, `-077`, `-078`; ratified with amendment `ADR-504`, `-505`,
`-507`, `-071`; ratified in part (part rejected) `ADR-506` (epoch = KV revision and "lower than highest seen" rejected),
`ADR-072` ("OpenDNP3 not selected" superseded by register Q11), `ADR-076` (latched states moved to PostgreSQL).

**NFRs (01 §1.12).** `NFR-035` guardian TIMEOUT semantics · `NFR-036` stop with the guardian down · `NFR-037` no command
without a durable trace · `NFR-038` epoch fencing · `NFR-039` per-segment latency · `NFR-040` restore resynchronization ·
`NFR-041` erasure survives restores · `NFR-042` no capacity-based rejection · `NFR-043` `SHADOW` publishes nothing ·
`NFR-044` stream retention and alerts · `NFR-045` ERCOT-visible capability invariant · `NFR-046` silent-hub exclusion.
`NFR-001`…`NFR-034` keep their IDs; texts updated where noted in 01's change table; every NFR now carries a build tag (R21).

**Entities (02 §1).** `Territory`, `MarketRole`, `TariffTable`, `AderResource`, `Enrollment`, `IsoInstruction`, `AsAward`,
`CurrentOperatingPlan`, `ConstraintSet`, `Reservation`, `Decision` (renamed from `ArbitrationDecision`), `GuardianBatch`,
`CommandEvent`, `MeterBlock`, `AmiInterval`, `Baseline`, `SettlementLine`, `HubKey`, `PartitionAssignment`,
`ScopeStopEvent`, `ApprovalEvent`, `EpochGen`, `ErasureLedger`, `VrMembership`, `LatchedState`, `AuditChain`,
`AuditPayload`, `AuditCheckpoint`, `AuditAnchor`, `SubjectKeyRef`. State machines 02 §2.11 (scope stop), §2.12 (shard
handover), §2.13 (ISO instruction); connectivity state `LATE`; command states `SUBMITTED` and `RECORDED` (v0.2 names kept, refusals and
failures carry a `reason`; batch states `TIMEOUT`, `VETOED`, `PARTLY_VETOED`, `ORPHANED` in §2.6); ops entities `ShiftLogEntry`, `Handover`, `Contact`.

**Interfaces.** MQTT topics `hub/{id}/meter`, `hub/{id}/replay`, `hub/{id}/assign`, `scope/{bank,zone,fleet}/{id}/stop`,
`fleet/lease/{grp}`, `keys/set`, `fleet/endpoint` (02 §3.1). Streams `METER`, `CMDEVT`, `INTAKE`, `ADMITTED`, `SCADA`,
`PLANNING`, `DEVCTL`, `CAPABILITY`; KV `og-guard`, `og-ackcorr` (02 §5). Runtime scenarios 01 §6.15–§6.17. Service
`safe-stop` (namespace `og-safestop`, register V-24); deployables `contracts-rt`, `contracts-batch`.

## 5. Unresolved cross-document issues (for the owners named, and for the register)

| # | Owner | Issue | What 01/02 now say |
|---|---|---|---|
| X-1 | `03-decision-engine.md` | §3.4/FR-DE-013 design target 120 s; §8.1 cycle steps S0–S11 in one partition process; §8.13 DM-07 "no verdict in 100 ms, 3 times"; §8.14 "No verdict within 100 ms counts as a veto" and "3 consecutive such cycles request a scoped safe stop"; §8.15 `seq` "assigned by the dispatcher", TTL max(3 cycles, 30 s), fallback while "heartbeat less than 15 min old"; §9.3 hourly anchor; FR-DE-012 "8 partition shards"; FR-DE-084 "re-partition"; §11.3 AI proposals | Register V-34 (240 s), `ADR-021` (allocator/shard split), R31/V-35 (TIMEOUT ≠ VETO), V-05/V-06/V-07, V-23, 01 §11.2 (20 shards), eligibility-only topology changes, R49 |
| X-2 | `03` and `../03-security` | `03` §8.14 "the guardian never modifies commands" vs `../03-security` §6.3 "clipped (modified to the nearest admissible values)" | 01 §5.5 and 02 §2.6 record verdicts per command without settling clip semantics; the two owners must agree one rule |
| X-3 | `05-failure-modes-and-recovery.md` | §2.2 envelope names (`cmd_id` UUIDv7, `valid_for_s`, `issuer`), the dispatcher publishing commands, re-issue after U(0, 1 s), lease TTL 10 s / renewal 3 s, "guardian holds a separate safe-stop key"; §2.3 guardian timeout row; §2.14 stream table; C9 "no synchronous DB dependency"; §4.2 C13 | 02 §3.2 names and "Earlier names" mapping; 01 §8.1; register V-01; R16 (key held by the SSA); 02 §5; 01 §8.3 |
| X-4 | `06-platform-and-operations.md` | §2.6 epoch = KV revision and "lower than the highest seen"; §1.7 submissions over HTTPS 8443; §1.8 one dispatcher and one guardian replica, no `safe-stop`, no `contracts-rt`/`-batch` split, no `og-safestop` namespace; §4.5 7-d/30-d Limits streams; §4.8 hash formula and 15-min anchors; `ADR-505`/`-506`/`-507` amendments; §3.5 cites "02 §3.7" for hub fallback schedules (the contract is 02 §2.2 and §3.5; §3.7 is now the `DeviceAdapter` port) | `ADR-023`; 01 §8.1; 01 §11.2, §5.12, `ADR-028`, register V-24; 02 §5; 01 §8.3, register V-23; 01 §17.2 |
| X-5 | `07-scada-integration.md` | §7.3 latched restrictive states in NATS KV; §7.10 subject names; §6.8 guardian-unavailable row ("not even a stop … queued"); §7.5 active + standby adapters on the node; `ADR-072` demo stack | `ADR-076` verdict (PostgreSQL); 02 §5.6 mapping; R16/01 §5.12; `ADR-071` verdict; register Q11 |
| X-6 | `../03-security/02-security-architecture.md` | §7.1 60-min command keys vs register V-10 (24 h); §7.2 `jti` as UUIDv7 vs the derived UUIDv8 command id; `leader_epoch` vs `epoch {cls, shard, gen}` + `gepoch`; DV-07 single floor vs floors per (issuer class, shard); DV-08 needs an exception for scope states (no `exp`); §7.5 "lower than the highest seen"; §6.1 namespace `og-safety` vs register V-24; §6.5 out-of-band path to the guardian vs R16's SSA; §12.2 chain formula; QSE-desk role code (R25) | 02 §3.2–§3.3; 01 §8.2, §8.3, §10, §5.12 |
| X-7 | `../04-ui/01-ui-ux-specification.md` | Aligned during this pass: the console shows 02 §2.3's states verbatim; 02 kept the v0.2 names, adopted the UI's `RECORDED` for `SHADOW` and added the internal `SUBMITTED`; the fields `A8-ui.md` asked for (ISO-instruction caller, shift log, handover, contacts) are in 02 §1.2 and §1.3. Remaining for the UI owner: none from 02 | 02 §1.2, §1.3, §2.3 |
| X-8 | `../01-product/02-functional-requirements.md` | NFR-207 availability not scoped by environment (ARC-036) | 01 `NFR-010` states node vs production behaviour |
| X-9 | `../05-testing/*` | New cases: counter and latch resynchronization after PITR (`NFR-040`), stream-full behaviour (`NFR-044`), forced kubepods OOM (`ADR-030`), stop to a reconnecting hub (`NFR-019`), shard handover (02 §2.12), stop with the guardian isolated (`NFR-036`, unblocks TC-SEC-032 variant B), SHADOW publishes nothing (`NFR-043`); G3 trace-hash criterion (ARC-029) | 01 §1.12, §9.1 |
| X-10 | `00-README.md` | ADR count (ARC-064) | 01 §17: 35 architecture ADRs plus verdicts on 19 proposed ones |
| R-1 | Register (proposal) | May `scada-gateway` trigger `safe-stop` for authenticated utility emergency-stop points while both guardian replicas are down? This extends R16's trigger list | 01 §18 item 1 (default today: latched, executed by the guardian on recovery; the utility's own stop path and the out-of-band trigger act meanwhile) |
| R-2 | Register (proposal) | Double fault PostgreSQL + a leader: accept autonomy for that shard, or fund a second generation source | `ADR-023` consequence |
| R-3 | Register (proposal) | Register V-29 leaves a gap between `ONLINE` (≤ 2 × cadence) and `SILENT` (> 3 missed reports); 02 adds a transitional `LATE` state (eligible, counted online, flagged) | 02 §2.1 |
| R-4 | Register (note) | R32's lease-bucket `sync: always` may be server-wide in the pinned NATS release | 01 §10: dedicated small JetStream domain if needed; safety does not depend on it |
| R-5 | Register | R14 can be marked resolved once `06` §1.8 is regenerated from the Helm values | 01 §14.2 |
| C-1 | `06-reviews/05-claims-verification.md` | Landed during this pass and was applied (§6); nothing in it contradicts 01 or 02 after the updates. Items outside 01/02 (EEA charging policy, IIN bits, DNP3 stack choice) belong to `03`, `05`, `07` | 02 §1.1, §1.2, §4.3; 01 §5.11, `NFR-045`, `ADR-032`, `ADR-072` verdict |

## 6. Register revision and claims check applied during this pass

The register (v0.2 text, revised at 10:15) and `../05-claims-verification.md` changed while this pass was running. Both
were re-read and applied; nothing was left inconsistent.

| Source | Change | Applied in |
|---|---|---|
| Register R17 (revised); claims check item 2 | The guardian invariant is now "telemetered AS capability ≤ ledger-free capacity for the product's duration": ERCOT creates a proxy AS offer for every qualified resource at every SCED run and caps awards by telemetered capability, so telemetered capability, not offers, is the boundary; real-time AS offers and an energy bid cover all telemetered capability; an awarded AS obligation survives `OUTL` | 01 `NFR-045`, `ADR-032`, change table row 13; 02 §4.3 (offers, awards) |
| Register R17 (revised); claims check items 1, 3 | ALR: online Non-Spin and ECRS arrive inside the 4-s UDSP with no separate deployment message; NCLR: SCED still awards AS each interval, deployment only on XML, held until recall, 95–150% against the 15-min meter interval before the instruction, two failures in 365 days disqualify for ≥ 6 months | 01 §5.11 (`ERCOT_AS`); 02 §1.2 (variants), §4.3 |
| Claims check items 4, 15; register R27 (verified note) | ALR-ADER: one load zone, one LSE, one DSP; NCLR-ADER: every LSE's acknowledgment; premises ≤ 100 kW belong to the submitting LSE; DSP acknowledgment per premise (NOIE in NOIE zones); limits 500/100/100 MW, ≤ 90% per QSE; premise and device series and allocation factors archived (sharing is register Q12) | 02 §1.2 `AderResource`; examples moved to a competitive-area zone (R27a) |
| Claims check item 14 | UDSP every 4 s; base ramp a configurable 4-min linear ramp (training value, not protocol); HDL/LDL from telemetered power ± 5 × ramp rate within MPC/LPC | 02 §4.3 |
| Register R20 (revised); claims check item 5 | SB 231: TEEEF units mobile, movable in < 12 h, ≤ 5 MW; leases need prior commission authorization; §39.918 does not apply to co-ops or municipal utilities | 01 §5.11; 02 §1.1 (`MobileAsset`, `MobileDeployment`), §2.10 |
| Claims check item 9; register R27 | Austin Energy tolling: reserved MW under the utility's charge/discharge control as a hard external schedule, with a ramp | 02 §1.2 (`TOLLING`) |
| Claims check item 10 | TDU storage contracts: PURA §35.153; load-ratio share of 100 MW; prior PUCT authorization; competitive bidding; discharge only on TDU direction; 16 TAC §25.58 still proposed | 01 §5.11; 02 §1.2 (`TDU_SB415`) |
| Claims check item 11 | ComEd PLC includes PJM load-drop estimates and losses; demand-response reductions are added back (assumption to verify per customer) | 02 §1.2 (`PJM_CAPACITY`) |
| Claims check item 12 | Step Function `dnp3`: no Python binding, non-production public licence; OpenDNP3 Python wrapper without TLS by default | 01 §17.2 (`ADR-072` verdict — inputs for the week-1 spike) |
| Register Q1 (revised default) | Co-signer or approver: shift supervisor for bank and zone, executive on call (or a second shift supervisor) for fleet; the system admin never approves dispatch (SoD-03) | consistent with 02 §6.1 (`SAD` not a dispatch approver) and 01 §8.4 (cites Q1) |
| Register Q7 / V-33 (answered by evidence) | ECRS 1 h; Non-Spin 4 h (2 h once NPRR1309 is implemented); Regulation and RRS 30 min | cited as register V-33 in 01 §6.3; no restated value in 01 or 02 |
