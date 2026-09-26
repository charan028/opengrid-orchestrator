# Resolution A7 — Security (`03-security/01-threat-model.md`, `03-security/02-security-architecture.md`)

Status: v1.0 · 2026-09-25 · Owner: security architect (threat model and security architecture) · Scope: every review
finding whose Location cites `03-security`, every finding the red team raised (all of them attack this set), and every
register item assigned to the two documents (`../../00-decision-register.md` v0.2). Documents changed:
`03-security/01-threat-model.md` → **v0.2**; `03-security/02-security-architecture.md` → **v0.2**. Both carry a "Changes in
this version" table.

**Method.** Each finding was treated as a claim: the quoted evidence was located in the v0.1 text (or shown to be absent)
and the reasoning checked before a fix was written; regulatory claims were checked against
`../05-claims-verification.md`. Fixes follow the register, which wins over every document; numbers it owns are cited as
"register V-nn". No finding removed a customer or service type (D0a); no fix refuses a service type or uses a business
reason (D0b; the only new clip codes are safety and integrity codes); no personal-data sharing was added (D5; register Q12
keeps its default). Where another owner had already changed a shared contract (`02` §3 by A2, the console by A8), this set
was aligned to it rather than diverging.

**Legend.** Verified: **Yes** (evidence and reasoning confirmed against this set) · **Partly** (evidence confirmed in part,
or the defect sits mainly in another document) · **No**. Disposition: **Fixed** · **Partly fixed** · **Rejected** ·
**Deferred R2** · **User decision Qn** · **No change needed**. "TM" = threat model, "SA" = security architecture;
"(cross-doc)" = remaining work in another document, listed in §4.

## 1. Findings

