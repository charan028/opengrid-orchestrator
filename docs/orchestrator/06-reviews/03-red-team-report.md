# OpenGrid Orchestrator — Independent Red-Team Report

Status: v0.1 · 2026-09-25 · Author: independent red team (OT/ICS and cloud offensive security) · Audience: the user, the
security, architecture, SCADA, decision-engine, platform and test authors, and the judges.

**Scope and method.** Documentation analysis only. Nothing was built, run, scanned or connected; no host (192.168.5.35 or
base.tocy-net.net) was touched. This report attacks the *specification set* under `G:\OpenGrid\docs\orchestrator\` as it
stands, looking for credible ways the system as designed could cause physical, financial, privacy or availability harm, and
for the places where the design's own words do not add up. It reads the brief, the decision register (D0–D5, R1–R16,
K1–K11, Q1–Q21), the threat model and security architecture (`TH-*`, `CTL-*`, `DET-*`, `FR-SEC-*`, `SC-*`, `RR-*`), the
system architecture and domain model, the decision engine, the SCADA integration, the failure-modes and platform documents,
and the non-functional test plan §6. Findings are written as design gaps, their consequences and their fixes; **exploit
sketches are conceptual (no working exploit code, no operational procedure)**, as a documentation red team requires.

The specification set is unusually mature: an independent `guardian` as sole signer (R1), physics-aware admission (G-01…G-14),
signed staggering, three-source measurement cross-checks, hash-chained anchored audit, propose-only AI, and a large security
test plan already exist. This report therefore does not re-litigate what is well covered. It concentrates on **residual harm
that survives the stated controls**, on **trust seams between components and documents**, and on the two questions the design
itself leaves open to this review: **R16** (stops depend on the guardian) and **K3** (audit-write vs. keep dispatching).

The binding user constraints are respected throughout: **nothing is dropped**, **dispatch is service-agnostic** (no fix here
is ever "refuse a service type"), and **no personal data is shared with third parties**. Where a control could drift into a
value judgement, that is itself flagged as a finding against the service-agnostic principle (see TH-158/CTL-144).

---

## 1. Executive summary — the three most dangerous paths

1. **The guardian is a single point of both integrity and availability, and the redundant stop path is contradicted and
   undesigned (R16/K11).** Because the guardian alone signs (R1) and fails closed, its **outage** freezes all new commands
   (a denial-of-dispatch that removes ~110 MW of controllable capacity at demo scale, ~1,100 MW at design scale, at the
   moment of an incident), and its **compromise** is bounded by nothing but its own envelope. A stop today *waits on the very
   component the emergency is about* — and the documents disagree on whether a separate safe-stop key even exists
   (`05 §2.10` asserts one; `03-security §6.5/§6.8` and `07 §6.8` say the stop is only "queued" until the guardian recovers).
2. **A compromised-but-authenticated counterparty can move real MW and mask a real overload while every check passes.** A
   distribution utility's SCADA master supplies the bank measurement, the dynamic bank limit (AO5 `BANK_LIMIT`) *and* holds
   the override — it feeds both the constraint and the measurement the guardian judges the same bank against — so it can raise
   legitimate output or hide a bank/feeder overload inside the envelope. Service-agnostic dispatch (correctly) forbids
   refusing its authentic, in-contract calls (RR-04); the guardian bounds but does not prevent the effect.
3. **The whole safety case rests on a grid-stress envelope nobody has signed off, watched by a frequency governor that
   trusted parties can bias.** The 50 MW/min fleet ramp, per-tick step and "0 reverse flow" defaults are assumptions (AS-01,
   Q13); at 100k hubs a 120-s fleet stop is ~250 MW/min, 5× that default (K5). The fleet-median frequency hold (G-07) that
   would catch an aggregate swing trusts hub-reported frequency, which a large compromised-hub cohort or vendor firmware
   (TH-038) can move or suppress.

Counts: **Critical 2 · High 8 · Medium 8 · Low 0** (§3). The R16 safe-stop design is in §4; residual risks the design must
accept in §5; go-live gates in §6.

---

## 2. Attack narratives

Each narrative names the attacker, the entry point, the path through named components and controls, what the design **would**
and **would not** stop, an impact estimate, and a detection assessment. MW/$/home figures use the brief's own units (11 kW
inverter, 39.2 kWh hub; 10,000-hub demo, 100,000-hub design) and are order-of-magnitude, labelled as estimates.

### N-01 — Insider: operator + approver collusion (ADV-03; AT-G; TH-068/070/110; RR-05)

**Entry.** Two legitimate console identities: a control-room `OP`/`APR` and an eligible second approver.
**Path.** The design correctly makes physics unapprovable: two colluding approvers cannot loosen G-01…G-14 (CTL-030–035),
so they cannot create a swing. The residual harm is **availability and money, not a grid event**. A single `OP` may engage a
*bank-scope* safe stop on one confirmation (Tier 1); at a 4CP peak that breaks one firm `DIST_DEFERRAL` or
`PARTNER_CAPACITY` interval per stop, attributable and reversible. Collusion widens this: a coordinated series of
bank-scope stops across many banks approaches a zone-scale loss without ever crossing a single Tier-2 threshold, and the
two colluders can seal a billing period (BAD+STL, SoD-07) or approve a dispatch-profile change (PPM + one domain approver)
in the same window.
**Stops.** Every action is attributed in the tamper-evident chain within one control cycle (CTL-073); the SOC is paged
(DET-041); physics holds. **Does not stop.** The *aggregate* of many individually-authorized bank stops is not itself a
tiered action; per-principal cumulation (DET-037) does not sum bank-stop *count* across principals; and two-person billing
seal is collusion-vulnerable by construction.
**Impact (est.).** Demo: loss of a few firm intervals, ≤ ~1–2 MW·intervals, plus a mis-sealed invoice period. Design scale:
tens of MW of firm delivery for one or more intervals; liquidated-damages and RTC+B buyback exposure.
**Detection.** High — every action is signed and paged; the weakness is response latency and that the actions look
legitimate. → RT-012, RT-015.

### N-02 — Compromised utility SCADA master or OpenADR VTN (ADV-01; AT-E E2; TH-050/125; RR-04)

**Entry.** A genuinely compromised, still-authenticated counterparty control centre (the 2015 Ukraine pattern applied to
*our* upstream), reaching `scada-gateway` over an authenticated association, or a compromised VTN reaching `integrations`.
**Path.** The counterparty issues **authentic, in-contract** calls. For a distribution operator (`CP-TDSP`/`CP-COOP`) this
is potent because one principal drives three things about the same bank: `AO5 BANK_LIMIT` (the dynamic "rating − margin" the
closed loop chases), the **bank measurement** the step-test cross-checks against, and `CROB5 UTILITY_OVERRIDE`. Lowering
`BANK_LIMIT` legitimately raises fleet output on that bank; a matching false bank measurement keeps the guardian's
step-test (CTL-051) satisfied because the same liar supplies both sides of the comparison. A compromised VTN can start
maximum in-contract events at correlated banks; a compromised master can oscillate setpoints or toggle overrides inside its
envelope (AB-16).
**Stops.** Global guardian limits still bound the physical effect (G-03 net loading ≤ 95%, G-04 ramp, G-05 sync step);
export beyond hosting capacity is clipped where an *independent* hosting figure exists; anomaly rules flag the pattern and
open out-of-band confirmation (DET-062, IRP-07). **Does not stop.** Where the counterparty *is* the only real-time source
for its own bank, its lie is authoritative for that bank until an independent source disagrees; and the service-agnostic
principle (correctly) forbids refusing its valid calls — the platform executes them.
**Impact (est.).** A masked service-transformer or single-bank overload (Impact 3–4): protection operation or a feeder/bank
event on the utility's own system; sustained firm-delivery distortion and battery wear across the affected banks.
**Detection.** Medium — the pattern (oscillation, override churn, step-test drift) is detectable, but the design's honest
response is "execute and confirm out of band," so detection does not prevent the first-order effect. → RT-003.

### N-03 — Compromised customer webhooks aggregating into a synchronized swing (ADV-06/09; AB-01/02; TH-006/111)

**Entry.** Several compromised customer credentials (`LARGE_LOAD`, `PIPELINE_AC`, plus market signals), each
individually valid.
**Path.** Each call is small and in-contract; they are timed to a common SCED :00 boundary and aimed at hubs behind
correlated banks or one load zone, so their *sum* is a synchronized ramp.
**Stops.** This is exactly what signed staggering (CTL-031, `nbf` jitter), the sync-step cap (G-05, ≤ ~1.7 MW per 2-s tick),
the fleet ramp cap (G-04, 50 MW/min), the energy ledger (G-09) and per-bank hosting (G-02/G-03) are for; the aggregate is
flattened to the envelope and clipped by priority with reasons (CTL-144). **Does not stop.** The envelope *is* the harm
floor, and two things weaken it: (a) cumulative admission is tracked **per principal** (DET-037), not per bank/zone/tick
**across principals**, so distinct compromised customers coordinate under the per-principal radar; (b) the envelope numbers
themselves are unvalidated assumptions (AS-01/Q13). The swing is bounded to "only" the envelope — which at design scale is a
50 MW/min movement no grid operator has yet blessed.
**Impact (est.).** Bounded to the fleet envelope: ≤ 50 MW/min aggregate, ≤ 10% of a bank's hubs per tick — significant if
the envelope is itself unsafe at scale.
**Detection.** Medium-High for the aggregate (DET-018/019/020/034), lower for the slow cross-principal build-up. → RT-004,
RT-012.

### N-04 — Compromised dispatcher or scada-gateway, post-R1 (ADV-01/02; AT-A P11; TH-001/018)

**Entry.** Code execution in `dispatcher` or `scada-gateway` (supply chain, dependency, or lateral movement).
**Path.** R1 is doing its job: the attacker cannot forge a command, because only the guardian signs. Three residual moves
remain. (a) **Signing-oracle within envelope** — drive the guardian to sign maximum-envelope batches every tick (TH-018);
bounded by G-01…G-14 and CAUTION on DET-016, so this yields at most the envelope, repeatedly. (b) **Withholding** — a
compromised or simply crashed `dispatcher` stops proposing, or proposes only holds; firm and awarded-AS obligations then
fall to fallback schedules and, past their TTL, to safe mode. This is a pure **availability/economic attack that R1 does not
address at all**: it breaches firm contracts (liquidated damages) and diverts AS awards (RTC+B buyback) without any forged
command. (c) **Narrative corruption** — `dispatcher` writes its own `ArbitrationDecision`/`DecisionTrace` via the outbox
(01 §8.1); a compromised dispatcher can record false rationale and false losers'-opportunity-cost while the guardian verdict
stays honest, so the *"why"* (a core-job deliverable) is attacker-authored even though the *command* is not.
**Stops.** Envelope and CAUTION bound (a); the guardian verdict and the linked chain bound (c) after the fact. **Does not
stop.** (b) withholding is not modelled as an attack; (c) the decision-trace author is the component under attack.
**Impact (est.).** Firm-delivery breach across the compromised partitions; corrupted decision narrative feeding operators,
auditors and the AI copilot.
**Detection.** Medium — DET-016 catches oracle abuse; there is no strong detector for "the leader is quietly under-serving"
beyond obligation `AT_RISK`, and no cross-check that the decision-trace rationale matches the guardian's independent view.
→ RT-008.

### N-05 — Guardian compromise or outage, and the redundant safe-stop path (R16/K11; TH-005/016; AT-B)

**Entry.** Guardian pod outage (crash, OOM eviction, dependency failure) or compromise (image, dependency, or its signing
material).
**Path — outage.** The guardian is the only signer and fails closed (P-6, CTL-029): no new commands exist. Hubs run the
last command to TTL, then a bounded fallback schedule for ≤ 15 min, then safe mode (no export). A stop **cannot be issued**
while the guardian is down — `03-security §6.8` and `07 §6.8` are explicit that a stop is merely "accepted as queued" and
takes effect only after the guardian recovers. Meanwhile `05 §2.10` (line 562) says "the safe-stop path uses its separate
key and publishes directly to EMQX," and `05 §4.2 C13` says the kill switch is "reachable through a cluster-admin CLI path
to guardian (still the only signer, R1)." **These three passages contradict each other**; R16 is genuinely undesigned.
**Path — compromise.** A compromised guardian is the ceiling — it holds the sole key, the anomaly thresholds and the kill
switch. No peer component can (a) stop the fleet independently of it or (b) invalidate its outstanding commands without its
cooperation (the key-set/epoch-bump authority is not specified as independent of the guardian).
**Stops.** Fail-closed protects against a swing during an *outage*; N-version and 2 replicas reduce single-pod risk.
**Does not stop.** A determined availability attack on the guardian (easier than forging) removes dispatch; a compromised
guardian is unbounded within its envelope; and there is no working, independent stop.
**Impact (est.).** Outage at peak: loss of ~110 MW (demo) / ~1,100 MW (design) of *controllable* capacity — the fleet
freezes then de-exports. Compromise: in-envelope harm plus loss of the safety supervisor.
**Detection.** High for outage (health checks, ALR); for compromise, the guardian is also the thing that would detect —
a conflict of interest. → RT-001, RT-002; the fix is §4.

### N-06 — Key compromise and fleet re-keying (AT-B; TH-016/017; IRP-04; RR-02)

**Entry.** Theft of an hourly command key (signer memory / SoftHSM token) or, worse, the dispatch intermediate.
**Path.** Hourly command keys are memory-only; an epoch bump re-keys ≥ 99% of online hubs in ≤ 15 min (FR-SEC-114), so a
stolen command key is bounded to ~1 h + the delivery-path requirement. The **intermediate** is the prize: on the demo node
it lives in SoftHSM2 with its PIN in a k8s Secret, and k3s secrets-encryption is defeated by node root (the design's own
AT-J J31/J33). Intermediate compromise enables durable forgery until an offline-root ceremony.
**Stops.** Environment-bound keys (a demo key cannot command production hubs, CTL-100/FR-SEC-116); short lifetimes;
epoch bump; HSM/KMS in production (RR-02 accepts the demo gap). **Does not stop.** On the demo node, node root reaches the
token and PIN; and IRP-04's epoch bump is itself published by… the guardian — if the guardian is the compromised element
(N-05), the re-key authority must be independent (ties to RT-002).
**Impact (est.).** Demo: bounded by "demo keys, simulated hubs, no real fleet" (RR-01/RR-02). Production: intermediate
compromise is severe until re-keyed.
**Detection.** Medium-High (DET-013/014/017). → RT-002, RT-010.

### N-07 — Replay and reordering across epochs (AT-D; TH-019/020/023/161; R8)

**Entry.** Network position on the MQTT/NATS path, or a leader that lost its lease in a partition.
**Path.** Well defended: per-hub monotonic `seq` persisted across reboot, `jti` replay cache, `nbf`/`exp` receipt-time TTL,
`aud`/`sub` binding, and **two** epoch checks (`key_epoch`, `leader_epoch`) at the guardian and again at `device-gateway`.
The residual seams: **leader-epoch fencing is "Proposed" (R8)** and both fencing points read the *same* NATS JetStream KV as
the epoch authority — a partition or stale KV read could momentarily present two "current" epochs; and DV-16's degraded mode
(a hub without authenticated time for > 24 h accepts seq-fresh commands under a 30-s receipt TTL) narrows but does not close
a window for a very-recently-captured command against a time-starved hub.
**Stops.** The layered checks reject stale/replayed/reordered commands (0 expected in TC-SEC-016/017). **Does not stop.**
A single shared epoch authority is itself a dependency; confirm R8 and make KV staleness fail-closed at the guardian.
**Impact (est.).** Low if R8 is confirmed; a brief window otherwise.
**Detection.** High (DET-013 on stale epoch). → RT-013.

### N-08 — False data injection to hide a bank overload (AT-C C4; TH-042/044; TH-114)

**Entry.** A compromised-hub cohort, or (stronger) the compromised utility source of N-02.
**Path.** The design's answer is three-source agreement — bank SCADA, fleet telemetry sum, and AMS meter data must concur
(CTL-050/051), so a lie must be told consistently to all three. Two weaknesses. (a) **AMS is not real-time** — 15-minute
data reconciled within 24 h — so in the moment there are effectively *two* real-time sources (bank SCADA and fleet sum). If
the utility source is the liar (N-02) and it also supplies `BANK_LIMIT`, the real-time cross-check collapses to fleet-sum
alone. (b) **Correlated hub silence** (a homeowner or jamming withholding telemetry at event times, TH-114) degrades the
fleet-sum source exactly when it is needed, weakening the very cross-check that would catch a false bank value.
**Stops.** Flatline detection (DET-028), neighbour-voltage correlation, step-test (DET-025), and hold-then-schedule on any
bad signal keep the loop from acting on a lie in the common case. **Does not stop.** A patient, consistent liar that
controls one real-time source and can also suppress or bias telemetry can hide a developing overload for a period.
**Impact (est.).** A masked service-transformer or bank overload (Impact 3–4).
**Detection.** Medium. → RT-003, RT-014.

### N-09 — Prompt injection into the ai-agent (AT-K; TH-134/135/138; RR-14)

**Entry.** Attacker-placed text the agent reads: a customer email at intake, a hub fault string, a SCADA alarm description,
a document, or tool output.
**Path.** Capability confinement is strong: propose-only tools, no dispatch/approve/config/SCADA/network tools, on-behalf-of
delegation, plain-text grounded output, DLP to the cloud, ≤ 500-char quoted/labelled untrusted content. The residual is the
**human at the intake seam**: `draft_call_from_text` produces a structured `Call` a person confirms; a plausible injected
`DIST_DEFERRAL`/`LARGE_LOAD` draft could be confirmed by a fatigued operator (RR-14). Injection can also bias arbitration
*proposals* systematically (AB-17) and probe for data via summaries (bounded by DLP, but pseudonymous hub-level series are
personal data under the GDPR benchmark and inference is not fully closed).
**Stops.** Every proposal re-enters gates 3–7; the guardian envelope is the backstop; nothing the agent proposes can exceed
what a human could already do through the validated path. **Does not stop.** A human confirming a well-crafted, in-envelope
draft — the injected call then executes as an ordinary, authorized call.
**Impact (est.).** Bounded to one in-contract, in-envelope call per accepted draft; no autonomous effect.
**Detection.** Medium (DET-065/066 canaries; monthly acceptance-skew review). → RT-011.

### N-10 — Supply chain: image, dependency, dispatch-profile tampering (AT-H/AT-L; TH-095/096/099/146/147)

**Entry.** A malicious dependency, a compromised CI/GitHub org, or a malicious dispatch-profile change.
**Path.** Controls are strong: digest-pinned images with cooldown, cosign + SLSA provenance verified at admission,
CODEOWNERS + two reviewers on the safety kernel, signed effective-dated profiles that the guardian can only *tighten*
(CTL-129–133). Residuals: the **judged demo build is SLSA L2** (provenance is *not* non-forgeable on the node path; L3 is a
production target); the profile bundle is cosign **keyless-OIDC via a GitHub org**, so a compromised org/CI (TH-096) is the
concentrated risk; a malicious profile that switches a closed-loop signal to a spoofable feed or flips failure behaviour to
"zero" is a Tier-2 "critical field," but the *only* thing standing between an approved-and-signed malicious profile and the
fleet is the guardian's global tighten-only envelope (CTL-132) and two human approvers.
**Stops.** A malicious *profile* cannot exceed global physics limits; an unsigned/mutated *image* is denied at admission
(DET-046). **Does not stop.** A backdoor in a pinned, long-trusted dependency (xz-style, TH-099 residual 8) inside the
guardian image is bounded only by egress control and the guardian's own independence — and the guardian is the one component
whose compromise is unbounded (N-05).
**Impact (est.).** Potentially severe if guardian code is reached; otherwise bounded by tighten-only envelopes.
**Detection.** Medium. → RT-016.

### N-11 — Lateral movement from co-located host services on the demo node (AT-J; TH-079/080; RR-01)

**Entry.** A vulnerability in Apache/PHP, Roundcube, the mail stack, MariaDB or `fdmp` on 192.168.5.35.
**Path.** Root on the shared kernel defeats every in-cluster control: it reads the k3s datastore and its encryption key,
the SoftHSM token and PIN, and any NodePort. The design accepts this as **RR-01 (High, demo only)** and relies on
*consequence* controls: only demo keys and trust anchors on the node (production hubs reject them, CTL-100/FR-SEC-116), no
real personal data, simulated hubs only, separate credentials from the prototype, and a short decommission window.
**Stops.** The consequence controls hold *as stated*: a node-root attacker on the demo cannot command a real hub or exfil
real personal data, because none exist there. **Does not stop.** The real ERCOT account (rotated, but present) is reachable;
and RR-01's acceptance is **fragile to scope creep** — it holds only until someone connects a pilot agent (Q21's "8883
LAN-only" is still *Proposed*) or loads real CEII topology to make the demo realistic, at which point the accepted residual
silently becomes an unaccepted one.
**Impact (est.).** Demo: loss of the demo cluster, the ERCOT key, and the local audit journal (see N-12). Not the real
fleet — *provided* the scope discipline holds.
**Detection.** Medium (DET-047/049, host alerts out of scope). → RT-007, RT-010, RT-017.

### N-12 — Billing fraud and audit-chain rewriting, and the audit-write window (TH-149/153/154; K3; C15)

**Entry.** A privileged DB user, a colluding BAD+STL pair, or a node-root attacker (N-11) during an audit-store outage.
**Path.** Post-hoc tampering is well caught: append-only INSERT-only tables, per-record producer signatures, a hash chain,
off-node anchors every 5 min with RFC 3161 tokens, counterparty-verifiable settlement roots, and hourly + nightly
verification with deterministic replay (CTL-073/137–143; TC-SEC-107/108 catch even a superuser who re-hashes forward). The
gap is the **window**: K3 is explicitly unresolved, and the failure-modes doc resolves it operationally as C15 —
**dispatch keeps running on a local decision journal** on a node volume when the central store is down, catching up later.
Between a decision's local-journal write and its off-node anchor (≤ 5 min + catch-up), the chain-of-record for
already-executed commands lives on a **mutable local volume that node root (RR-01) can alter before it is anchored**. Add
collusion on the billing seal (BAD+STL, SoD-07, RR-05) and provisional "estimated" M&V lines (05 §2.10 line 563) and there
is a path to distort records in the pre-anchor window.
**Stops.** Anything already anchored is tamper-evident; recomputation catches inconsistent billing (DET-074).
**Does not stop.** The pre-anchor local-journal window during an audit outage, on a node an attacker may hold root on.
**Impact (est.).** Corrupted decision/billing records for the affected window; loss of the SO-5 guarantee exactly when a
node-root attacker is present.
**Detection.** Medium — detected at the next anchor/verification, not within the window. → RT-007, RT-015.

### N-13 — DoS and reconnect storms, and the availability inverse of R1 (AT-F; TH-031/032/086/159)

**Entry.** A flood of the MQTT/API edge, a self-inflicted reconnect storm, or a resource spike (malicious or a co-tenant
like ClamAV scanning a large message).
**Path.** Volumetric and storm handling is strong: full-jitter reconnect, staged admission, broker limits, bulkheads,
priority classes, and — decisively — **firm delivery continues from pre-signed fallback schedules** (CTL-038) so a denial
of *control* is not a denial of *firm delivery* for the schedule's duration. The residual is structural: **guardian/EMQX
eviction under node memory pressure**. cgroup PriorityClass orders pods only *within* `kubepods`; it does not protect them
against **host** processes (mail/MariaDB/`fdmp`), and the architecture admits this (01 §14.2 note). A host-side memory spike
can OOM the node and evict even Guaranteed-QoS control pods; because the guardian fails closed, that becomes a fleet-wide
loss of new commands (the availability inverse of R1's integrity win).
**Stops.** Fallback schedules cover the window; jitter/admission prevent self-inflicted storms. **Does not stop.** A host
co-tenant (not under Orchestrator control, "never touch") driving the node into OOM; and a guardian outage that outlasts the
fallback-schedule horizon degrades to safe mode (no export) fleet-wide.
**Impact (est.).** Loss of active closed-loop control and, past the fallback horizon, loss of export — at peak, the removal
of the fleet's grid services.
**Detection.** High (health/ALR). → RT-006, RT-001/RT-002 (the guardian availability dependency).

### N-14 — Fleet-median frequency/voltage governor bias or suppression (TH-045; TH-038; G-07/G-08)

**Entry.** A large compromised-hub cohort, or a hub-vendor cloud/firmware pushing a common-mode misreport (TH-038 — the
Deye-style independent path).
**Path.** The frequency-aware hold (G-07) computes a fleet median from ≥ 100 hubs across ≥ 3 zones and cross-checks ERCOT
public data *"when available."* The median-of-many defence assumes an **honest, independent majority**. A vendor firmware
line or a compromised cohort is a *common-mode* source that violates that independence and can shift the median across a
threshold — worse, it can be used to **suppress** a hold (report normal during a real excursion), so the fleet keeps
charging/discharging into a developing frequency event, the opposite of the intended safety action.
**Stops.** A *minority* cannot move the median (the design's stated property); the ERCOT cross-check catches gross bias when
present. **Does not stop.** A large common-mode source; and the cross-check is conditional ("when available").
**Impact (est.).** Contribution to, or failure to arrest, a frequency excursion — the BlackIoT class of harm the threat
model itself cites (§1.4).
**Detection.** Medium (DET-024, and divergence from the ERCOT feed when present). → RT-005.

### N-15 — Demo SCADA TLS-only exception and pilot scope creep (Q11; RR-15; TH-121/122)

**Entry.** The register's default TLS-only exception for DNP3-SA / ICCP IEC 62351-4 on the demo, combined with pressure to
connect a real pilot counterparty to show the system "usable tomorrow."
**Path.** On the demo, all SCADA counterparties are `grid-sim` inside the cluster, so spoof/replay of SCADA controls is
bounded (RR-15). The risk is a **governance transition**: the moment a real utility association is connected without a
licensed Secure Authentication library, the design's own defences against spoofed/replayed SCADA controls (which depend on
SA, not just TLS) are absent, and TLS-only leaves application-layer control authentication to a compensating VPN/allow-list.
**Stops.** For the *simulated* demo, TLS + SBO + allow-lists + `guardian` still bound the effect. **Does not stop.** A real
association under the exception would lack the application-layer authentication the rest of the design assumes.
**Impact (est.).** None at demo (simulated); High if the exception is carried into a real association.
**Detection.** N/A (design gate). → RT-009.

---

## 3. Findings

Severity: Critical / High / Medium / Low. "Location" cites the document, section and existing IDs. "Fix" maps to existing
`CTL-*`/`FR-SEC-*` where a strengthening suffices, or proposes a new ID (`CTL-147…151`, `FR-SEC-204…207`).

| ID | Sev | Location | Gap | Exploit sketch (conceptual) | Fix |
|---|---|---|---|---|---|
| **RT-001** | **Critical** | Register R16/K11; `03-security §6.5, §6.8`; `07 §6.8`; `05 §2.10` (line 562), `§4.2 C13` (line 1420) | **No working stop when the guardian is down, and the docs contradict each other** on whether a separate safe-stop key exists. A stop "waits on" the component whose failure is the emergency. | During a guardian outage, a scoped stop is only "queued" and takes effect after recovery; an operator facing a developing event cannot stop the fleet on demand. | Implement the independent **Safe-Stop Authority** of §4 (new **CTL-147**, **FR-SEC-204**); reconcile `05`, `03-security` and `07` to one design; keep the guardian sole signer of *run* commands (R1) while giving *stop* an independent, cryptographically stop-only path. |
| **RT-002** | **Critical** | `03-security §6.1`; `01 §5.5`; `03 §8.14` | **Guardian concentrates both integrity and availability.** Sole signer + sole holder of anomaly thresholds + fail-closed + kill switch; no peer can stop it or invalidate its outstanding commands without its cooperation. Outage = denial of dispatch (~110/1,100 MW controllable); compromise = unbounded within envelope. | Attack guardian *availability* (eviction/crash/dependency) — easier than forging — to remove dispatch at peak; or compromise the guardian image/deps for in-envelope harm with no peer to contain it. | CTL-147 (SSA can stop independently); new **FR-SEC-205**: a **key-epoch-bump / dispatch-key-revocation authority independent of the guardian** so a rogue guardian's commands can be invalidated; keep N-version (CTL-086) and ≥ 2 replicas; host-level availability per RT-006. |
| **RT-003** | High | `07 §3.1.8` (AO5 `BANK_LIMIT`), `§6.2`; `03-security §6.3` (G-03, G-12), `§10.2`; TH-125/RR-04 | A distribution counterparty supplies the **bank measurement, the dynamic bank limit and the override** for the same bank, so the guardian's step-test and envelope for that bank are influenced by the same principal — it can mask or induce a real overload, or drive max legitimate output, inside the envelope. | A compromised authenticated master lowers `BANK_LIMIT` (raising output) and supplies a matching false bank reading so the step-test agrees; service-agnostic dispatch executes its valid calls. | New **CTL-150**: a utility-supplied dynamic limit may **increase** fleet output only when an *independent* real-time measurement of that bank corroborates it; bound per-interval `BANK_LIMIT` deltas; when the counterparty is the sole real-time source and it disagrees with the fleet sum, hold-then-schedule (extend CTL-051, G-12). Keep RR-04 explicit. |
| **RT-004** | High | AS-01; register Q13, K5; `03-security §6.3` (G-04/G-05) | **The grid-stress envelope is an unvalidated assumption** — 50 MW/min fleet, ~1.7 MW/tick, 0 reverse flow — yet it is the harm floor for every aggregate/insider/counterparty path. At 100k hubs a 120-s fleet stop is ~250 MW/min (K5), 5× the ramp default. | Any path bounded "to the envelope" is only as safe as the envelope; if it is set too high for the interconnection, the bounded outcome is itself a grid event. | Gate go-live on ERCOT-/utility-signed envelope values (new **FR-SEC-206**); make the stop ramp **scale with MW** so a fleet stop is not a super-ramp (resolve K5); characterize fleet-trip behaviour > 100 MW with ERCOT (AS-01). |
| **RT-005** | High | `03-security §6.3` (G-07/G-08); TH-045; TH-038 | The **fleet-median frequency/voltage governor trusts hub-reported values**; a large compromised cohort or vendor firmware (common-mode) can bias or **suppress** a hold; ERCOT cross-check is conditional ("when available"). Suppression lets the fleet drive into an excursion. | Common-mode firmware misreports frequency so the median stays "normal" during a real excursion; the safety hold never fires. | New **CTL-151**: make an **independent, authenticated grid-frequency/voltage reference** (utility/ERCOT feed, or hardware references at trusted sites) the *authority* for holds, with the hub median as corroboration only; alarm on median-vs-reference divergence (extend CTL-034, DET-024). |
| **RT-006** | High | `01 §14.2` (note, lines ~1406–1411); TH-086; RR-01 | **Host co-tenants can starve the control plane.** cgroup PriorityClass orders pods only within `kubepods`, not against host processes (mail/MariaDB/`fdmp`); a host memory spike can OOM the node and evict Guaranteed-QoS guardian/EMQX → fail-closed → fleet-wide loss of new commands. | A co-tenant load spike (even non-malicious ClamAV/`fdmp`) during an event drives node OOM; the guardian is evicted; the fleet runs open-loop on stale schedules, then de-exports. | New **CTL-148**: reserve a host-level cgroup/memory floor for `kubepods` (or move the control plane off the shared host); monitor **host-level** (not just cgroup) pressure (extend CTL-080); production must not co-tenant the control plane. Accept as demo residual with the migration date. |
| **RT-007** | High | Register K3; `05 §4.2 C15` (line 1430), `§2.10`; `03-security §12.3/§12.6` | **Audit-write window.** C15 keeps dispatching on a **local decision journal** on a node volume when the central store is down; that journal is mutable by node root (RR-01/AT-J) until the next off-node anchor (≤ 5 min + catch-up). Commands execute while their record-of-truth is local and alterable. K3 is explicitly unresolved. | A node-root attacker (N-11) alters local-journal entries for already-executed commands before they are anchored; SO-5 accountability is lost for that window. | New **CTL-149 / FR-SEC-207**: the local journal must be **producer-signed and hash-chained on write** and anchored within a tight bound (seconds, not 5 min) even in degraded mode; on local-journal integrity failure go CONSERVATIVE; resolve K3 explicitly (firm delivery *and* signed local journal, not either/or). |
| **RT-008** | High | `01 §5.4, §8.1`; `03 §8.14`; TH-001 | **R1 protects command integrity, not firm-delivery availability, and not the decision narrative.** A compromised/withholding `dispatcher` can under-serve firm/AS obligations (liquidated damages, RTC+B buyback) with no forged command, and it authors its own `DecisionTrace` rationale/opportunity-cost. | A compromised leader proposes only holds and writes plausible-but-false "why"; obligations quietly breach; the trace exonerates it. | Add a **delivered-vs-committed withholding detector** at `contracts`/`guardian` that alarms on unexplained shortfall independent of the dispatcher's own trace (extend DET-018, obligation `AT_RISK`); have the **guardian co-sign the arbitration inputs hash** so a fabricated trace is detectable against its independent verdict (extend CTL-137/CTL-047). |
| **RT-009** | High (gate) | Register Q11; `07 §8.2`; `03-security §8.2`; RR-15 | **Demo SCADA TLS-only** (no DNP3-SA / ICCP IEC 62351-4) is bounded only because counterparties are simulated; a real association under the exception would lack the application-layer control authentication the design assumes. | A pilot utility is connected under the exception; spoofed/replayed SCADA controls that SA would reject are bounded only by a VPN/allow-list. | Hard gate (already RR-15/SC-10): **no real SCADA association carries controls without a licensed SA library**; keep the exception limited to `grid-sim`; enforce in onboarding (extend CTL-110, SC-10). |
| **RT-010** | High (demo) | `03-security §7.6, §15`; TH-016/017; RR-02; AT-J J33 | **Demo key custody**: dispatch intermediate in SoftHSM2 with its PIN in a k8s Secret; node root reads both (secrets-encryption defeated by root). Intermediate compromise → durable forgery until an offline ceremony. | Node-root on the demo (N-11) reads the token+PIN; on the demo this is bounded by "demo keys, simulated hubs" (RR-01/RR-02); in production it would be severe. | Accept RR-02 for the demo; production HSM/KMS (CTL-045); ensure the **epoch-bump/revocation authority is independent of the guardian** (RT-002/FR-SEC-205) so a stolen key can be invalidated even if the guardian is the compromised element. |
| **RT-011** | Medium | `03-security §13.2, §13.4`; AT-K; TH-134/138; RR-14 | **AI intake human seam.** `draft_call_from_text` → human confirm; a fatigued operator may confirm a plausible injected draft; pseudonymous hub-level series (personal data under the GDPR benchmark) flow to the summarizer with DLP but residual inference. | Injected customer text yields a plausible in-contract draft; the operator confirms; it executes as an authorized call (bounded by the envelope). | Flag every intake-drafted call as **AI-drafted** through to the confirmation preview and audit; always confirm against contract with the guardian dry-run impact preview (extend CTL-121/CTL-146); keep guardian envelopes as the backstop. Residual RR-14. |
| **RT-012** | Medium | `03-security §5.8, §6.3`; DET-037; AB-01/AB-02; TH-110/111 | **Cumulation is per-principal, not per-bank/zone/tick across principals.** Distinct compromised customers (or an insider using several) coordinate under the per-principal thresholds. | Several principals each stay below Tier-1 and below anomaly baselines while their calls sum behind one bank/zone at one boundary. | Add **per-bank and per-zone cumulative admission windows** across principals in the guardian (extend CTL-032/CTL-039/G-02/G-03/G-10 to include a cross-principal synchronization/rate check), not only per-principal (DET-037). |
| **RT-013** | Medium | `01 §10`; register R8; TH-132/161 | **Leader-epoch fencing is "Proposed" and relies on a single shared KV** as the epoch authority read by two checkers; a partition/stale read could momentarily present two "current" epochs. | A leader that lost its lease in a partition still reaches the guardian, and a stale KV read fails to fence it before device-gateway's second check. | Confirm R8; make **KV staleness fail-closed** at the guardian; keep the device-gateway second check (already CTL-145). |
| **RT-014** | Medium | `03-security §10.2`; TH-114/042 | **Cross-checks assume telemetry availability.** Correlated hub silence (homeowner/jamming at event times) degrades the fleet-sum source exactly when it is needed to catch a false bank value (RT-003). | An attacker (or gaming homeowners) suppress telemetry during events, weakening the three-source physics check. | Treat **correlated silence as a degraded measurement source** and prefer hold-then-schedule; feed silence-correlation into trust scoring (extend CTL-050/CTL-052, DET-029). |
| **RT-015** | Medium | `03-security §12.10`; `05 §2.10` (line 563); TH-149/154; RR-05 | **Billing collusion + provisional lines.** BAD+STL two-person seal is collusion-vulnerable; "estimated" M&V lines allow value before reconciliation; daily recompute/anchor catch post-hoc, not intra-window. | Colluding BAD+STL seal a period with inflated estimated lines before reconciliation. | Require **reconciliation before an estimated line can be sealed**; anchor provisional lines too; keep RR-05 explicit; consider a third-party (auditor) attestation on seals above a threshold (extend CTL-142). |
| **RT-016** | Medium | `06 §7`; `03-security §14`; AT-H/AT-L; TH-096/099 | **Demo build is SLSA L2** (provenance forgeable on the node path) and profile bundles are cosign keyless-OIDC via a GitHub org — a compromised org/CI is the concentrated risk; a backdoored pinned dep in the guardian image is bounded only by egress + guardian independence (which RT-002 shows is itself concentrated). | Compromise the CI/org to sign a malicious guardian image or a "critical-field" profile that survives review. | Keep **guardian/OPA/profile changes behind CODEOWNERS + two reviewers** (CTL-083); pursue **SLSA L3 for the demo guardian image** specifically; egress-deny for the guardian (CTL-082); accept L2 for non-safety demo images. |
| **RT-017** | Medium (governance) | RR-01; register Q21; `03-security §19.5` | **RR-01's acceptance is fragile to scope creep**: it holds only while the node has no real hub, no real personal data, and demo anchors only; connecting a pilot agent (Q21 "8883 LAN-only" is *Proposed*) or loading real CEII topology silently voids the accepted residual. The real ERCOT account already lives there. | Someone connects a pilot agent or loads real topology "just for the demo"; the accepted-residual co-location risk now covers real assets/data. | Enforce a **hard, monitored gate** that the node never gains a real device credential, real personal-data class, or real CEII topology while co-located (extend CTL-100/CTL-079); make Q21 a decision, not a default. |
| **RT-018** | Medium | `03-security §6.5` (out-of-band path); `05 §4.2 C13` | **Both the alert path and the out-of-band stop path terminate on operator/SOC networks and (today) on the guardian.** A combined compromise could blind *and* freeze. | Compromise the SOC workstation network and the guardian together: no stop, no clear alerting. | Fold into R16: the **SSA out-of-band path is independent of the guardian** and reachable on a distinct trust path with a hardware token (§4); dead-man's-switch alerting on an independent channel (CTL-056, ALR-290). |

Counts: **Critical 2 · High 8 · Medium 8 · Low 0 = 18.**

---

## 4. R16 design proposal — an independent, minimal safe-stop path

**Requirement (register R16, K11).** A stop must never wait on the component that signs dispatch. The naive hint in
`05 §2.10` ("a separate key publishes directly to EMQX") is right in spirit but, as written, would reintroduce exactly the
second-signer risk R1 removed — a second key that can put messages on hub command topics. The design below keeps R1 intact
by making the second path **cryptographically able to do only one thing: stop.**

### 4.1 The Safe-Stop Authority (SSA)

- **A separate, minimal service** `safe-stop` in `og-safety`, deployed independently of the guardian (own pod; own node/zone
  in production; ≥ 2 replicas). It has **no dependency on `dispatcher`, `contracts`, `api` or the guardian to function.**
- **A distinct key hierarchy** — a `safe-stop` root/intermediate separate from the dispatch, device, service and audit
  roots (CTL-042/CTL-093) — issuing keys with a dedicated extended key usage `safe-stop-only`.
- **The only message it can produce is a scoped `SAFE_STOP`/`CEASE`** (bank / zone / fleet) with setpoint 0 and the R4 ramp
  (30/60/120 s). It can never emit a run/setpoint/mode/schedule command.
- **Hub firmware accepts a validly signed `SAFE_STOP` from either the guardian's dispatch key or the SSA's safe-stop key**,
  but a new device rule (**DV-17**) restricts a token bearing the `safe-stop-only` EKU to `cmd ∈ {SAFE_STOP, CEASE}`,
  setpoint 0, ramp ≤ the R4 limit. So even if the SSA key is stolen, the worst possible outcome is a **fleet stop** (the safe
  direction) — an availability incident, ramped, with home load and reserve untouched — never a swing.
- **Stop wins by precedence** (already rank 3, `07 §6.2`; `03 §8.15(c)` `class` ordering): a `SAFE_STOP` supersedes any run
  command and cannot be undone by a lower-class command. **Release stays with the guardian** (two-person Tier 2, R3/R4): the
  SSA can only stop, so a stolen SSA key cannot cause a rebound (the release is the dangerous direction and stays on the
  guarded path).
- **Triggers into the SSA:** (a) the guardian forwards a stop in normal operation (SSA is transparent then); (b) an
  **out-of-band operator/SOC trigger** over a distinct mTLS path with a hardware token — this is the existing CTL-037
  out-of-band endpoint, **re-pointed at the SSA instead of the guardian**, so it works with `api`, `console`, `dispatcher`
  *and the guardian* down; (c) an optional conservative **watchdog**: if the SSA loses the guardian heartbeat *and* its own
  independent fleet-state replica shows the fleet still exporting past the fallback horizon, it may stop the affected scope —
  bounded, stop-only, and logged. The watchdog is optional because hub fallback→safe-mode already covers a pure outage; its
  value is against a *compromised* guardian that keeps signing.
- **Containing a rogue guardian:** the SSA stop must be paired with an **independent dispatch-key epoch-bump / revocation
  authority** (RT-002/FR-SEC-205) held by key custody (`SEC`+`SRE`), so that after stopping, a compromised guardian's
  outstanding dispatch commands are invalidated (`key_epoch` advance) without the guardian's cooperation. Stop + invalidate =
  contain.

### 4.2 New requirements and controls

- **CTL-147** — Independent Safe-Stop Authority with a stop-only key and EKU; hubs accept safe-stop from it under DV-17.
- **FR-SEC-204** — The kill switch reaches hubs within one control cycle **with the guardian pod stopped and both guardian
  replicas isolated** (this is exactly TC-SEC-032 variant B, today "blocked pending R16" — the SSA is what unblocks it).
- **FR-SEC-205** — A dispatch-key epoch-bump/revocation authority independent of the guardian.
- **DV-17** — Device-side restriction of the `safe-stop-only` EKU to stop/cease, setpoint 0, ramp ≤ R4.
- **SC-20** — Hardware condition: real hub firmware pins the safe-stop root and enforces DV-17 (folds into Q2). Where
  firmware cannot distinguish EKUs, degrade to: the SSA key may sign only on `SAFE_STOP` scope-group topics, and firmware
  treats those topics as stop-only.

### 4.3 Threat analysis of the SSA

| Threat to the SSA | Effect | Why it is acceptable / mitigation |
|---|---|---|
| SSA signing key theft | Fleet-wide **stop** only (ramped; home load and reserve intact; reversible only via the guarded two-person release) | Converts an integrity risk into a bounded, loud, self-limiting **availability** risk — strictly better than "no stop possible." Detect via DET on any safe-stop not paired with a guardian-forward or an authorized out-of-band trigger. |
| SSA abused to stop the fleet (denial by stop) | Availability/economic harm | Same auth/authz as kill-switch engage (hardware token, RBAC, rate-limit); release is fast-but-safe; every trigger audited and paged. This is a deliberate, accepted trade: a rogue *stop* is far less dangerous than a rogue *run*. |
| SSA outage | No independent stop | ≥ 2 replicas; separate node/zone in production; independent of the guardian's failure modes (crash, OOM-within-kubepods, dependency). On the shared demo node it shares the kernel (RR-01) but not the guardian's software failure domain. |
| Split brain (SSA + guardian both signing) | None harmful | The SSA only stops; a stop always wins precedence, so there is no conflicting-command hazard. |
| Watchdog false trigger | Unnecessary stop | Conservative thresholds; stop-only; audited; prefer human trigger. The watchdog is optional and off by default until validated. |
| Firmware cannot enforce DV-17 | SSA key could carry more | Degrade to topic-scoped stop-only (SC-20); until hardware confirms Q2, treat the SSA key with the same custody as the dispatch intermediate. |

**Net effect.** R1 is preserved (the guardian remains the sole signer of anything that *moves* MW); the availability and
compromise concentration of RT-001/RT-002 is broken (an independent path can always stop, and an independent authority can
invalidate a rogue guardian); and the new key, being cryptographically stop-only, adds an availability risk, not an
integrity one.

---

## 5. Residual risks the design must accept explicitly

These cannot be engineered away within the stated constraints and must be *owned*, not hidden:

1. **RR-01 co-location (demo, High).** A node-root neighbour controls the demo Orchestrator. Acceptable only while the node
   carries demo keys/anchors, simulated hubs and no real personal data — and only if RT-017's scope gate is enforced.
2. **RR-04 authenticated-but-compromised counterparty (Medium).** Service-agnostic dispatch means the platform executes a
   compromised counterparty's authentic, in-contract calls; the guardian bounds the physical effect and out-of-band
   confirmation flags the pattern, but the first-order effect is not prevented (N-02, RT-003).
3. **Unvalidated grid-stress envelope (High until signed).** The safety case reduces to "the admitted envelope is below
   harm," and that envelope is an assumption (AS-01/Q13, RT-004). Until ERCOT-facing staff and each utility sign it, the
   fleet's guaranteed-safe ceiling is unproven.
4. **Guardian as concentrated trust (reduced, not eliminated).** Even with the SSA and an independent epoch-bump authority,
   the guardian is still the sole *integrity* authority for run commands; N-version, CODEOWNERS and property tests reduce but
   do not remove the risk that a single correct implementation is wrong or subverted (RT-002, RT-016).
5. **RR-15 demo SCADA TLS-only (Low demo / High if carried to real).** Bounded to simulated counterparties; a hard gate must
   prevent it reaching a real association (RT-009).
6. **RR-03 hub firmware / vendor-cloud trust (Medium).** Signed commands, DV rules and behaviour detection assume hub
   hardware capabilities that are unconfirmed (Q2), and an independent vendor path (TH-038) can only be detected, not
   prevented (N-14, RT-005).
7. **RR-02 software key custody on the demo node (Medium).** No HSM on the node; accepted for the demo, HSM/KMS required for
   production (RT-010).
8. **K3 audit-write window (Medium).** Even with CTL-149, a bounded window exists where a just-executed command's record is
   local before anchoring; the design must state the maximum window and its acceptance (RT-007).
9. **RR-05 two-person collusion (Low).** Physics cannot be approved away, but billing seal and profile approval can be
   colluded; conduct review and non-reporting-line approver pools are the only mitigations (N-01, RT-015).
10. **RR-14 AI persuasion of a human (Low).** A grounded but wrong rationale or a plausible injected draft accepted by a
    tired operator; the guardian envelope is the backstop (N-09, RT-011).

---

## 6. What would satisfy me before go-live

Ordered; each is a concrete gate a reviewer can check.

1. **R16 closed.** The independent Safe-Stop Authority of §4 (CTL-147, FR-SEC-204, DV-17) implemented; TC-SEC-032 **variant
   B** passes (stop reaches hubs within one control cycle with the guardian and both replicas isolated); `05`, `03-security`
   and `07` reconciled to one safe-stop design; an independent dispatch-key epoch-bump authority (FR-SEC-205) demonstrated
   invalidating a rogue guardian's commands.
2. **Envelope signed.** The grid-stress defaults (AS-01/Q13) signed off by ERCOT-facing staff and each partner utility; the
   fleet-stop ramp made to scale with MW (K5); fleet-trip behaviour > 100 MW characterized with ERCOT (RT-004, FR-SEC-206).
3. **Independent grid reference for holds.** The frequency/voltage holds authoritative on an independent, authenticated
   reference with the hub median as corroboration only (CTL-151, RT-005).
4. **Counterparty measurement independence.** A utility-supplied dynamic bank limit may increase output only with an
   independent corroborating measurement; sole-source disagreement forces hold-then-schedule (CTL-150, RT-003).
5. **Control-plane availability on a real footing.** The control plane off the shared host, or a host-level cgroup/memory
   reservation for `kubepods` with host-pressure monitoring proven (CTL-148, RT-006); and the guardian-availability
   dependency addressed by the SSA (RT-002).
6. **Audit window closed.** The local decision journal producer-signed, hash-chained on write and anchored within a stated
   tight bound; local-journal integrity failure forces CONSERVATIVE; K3 resolved as *both* firm delivery *and* signed audit
   (CTL-149/FR-SEC-207, RT-007). TC-SEC-107/108 extended to inject tampering **inside** the pre-anchor window.
7. **Withholding and narrative integrity.** A delivered-vs-committed withholding detector independent of the dispatcher's own
   trace, and guardian co-signature of the arbitration inputs hash (RT-008).
8. **Cross-principal aggregation.** Per-bank/zone cumulative admission across principals in the guardian (RT-012).
9. **Scope-creep gate on the demo node.** Enforced, monitored proof that the node never gains a real device credential, real
   personal-data class, or real CEII topology while co-located; Q21 decided (RT-017).
10. **Real-SCADA gate.** No real SCADA association carries controls without a licensed Secure Authentication library (Q11 →
    decision, RR-15/SC-10, RT-009); real QSE/ADER connection preceded by SC-05…SC-12 recorded **Met**.
11. **Hub hardware confirmed (Q2).** SC-01/SC-02/SC-20: device-local reserve floor, ES256/EdDSA verification, persisted
    sequence/epoch, safe-stop-root pinning and DV-17 proven on real firmware before any real hub connects.
12. **Red team executed, not just planned.** ST-08 / TC-SEC-136…148 (RT-A…RT-L) run once in simulation before the judged
    demo, with the R16 variant included, High/Critical findings fixed and re-tested, and the calendar/owner gap (K10) closed
    so the exercise actually has a slot.

Go-live for the **judged demo** is acceptable with items 1, 6, 9 and 12 met and the rest recorded as owned residuals;
go-live for **any real hub, counterparty, market or personal data** requires all twelve.

---

## 7. Cross-references

| Topic | Document |
|---|---|
| Binding decisions, R16/K3/K11, Q-defaults | `../00-decision-register.md` |
| Threats, attack trees, residual risks, SC conditions | `../03-security/01-threat-model.md` |
| Controls, `FR-SEC-*`, detections, playbooks, guardian design | `../03-security/02-security-architecture.md` |
| Guardian as sole signer, leader/epoch model, outbox/audit | `../02-architecture/01-system-architecture.md` |
| Command lifecycle, device verification, local autonomy | `../02-architecture/02-domain-model-and-interfaces.md` |
| Kill switch, guardian interaction, degraded modes | `../02-architecture/03-decision-engine.md` |
| Safe state per component (line 562), C15 audit window | `../02-architecture/05-failure-modes-and-recovery.md` |
| Single-node hardening, CI/CD, secrets, profile governance | `../02-architecture/06-platform-and-operations.md` |
| SCADA control authority, kill-switch mapping, guardian-down behaviour | `../02-architecture/07-scada-integration.md` |
| Security test cases incl. TC-SEC-032 (R16 probe) and RT-A…RT-L | `../05-testing/03-test-cases-nonfunctional.md` §6 |