### 1.1 Red team (`03-red-team-report.md`) — every finding targets this set

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| RT-001 | Partly — the substance holds: v0.1 SA §6.1/P-6 made `guardian` the only signer and fail-closed, and the §6.5 out-of-band path was "a minimal `guardian` kill endpoint", so no stop was possible with `guardian` down. The cited wording is wrong for this set: SA §6.5 never said "queued" and SA §6.8 is the per-customer-type envelope table; the "accepted as queued … after recovery" text is `07` §6.8 and TC-INT-712 | Fixed | SA §6.5 (never queued), §6.9 CTL-147, §7.4 DV-17/DV-18, FR-SEC-204, IRP-15; TM SO-9, TH-162…TH-167, AT-M | `07` §6.8, TC-INT-712 and `05` §2.2/§2.10/§4.2 C13 must follow R16 (cross-doc) |
| RT-002 | Yes — v0.1 SA §7.7: the epoch bump was a key set "on a retained topic writable only by the signer", under the dispatch intermediate the guardian holds; no peer could stop or invalidate a rogue guardian | Fixed | SA §6.10 CTL-152, DV-21, FR-SEC-205, IRP-04, IRP-15, SoD-09; TM TH-168, TH-169, RR-17 | The epoch authority can only invalidate, never add trust, so its own key is not a second signer |
| RT-003 | Yes — v0.1 SA §10.2 step test compared the bank SCADA value supplied by the same utility that sets `BANK_LIMIT`; G-03/G-12 had no corroboration rule | Fixed | SA G-18 CTL-150, §10.2, DET-084, FR-SEC-211, IRP-07; TM TH-179, AB-20, RR-04 | Holds only on a physics-consistency failure, never for the counterparty's identity (service-agnostic) |
| RT-004 | Yes — v0.1 SA G-04 "total fleet net ramp ≤ 50 MW/min *(assumption; register Q13)*"; TM AS-01 | Partly fixed | SA FR-SEC-206, §23.1 gate 2, §6.3 intro; TM TH-185, RR-16, SC-21, §1.4 | Go-live gate and fleet-trip characterization adopted. "Stop ramp scales with MW" is adopted only for non-protective stops (≤ V-30 cap); protective stops keep fixed 30/60/120 s by register V-16, unsigned — their rate at 100,000 hubs is inside the Q13 sign-off |
| RT-005 | Yes — v0.1 SA G-07 "fleet-median frequency … cross-checked with ERCOT public data when available"; TM TH-045 rated 9, residual 2 | Fixed | SA G-07/G-08, §10.8 CTL-151, DET-085, FR-SEC-212; TM TH-045 re-rated, SC-22, AS-07 | Independent reference is the authority; hub median corroborates |
| RT-006 | Yes — v0.1 SA §19.6 "no workload can starve `guardian`" holds only inside `kubepods` | Partly fixed | SA §19.6 CTL-148, DET-086, FR-SEC-213, §23.1 gate 5; TM TH-086, RR-21 | Adapted to the host rule: host-level monitoring, control path Guaranteed or limits under the kubepods cap (R35), production on dedicated nodes. The proposed host-level memory floor for `kubepods` is **rejected**: it would reserve memory against co-resident services that must never be touched (brief §4, register R35) |
| RT-007 | Partly — v0.1 SA §12 had no degraded-mode journal at all (K3 open); the mutable local journal the red team analyses is `05` C15's | Fixed | SA §12.11 CTL-149, DET-087, FR-SEC-207, §23.1 gate 6; TM TH-178, A-27, RR-18 | Anchor every 10 s nominal, 5 min maximum (register R22, V-23); the red team's "seconds" is met by the 10-s head anchor |
| RT-008 | Yes — v0.1 SA §12.1: `DECISION` records are produced by `dispatcher`; no detector of withholding independent of its trace | Fixed | SA §10.7 CTL-154, DET-082, DET-083, §7.2 `ctx.inputs_hash`, FR-SEC-209; TM TH-182, TH-183 | `01` §5.5/§5.6 (A2) carries the verdict-bound inputs hash and the detector host |
| RT-009 | Yes — v0.1 SA §8.2 stated the licensed-SA requirement in prose only | Fixed | SA §8.2 hard gate (signed association security profile), FR-SEC-178, §23.1 gate 10; TM TH-187, RR-15, SC-10 | A TLS-only real association can exist only as a read-only monitoring association |
| RT-010 | Yes — v0.1 SA §7.6 SoftHSM2 with its PIN in a Kubernetes Secret; recovery depended on the guardian | Fixed | SA §7.6, §6.10, IRP-10; TM RR-02, AT-B, AT-J J35 | RR-02 accepted for the demo only; production HSM/KMS; the epoch-authority key never on the node |
| RT-011 | Yes — v0.1 SA §13.2 `draft_call_from_text` had no persistent AI-drafted flag | Fixed | SA §5.8 (AI-drafted rule), §7.2 `ctx.ai_drafted`, §13.2, CTL-121, CTL-146; TM TH-134, RR-14 | Console parts are A8's (UI-SEC-16) |
| RT-012 | Yes — v0.1 SA §5.8 "cumulative total per principal over 15 minutes (DET-037)" | Fixed | SA G-17 (CTL-039), §5.8, §5.9, DET-088; TM TH-180, AB-19 | Register V-14 |
| RT-013 | Yes — v0.1 SA §7.5 "Leader-epoch fencing (decision register R8, proposed)", "lower than the highest it has seen", read from the shared KV | Fixed | SA §7.5 (equality at commit, fail closed), §7.2, DV-07, FR-SEC-117; TM TH-161 | Register R32 |
| RT-014 | Yes — v0.1 SA §10.2 had no rule for correlated silence | Fixed | SA §10.2, §10.3, DET-089, FR-SEC-146; TM TH-181 | — |
| RT-015 | Yes — v0.1 SA §12.10 sealed periods with no rule for estimated lines | Fixed | SA §12.10 (CTL-142), DET-090, FR-SEC-176; TM TH-184, RR-05 | Auditor attestation above a stated threshold *(assumption)* |
| RT-016 | Partly — SA §14 stated "Build L2 on the demo pipeline" (confirmed); the "cosign keyless-OIDC via a GitHub org" profile signing is not in this set (SA §9.1 names a CI configuration-signing key) | Fixed | SA §14 (SLSA L3 for `guardian` and `safe-stop` images on the demo, approval-bound bundle loading, demo pre-apply verification), CTL-083, CTL-088; TM AT-H, AT-L, RR-17 | `06` owns the pipeline (cross-doc) |
| RT-017 | Yes — v0.1 SA §19.5 "if 8883 is ever opened … LAN only (register Q21, proposed)", unmonitored | Fixed | SA §19.5 scope gate, §11.2, DET-091, FR-SEC-220, §23.1 gate 9; TM TH-186, RR-01, AS-05, C-01 | Register Q21 default applied; the load generator now connects from the LAN (R35, Q24) |
| RT-018 | Yes — v0.1 SA §6.5 out-of-band path ended on the guardian; §17.1 alerts ran only through platform channels | Fixed | SA §17.1 dead-man channel, DET-092, FR-SEC-221, §6.9 status endpoint; TM TH-170 | ALR-290 is `06`'s (cross-doc) |
| RT §4 (Safe-Stop Authority design) | Yes — the design fits R1 and R16 | Fixed | SA §6.9, §6.10, §7.1, §7.4, §19; TM Z-14, TB-16, C-15…C-17, DF-51…DF-54, A-25, A-26 | Aligned with `02` §3.1/§3.2 (A2): SSA publishes `ENGAGED`, `guardian` publishes `RELEASED`, scope states carry no `exp`; one strengthening: `guardian` also publishes its own `ENGAGED` if the SSA's state is not on the retained topic within one cycle |
| RT §4.3 (SSA threat analysis) | Yes | Fixed | TM §11 AT-M table, TH-162…TH-169 | One extra threat found: stop–release oscillation if the SSA re-asserts over a newer release (TH-165) |
| RT §5 (residual risks) | Yes | Fixed | TM §14 RR-01…RR-05, RR-14, RR-15 updated; RR-16…RR-18 added; §14.1 mapping | Plus RR-19 (single-person stop), RR-20 (SSA key), RR-21 (host co-tenants) |
| RT §6 (go-live gates) | Yes | Fixed | SA §23.1; TM §14.2 | Demo: gates 1, 6, 9, 12; any real hub, counterparty, market or personal data: all twelve |
| N-01…N-15 (narratives) | Yes — each maps to a finding above | Fixed | TM §9.T–§9.W, AT-M, AB-18…AB-21 | N-01 → TH-068, TH-163, AB-18; N-02/N-08 → TH-179; N-03 → TH-180; N-04 → TH-168, TH-182, TH-183; N-05/N-06 → AT-M, TH-168, TH-169; N-07 → TH-161; N-09 → TH-134; N-10 → TH-168; N-11 → TH-186; N-12 → TH-178, TH-184; N-13 → TH-086; N-14 → TH-045; N-15 → TH-187 |

### 1.2 Architecture and SRE review (`01-review-architecture-sre.md`) — findings citing 03-sec or assigned to it

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| ARC-003 | Partly — for this set the finding confirms consistency (SA already assumed batches, per-stream chains, scope broadcasts); the one gap was SA §7.3 "to be decided in an architecture ADR" | Fixed | SA §7.3 (decided, R31), §12.2 (per-stream chains with `01`'s stream names) | Global chain tip is `01`'s, fixed by A2 |
| ARC-004 | Yes — SA §7.9/FR-SEC-125 set 250 ms for a 10,000-hub batch, a third budget; SA §6.1 had no TIMEOUT semantics | Fixed | SA P-6, §3.2, §5.10 `TIMEOUT`, §6.1, §7.9, DET-097, FR-SEC-124/125; TM TH-172 | Register R31, V-35 |
| ARC-008 | Yes — SA §7.5 "the active `guardian` instance … elected through a NATS JetStream key-value lease"; no state inventory | Fixed | SA §6.1 state inventory, HA, FR-SEC-216 | Replica count stated once in `06` §1.8 (R31) |
| ARC-009 | Yes — DV-07 "`leader_epoch` ≥ highest stored leader epoch" per hub; SA §7.5 "lower than the highest it has seen" | Fixed | SA §7.2 (`epoch {cls, shard, gen}`, `gepoch`), §7.5, DV-07, FR-SEC-117 | Floors per issuer class (`EXEC`, `GUARD`, `STOP`) as `02` §3.3 |
| ARC-010 | Yes — DV-09 persisted `seq` with no resynchronization path | Fixed | SA DV-09, §12.6 `RESTORE`, §19.7, ST-22 | Register R36 |
| ARC-011 | Partly — the gap is `02`'s device contract; SA assumed scope broadcasts but did not name retained scope topics | Fixed | SA §7.2 (defers to `02` §3), §11.2 retained topics, DV-18 | `02` §3.1 rebuilt by A2 |
| ARC-012 | Partly — SA §7.8 meter blocks assumed energy registers that `02` lacked; SA had no `boot_id` | Fixed | SA §7.8 (`boot_id`, dedupe key) | Register R33 |
| ARC-017 | Yes — `02` v0.2 retained unsigned `twin/desired`; SA had no rule against actuating on unsigned retained state | Fixed | SA DV-19, CTL-153, §4.2, §11.2, DET-093, FR-SEC-208; TM TH-174 | Assigned by register R33 |
| ARC-018 | Yes — DV-14 "≤ 1 accepted command per 2 s" with no exemption; no submission idempotency | Fixed | SA DV-14, §7.5 idempotency, G-14, FR-SEC-218; TM TH-175 | Exemption narrowed to the safe direction: stops and restrictive `UTILITY` controls; a `UTILITY` command that raises output stays rate-limited |
| ARC-021 | Partly — SA §12.2 already hashed the body with RFC 8785, but `chain` = SHA-256(`prev` ‖ `id` ‖ sequence ‖ timestamp) left producer, type, parents and stream outside the hash | Fixed | SA §12.2 (formula identical to `01` §8.3), FR-SEC-172; TM TH-153 | Register R22 |
| ARC-022 | Partly — the DEK-in-the-database statement is `01` §7.1/§7.3; SA §12.7 did not say where subject keys live and SA §16.9 said "deletions propagate at expiry", so SA did not prevent resurrection | Fixed | SA §12.7, §16.8, §16.9, CTL-140, FR-SEC-215; TM TH-177 | Register R38; TC-DR-017 kept |
| ARC-024 | Yes — (1) DV-14 had no stop exemption; (2) SA §6.6 let a stop override a fallback schedule only if it arrived, and v0.1 stops expired after 300 s with no retained state; (3) the "separate safe-stop key" held by `guardian` is `05`'s | Fixed | SA DV-14, DV-18, §6.5, §6.6 (V-07 gate), FR-SEC-217; TM TH-118 | The proposed escrow of pre-signed stops is not adopted: register R16 considered and did not choose it (expires in a long outage; does not contain a rogue guardian) |
| ARC-027 | Yes — SA §7.3 "to be decided in an architecture ADR"; hourly keys vs other documents' key models | Fixed | SA §7.1 (24-h keys, SSA and epoch-authority hierarchy), §7.3, §7.6, §7.7; V-36 ES256 only | The ADR log itself is `01`'s |
| ARC-033 | Partly — the alert budget is `05`/`06`'s; in SA, DET-013 paged S1 on every stale-epoch rejection and S1 promised a 24/7 page | Fixed | SA DET-013, §17.2 paging budget | Register R41, V-25, Q15 |
| ARC-036 | Partly — SA §7.6 needed an HSM operation every hour; SA §4.2 did not pre-issue certificates | Fixed | SA §4.2, §6.1, §7.6 (pre-issued, overlapping), §5.5 (OPA per batch) | Dependency matrix is `01`'s |
| ARC-037 | Yes — SA §7.9 one process for a 10,000-hub batch; no class priority | Fixed | SA §6.1 priority queues, process pool, §7.9, FR-SEC-125 | Register R31 |
| ARC-041 | Partly — SA §6.2 guardian modes were not mapped to `05`'s fleet modes | Fixed | SA §6.2 | Register R42 |
| ARC-043 | Partly — SA did not state the pre-image before signing | Fixed | SA §12.2 | Register R22 |
| ARC-045 | Yes — SA §12.3 cadences marked *(assumption)*; nightly full verification | Fixed | SA §12.3 (V-23; interim bucket, Q4), §12.9 (incremental) | — |
| ARC-049 | Partly — the contradiction is `01`'s; SA §13 lacked the constraint-set semantics | Fixed | SA §13.1; TM AB-17 | Register R49 |
| ARC-051 | Partly — SA already had a 64-bit `seq`; epochs had no width | Fixed | SA §7.2 (`bigint`) | Register V-40 |
| ARC-052 | Yes — SA §11.2 "Ban 5 min after 15 connects per minute"; DET-009 flapping ban | Fixed | SA §11.2, DET-009, FR-SEC-152 | Register V-21 |
| ARC-055 | Partly — the test-found conflicts that land here: NF-Q16 (AI budget), NF-Q20 (namespaces), C-07 (algorithm), C-18 (anchor cadence) | Fixed | SA §13.8, §19.1, §7.1, §12.3 | Register V-22, V-24, V-36, V-23 |
| ARC-056 | Yes — SA §6.1 "own inputs" but no rule on a common-mode estimator | Fixed | SA §6.1, G-01, FR-SEC-222; TM TH-176 | Register R31 |
| ARC-061 | Partly — SA §5.9 "HTTP 429" could read as a refusal of a valid call | Fixed | SA §3.2, §5.9, §11.1, FR-SEC-141 | Register R48 |

### 1.3 Grid, ERCOT market and SCADA review (`02-review-grid-market-scada.md`)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| GRD-001 | Partly — SA §8.4 precedence had no rank for ERCOT instructions (confirmed); the control-law part is `03`'s | Fixed | SA §8.4 rank 5, §5.6 `IsoInstruction`, G-15, FR-SEC-181; TM TH-188 | Claims check #1 confirms the ALR/UDSP rule |
| GRD-002 | Partly — no guardian invariant on ERCOT-visible capability (confirmed); telemetry is `07`/`03`'s | Fixed | SA G-15 CTL-155, DET-095, FR-SEC-210; TM TH-188 | Claims check #2: proxy offers confirmed, broader than stated |
| GRD-003 | Yes — SA G-03 "Net loading ≤ 95% of rating" with no unit or reactive power | Fixed | SA G-03 (kVA or per-phase current, P and Q add-back), §10.2, FR-SEC-121 | Register R18 |
| GRD-004 | Partly — SA A-29 storm hold had no EEA rule; the reserve-raise default was `05`/`03`'s | Fixed | SA G-16 CTL-155, A-29, DET-095, FR-SEC-210; TM TH-189 | Register R19; claims check #7: the NPRR1002 duty binds registered ESRs only, so G-16 is labelled operator policy |
| GRD-005 | Partly — SA §6.7 already forbade a remote close but kept a Tier-2 remote permissive and no statute shape | Fixed | SA §6.7, §6.8, A-41, FR-SEC-200; TM TH-060, SC-16 | Register R20; claims check #5 confirms §39.918 and its SB 231 limits |
| GRD-007 | Yes — SA G-03 net loading ≤ 95% implies headroom = 0.95 × rating − measured load, which counts the fleet's own charging | Fixed | SA G-03 (same add-back formula as `dispatcher`), FR-SEC-122 | Register R18 |
| GRD-009 | Yes — SA G-07 acted only on non-firm increases; nothing froze integrators or trust penalties | Fixed | SA G-07, §10.3, §7.8, FR-SEC-123, FR-SEC-147 | Register R26 |
| GRD-010 | Yes — SA §6.5 "Approval to engage: Tier 2 … except a risk-reducing stop"; A-16/A-17 "A" | Fixed | SA §5.2, §5.8, §6.5, SoD-11, DET-079, DET-080, FR-SEC-136, FR-SEC-214; TM TH-068, TH-163, AB-18, RR-19 | Register R3 amended; co-signers are register Q1 (default applied) |
| GRD-011 | Partly — SA §6.6 issued fallback schedules for every hub with firm obligations, with shared boundaries and no stop gate; no counterparty stop path | Fixed | SA §6.6 (V-07), DV-20, CTL-156, FR-SEC-219; TM TH-173, SC-24 | Register R25, V-07 |
| GRD-012 | Yes — SA G-04 "total fleet net ramp ≤ 50 MW/min" with no firm or ISO rule | Fixed | SA G-04, G-05, FR-SEC-119; TM AB-21 | Register V-30 (unsigned) |
| GRD-013 | Yes — guardian ramp caps and ADER ramp telemetry were unlinked | Fixed | SA G-15 (telemetered ramp = min(physical, guardian-permitted share)), FR-SEC-210 | Register V-30; claims check #14 |
| GRD-019 | Yes — SA A-41 gave `OP`+`APR` the remote energize permissive; §6.7 "on the Orchestrator side a Tier-2 approval" | Fixed | SA A-41 (readiness and request only), §6.7, FR-SEC-200 | Register R20 |
| GRD-020 | Partly — SA CTL-091 gated firmware versions but did not read back settings | Fixed | SA G-13, CTL-091, DET-096, FR-SEC-223; TM SC-23 | Register R26 |
| GRD-025 | Yes — SA §6.4 kill-switch ramp with no sequencing or frequency gating | Fixed | SA §6.4, §6.5; TM TH-171 | Register V-16 |
| GRD-027 | Yes — SA G-12 "older than 24 h … export 0" | Fixed | SA G-12; TM SC-11 | Register R28 |
| GRD-032 | Yes — SA G-02 (80%/90% of kVA) differed from the dispatcher's κ = 1.0 | Fixed | SA §6.3 intro (one distribution-defaults source), G-01, G-02 | Re-running Examples A–C is `03`'s |
| GRD-033 | Yes — SA G-03 zero reverse flow only "at a bank" | Fixed | SA G-03 (feeder heads, unconfirmed regulators); TM SC-11 | Register R28 |
| GRD-041 | Yes — SA §5.8 Tier 1 for "any change to a customer's declared capacity" | Fixed | SA §5.8 (automatic downward re-declarations and ERCOT telemetry and COP updates) | Register R3 amended |
| GRD-042 | Yes — SA §16.5 "aggregates only until a lawful basis is confirmed"; claims check #4 confirms GD 3.3 §5.d/§5.e | User decision Q12 | SA §16.3 PU-05, §16.5, FR-SEC-168, FR-SEC-170; TM SC-07 | Default kept: ERCOT lanes simulated, no per-premise data leaves the platform |
| GRD-047 | Yes — SA G-03/CTL-033 blocked need-window charging with no reserve-recovery rule | Fixed | SA G-03, FR-SEC-122 | Register R28 |
| GRD-053 | Yes — SA §8.3 and FR-SEC-180 allowed direct operate only on the emergency stop | Fixed | SA §8.3, §8.4, §7.5, FR-SEC-180 | Register R29 |
| GRD-054 | Partly — SA did not require `COMMAND_SEQ`, but did not say when it applies | Fixed | SA §7.5 SCADA row, §8.4 | Register R29 |
| GRD-056 | Yes — SA §6.8 "ECRS … defaulting to the stricter 2 h" | Fixed | SA §6.8; TM §13.1 | Claims check #6: ECRS 1 h (NPRR1282), Non-Spin 4 h switching to 2 h at NPRR1309 |

### 1.4 Judging and product review (`04-review-judging-and-product.md`)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| JDG-006 | Partly — SA assumed step-ca, policy-controller, Redis, Loki, Tempo and an egress proxy on the node; R35's demo profile omits them | Fixed | SA AS-A9, §4.1, §11.1, §14, §17.1, §19.2 | Sequencing only (R21): each control states its demo realization |
| JDG-015 | Partly — same as JDG-006 | Fixed | SA AS-A9 | — |
| JDG-007 | Yes — K3 open in SA; anchors "every 5 min *(assumption)*" | Fixed | SA §12.2, §12.3, §12.11 | Register R22 |
| JDG-008 | Partly — SA §8.2 demo exception assumed TLS from the DNP3 library | Fixed | SA §8.2 (TLS terminator sidecar if the stack lacks TLS; hard gate) | Claims check #12; protocol breadth is `07`'s |
| JDG-021 | Yes — SA §5.5 implied a per-decision OPA call on the command path | Fixed | SA §5.5 (one evaluation per batch), §6.1 (process pool) | Register R31 |
| JDG-027 | Yes — SA §13.1 declines personal-data requests on the node (Q17) | No change needed | SA §13.1 | Staging it as a privacy beat is `01-product`/UI's |

## 2. Register items assigned to this set

| Item | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| D5 | Yes | Fixed | SA §16 (V-18, V-19, Q12 default, R38) | — |
| R1 | Yes | Fixed | SA P-2, §3.2, §6.1 | Only `guardian` moves MW; SSA stop-only; epoch authority invalidate-only |
| R3 (amended) | Yes | Fixed | SA §5.2, §5.8, §6.5, SoD-11; TM TH-068, TH-163, RR-19, AB-18 | Register Q1 open (defaults applied) |
| R4 (amended) | Yes | Fixed | SA §6.4, §6.5, FR-SEC-127 | — |
| R5 | Yes | No change needed | SA §9.4, §10.1 | Already stated |
| R8 | Yes | Fixed | SA §7.5 | No longer "proposed"; with R32 |
| R10 (amended) | Yes | Fixed | SA §9, §9.5, CTL-133, FR-SEC-196 | With R47 |
| R15 | Yes | Fixed | SA A-44, §5.10 `SAFETY_PAUSE` | — |
| R16 (closed) | Yes | Fixed | SA §6.5, §6.9, §6.10, CTL-147, CTL-152, DV-17, FR-SEC-204, FR-SEC-205; TM SO-9, AT-M, SC-20 | — |
| R17 | Yes | Fixed | SA G-15, §5.6, §8.4, CTL-155, FR-SEC-181, FR-SEC-210; TM TH-188 | — |
| R18 | Yes | Fixed | SA G-03, §10.2 | — |
| R19 | Yes | Fixed | SA G-16; TM TH-189 | Operator policy (claims check #7) |
| R20 | Yes | Fixed | SA §6.7, A-41, A-45, FR-SEC-200; TM SC-16 | — |
| R22 | Yes | Fixed | SA §12.2, §12.3, §12.11, CTL-149 | K3 resolved as "both" |
| R25 | Yes | Fixed | SA §5.1 `QSD`, A-46, DV-20, CTL-156; TM TH-173, SC-24 | — |
| R26 | Yes | Fixed | SA G-07, G-13, §10.3, CTL-091, DET-096, FR-SEC-223; TM SC-23 | — |
| R28 | Yes | Fixed | SA §6.3 intro, G-01…G-03, G-12, §10.1, DET-028 | — |
| R29 | Yes | Fixed | SA §7.5, §8.3, §8.4, FR-SEC-180 | — |
| R30 | Yes | Fixed | SA §7.2, §7.5 (shard terms) | — |
| R31 | Yes | Fixed | SA §6.1, CTL-029, FR-SEC-216, FR-SEC-222 | — |
| R32 | Yes | Fixed | SA §7.2, §7.5, DV-07, DV-09, FR-SEC-117, FR-SEC-218 | — |
| R33 | Yes | Fixed | SA §7.2, DV-19, CTL-153 | Aligned with `02` §3 |
| R34 | Yes | Fixed | SA §4.2, §11.3 | — |
| R35 | Yes | Partly fixed | SA §19, CTL-148, AS-A9 | The host-level floor of RT-006 is rejected by the host rule |
| R36 | Yes | Fixed | SA DV-09, §12.6, §19.7 | — |
| R37 | Yes | Fixed | SA G-09, §12.10 | — |
| R38 | Yes | Fixed | SA §12.7, §16.8, §16.9, FR-SEC-215 | — |
| R41 | Yes | Fixed | SA §17.2, DET-013 | With V-25 |
| R42 | Yes | Fixed | SA §6.2 | — |
| R45 | Yes | Fixed | SA §17.1 | — |
| R46 | Yes | Fixed | SA ST-15, FR-SEC-163 | — |
| R47 | Yes | Fixed | SA §9.5 | — |
| R48 | Yes | Fixed | SA §3.2, §5.9, §11.1, FR-SEC-141 | — |
| R49 | Yes | Fixed | SA §13.1 | — |
| R50 | Yes | Fixed | This file §1.1 | RT-008, -009, -011, -012, -014…-018 applied here |
| K3 | Yes | Fixed | SA §12.11 | Register R22 |
| K5 | Yes | Partly fixed | SA §6.4, §6.5, G-04 | Design per V-16/V-30; values unsigned (Q13) |
| K11 | Yes | Fixed | SA §6.9 | Register R16 |
| V-05…V-11 | Yes | Fixed | SA DV-08, §6.6, §4.2, §4.1, §7.1, §7.7, §6.9 | V-05 read as applying to commands; scope states carry no `exp` (see §4, item 11a) |
| V-12…V-15 | Yes | Fixed | SA §5.8, FR-SEC-133, FR-SEC-136, FR-SEC-214 | — |
| V-16, V-17 | Yes | Fixed | SA §6.4, §6.5 | — |
| V-18, V-19 | Yes | Fixed | SA §16.5, §16.6, §16.8, DET-077 | — |
| V-21 | Yes | Fixed | SA §11.2, DET-009, FR-SEC-152 | — |
| V-22 | Yes | Fixed | SA §13.8, FR-SEC-191 | — |
| V-23 | Yes | Fixed | SA §12.3, §12.11 | — |
| V-24 | Yes | Fixed | SA §19.1, §19.2 and throughout | `og-safety` → `og-guardian` and `og-safestop`; `og-north` and `og-scada` folded into `og-edge` |
| V-25 | Yes | Fixed | SA §17.2 | — |
| V-30 | Yes | Fixed | SA G-04, G-05, G-15 | — |
| V-35 | Yes | Fixed | SA §6.1, §7.9, FR-SEC-125 | — |
| V-36 | Yes | Fixed | SA §7.1, §7.2 | — |
| V-37 | Yes | Fixed | SA §5.1 (aliases; `QSD`, `FSE`) | UI alias `QSE` = `QSD` |
| V-40 | Yes | Fixed | SA §7.2 | — |
| Q1 | — | User decision Q1 | SA §5.3 SoD-03, §5.8, §6.5 | Default co-signers applied; `SAD` as fleet co-signer needs the narrow SoD-03 exception |
| Q2 | — | User decision Q2 | TM SC-02, SC-20; SA AS-A6 | — |
| Q7 | Yes | Fixed | SA §6.8; TM §13.1 | Default = claims-check value (ECRS 1 h) |
| Q11 | — | User decision Q11 | SA §8.2, FR-SEC-178; TM RR-15 | Hard gate applies either way |
| Q12 | — | User decision Q12 | SA §16.5; TM SC-07 | Default kept |
| Q13 | — | User decision Q13 | SA FR-SEC-206, §23.1; TM SC-21, RR-16 | — |
| Q17 | Yes | No change needed | SA §13.1 | Default: decline on the node |
| Q20 | — | User decision Q20 | SA `FSE`, A-45 | — |
| Q21 | — | User decision Q21 | SA §19.5, FR-SEC-220 | Default: LAN-only, monitored |
| Q24 | — | User decision Q24 | SA §19.2, §19.5 | Default: this workstation as load-generator host |
| C-07, C-12, C-18, C-24 | Yes | Fixed | SA §7.1, §5.1, §12.3, §5.8 | V-36, V-37, V-23, V-12/V-13 |
| NF-Q15, NF-Q16, NF-Q17, NF-Q19, NF-Q20 | Yes | Fixed | SA §11.2, §13.8, §12.3, §6.9, §19.1 | V-21, V-22, V-23, R16, V-24 |
| X-2 (A2: guardian clip semantics) | Yes — SA §6.3 "clipped (modified to the nearest admissible values)" vs `03` §8.14 "the guardian never modifies commands" | Fixed | SA §6.3 intro | A clip is a verdict; the shard re-submits the clipped batch in the same tick, so every signed command equals a traced proposal |
| A8 items 1, 2, 4, 5 | Yes | Fixed | SA §5.1 (alias `QSE` = `QSD`), §5.2 (A-50 SIM Lab, `UTL` portal), §6.5 (reason list, `STOP_RECLASSIFIED`), §6.9 (token holders, second-token co-sign, status endpoint) | CTL-037 re-pointed at the SSA |

## 3. New IDs

- **Threat model:** TH-162…TH-189; A-25…A-27; TB-16, TB-17; Z-14; C-15…C-19; DF-51…DF-56; SO-9; AT-M; AB-18…AB-21;
  SC-20…SC-24; RR-16…RR-21; AS-07, AS-08; §14.1, §14.2.
- **Security architecture:** CTL-147…CTL-156 (147 SSA, 148 host-level memory-pressure monitoring, 149 signed local
  journal, 150 counterparty-limit corroboration, 151 independent grid reference, 152 epoch authority, 153 signed-input-only
  actuation, 154 withholding and narrative integrity, 155 ISO boundary and emergency posture, 156 direct counterparty stop
  path); FR-SEC-204…FR-SEC-223; DET-078…DET-097; IRP-15, IRP-16; ST-21…ST-25; G-15…G-18; DV-17…DV-21; A-44…A-50; SoD-11…
  SoD-13; roles `QSD`, `FSE`; P-12, P-13; AS-A8, AS-A9; §6.9, §6.10, §10.7, §10.8, §12.11, §23.1.
- The red team's proposed numbers were used for its controls and requirements: CTL-147…CTL-151, FR-SEC-204…FR-SEC-207,
  DV-17, SC-20.

## 4. Unresolved cross-document issues

| # | Document (owner) | Issue | What this set requires |
|---|---|---|---|
| 1 | `02-architecture/07-scada-integration.md` | §6.8 `guardian`-unavailable row ("not even a stop … accepted as queued") and TC-INT-712 | R16: the stop executes through the Safe-Stop Authority within one cycle; only the release waits for `guardian` (SA §6.5) |
| 2 | `02-architecture/05-failure-modes-and-recovery.md` | §2.2 issuer precedence and HUB-R06 ("`guardian`'s separate safe-stop key"), §2.10 `guardian` row, §4.2 C13 (kill switch "still" signed only by `guardian`), §2.3 timeout row | SSA per SA §6.9; TIMEOUT is not a veto (R31) |
| 3 | `02-architecture/02-domain-model-and-interfaces.md` | §3.1/§3.2 say `keys/set` is "signed by the dispatch intermediate — from the guardian's rotation or the dispatch-key epoch authority", relayed by `device-gateway`; §3.3 cites DV-01…DV-17 | An epoch advance is signed by the epoch-authority key (dispatch root, `dispatch-epoch` EKU) that `guardian` cannot use, and is published by a dedicated epoch-authority identity with publish rights on `keys/set` only (add it to the ACL table); DV rules now run to DV-21 (SA §6.10, DV-21). A cease-export variant of the scope state is optional (DV-17) |
| 4 | `02-architecture/01-system-architecture.md` | §5.12/`ADR-025` publisher rule for `ENGAGED` | SA §6.5/§6.9 add: `guardian` also publishes its own `ENGAGED` if the SSA's state is not on the retained topic within one control cycle |
| 5 | `02-architecture/03-decision-engine.md` | §8.16 engage tiers (bank Tier 1, zone/fleet Tier 2); A-DE-32; the utility `ESTOP` "without the kill-switch ramp" | Amended R3 (single-person engage); V-16 protective ramps apply to utility stops; clip semantics now match §8.14 |
| 6 | `02-architecture/06-platform-and-operations.md` | Namespace and budget tables; monitoring; pipeline | Add `og-safestop` (`safe-stop` ≥ 2 replicas, Guaranteed, top priority) with its NetworkPolicies; host-level memory-pressure monitoring with no reservation against co-resident services (CTL-148, DET-086); 8883 LAN-only with a source allow-list; SLSA Build L3 for the `guardian` and `safe-stop` images; demo pre-apply signature verification (no policy-controller); egress allow-list without the proxy on the demo profile; ALR rules for DET-078…DET-097 inside the ≤ 25 paging budget; the dead-man receiver (ALR-290) |
| 7 | `01-product/02-functional-requirements.md` | FR-SAFE-007/008 (second approver before a zone or fleet stop takes effect); role list | Amended R3; add `QSD` and `FSE` |
| 8 | `04-ui/01-ui-ux-specification.md` | Stop dialog, out-of-band console, roles | Reason codes of SA §6.5 (protective vs non-protective), co-sign by a second token holder, `QSE` alias = `QSD`, `FSE`, A-50 SIM Lab (`SIMULATION` deployments only), the `UTL` portal scope of SA §5.2 |
| 9 | `05-testing/*` | Cases and fixtures | TC-SEC-032 variant B verdict unblocked (FR-SEC-204); TC-SEC-107/108 extended inside the pre-anchor window (FR-SEC-207); TC-FUN-503/505 per amended R3; new cases for FR-SEC-204…FR-SEC-223 and ST-21…ST-25; `FX-USERS` adds `QSD`, `FSE`; matrix test 18 × 50; ST-15 host pressure only on a replica VM (R46); TC-INT-712 rewritten |
| 10 | `00-brief.md` (project lead) | §5 service map | Add `safe-stop` (register R16) |
| 11 | `00-decision-register.md` (project lead) | Readings to confirm | (a) V-05 is read as applying to commands; retained scope states carry no `exp` and are ordered by scope `seq` (SA DV-08 exception; `02` made the same call); (b) the Q1 default names `SAD` as a fleet co-signer, which needs the narrow SoD-03 exception of SA §5.3; (c) R3's "fleet-wide mode changes" (Tier 2) is read as loosening changes, restrictive ones following the engage rule; (d) RT-004's "stop ramp scales with MW" is adopted only for non-protective stops (V-16) — Q13; (e) RT-006's host-level memory floor is not adopted (brief §4, R35) |

## 5. Red-team claims found to be wrong or overstated

1. **"`03-security` §6.5/§6.8 … are explicit that a stop is merely 'accepted as queued'" (RT-001, N-05, §1 item 1).** Wrong
   for this set. v0.1 SA §6.5 never said "queued", and SA §6.8 is the per-customer-type envelope table. The "accepted as
   queued … enforced after recovery" wording is in `07` §6.8 (guardian-unavailable row) and TC-INT-712. The underlying
   point was right: with the out-of-band endpoint on `guardian` and `guardian` the only signer, no stop was possible without
   it.
2. **RT-007 locates the mutable local journal in `03-security` §12.3/§12.6.** v0.1 SA had no degraded-mode journal; the
   journal is `05` C15's. This set's defect was leaving K3 unaddressed, now fixed (SA §12.11).
3. **RT-016's "profile bundle is cosign keyless-OIDC via a GitHub org".** Not stated in this set (SA §9.1 names a CI
   configuration-signing key; keyless signing is `06`'s). The concentrated-CI risk is real and is now closed by
   approval-bound bundle loading (SA §14).
4. **N-03/RT-004 "at 100k hubs a 120-s fleet stop is ~250 MW/min".** Understated as an upper bound: with the whole fleet at
   full output the protective fleet stop moves up to ≈ 550 MW/min (TM §1.4). This strengthens the finding.
5. **RT-003 calls the step test "the guardian's (CTL-051)".** In v0.1 CTL-051 was owned by `fleet-state`; the physics check
   now also feeds `guardian`'s G-18, so the substance stands.
