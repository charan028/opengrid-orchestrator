# OpenGrid Orchestrator — MVP-S Test Plan (Saturday 2026-09-26, 18:00)

Invariants: see 00-invariants.md (canonical). Status: draft for owner approval (gate G1).

Status: draft for G1 approval · Written: Friday 2026-09-25 · Author: test architect for this increment · Companions:
[`01-saturday-delivery-plan.md`](01-saturday-delivery-plan.md) (acceptance A1–A11, modules, schedule, gates G1–G5, cut
lines), [`../06-reviews/06-first-principles-review.md`](../06-reviews/06-first-principles-review.md) (P1–P8, K1–K13,
commitment lock §3, canonical LP §5, decisions §8a), [`02a-mvp-s-spec-engine.md`](02a-mvp-s-spec-engine.md),
[`02b-mvp-s-spec-platform.md`](02b-mvp-s-spec-platform.md), [`03-mvp-s-epics-stories.md`](03-mvp-s-epics-stories.md)
(ES01–ES10), and the full-engine test set [`../05-testing/01-test-strategy.md`](../05-testing/01-test-strategy.md),
[`02-test-cases-functional.md`](../05-testing/02-test-cases-functional.md),
[`03-test-cases-nonfunctional.md`](../05-testing/03-test-cases-nonfunctional.md), whose levels, harness concepts
(`agent-sim`/`grid-sim`), fixtures (`FX-*`) and gates this plan reuses at MVP-S scope. `04-traceability-matrix.md`
there is the full-engine matrix; §6 below is the MVP-S-only matrix.

This is the test plan for the **first working increment** of the Orchestrator, not a reduced demo (review §8a #5). It
supersedes nothing in `05-testing/*`; it is the subset that a 30-hour build can execute, chosen so that every
zero-tolerance invariant (K1–K13) and every Saturday acceptance item (A1–A11) is proven, not asserted.

**Conventions.** Test IDs `TS-<epic nn>-<nn>`, e.g. `TS-05-03`, unique within this document; numbering per epic is
contiguous across §2 (property) and §3 (functional) so no ID repeats. Level tags: **U** unit, **P** property-based
(Hypothesis), **I** integration, **E** end-to-end on the server against the sim harness, **N** performance, **C**
chaos/resilience, **S** security. Tooling: `pytest`, `hypothesis`, `pytest-asyncio`, a custom Locust-style load driver
(§4), a scripted chaos runner (`systemctl kill/stop`, process `kill -9`, feed stubs — no `iptables`, per the delivery
plan's native-systemd deployment). Epics (from `03-mvp-s-epics-stories.md`): **ES01** Platform & data · **ES02** Live
feeds & forecast · **ES03** Fleet twin & test harness · **ES04** Contracts, opportunities & commitments · **ES05**
Dispatch · **ES06** Guardian & safe stop · **ES07** Health & degraded modes · **ES08** Settlement · **ES09** Audit
trace & retention · **ES10** Operator UI.

---

## 1. Strategy in one page

### 1.1 Levels

| Level | Proves | Runs where | Owner |
|---|---|---|---|
| **U** Unit | One function/algorithm in isolation (control law, LP row-builder, hash-chain link, performance-factor formula) | developer venv, every commit | module author |
| **P** Property (Hypothesis) | An invariant (K1–K13) holds for ≥ 10³ generated cases per run (10⁴ nightly), shrinking to a minimal counterexample | developer venv + CI, every commit touching `ledger`/`selector`/`allocator`/`guardian`/`trace` | module author + reviewer |
| **I** Integration | Two or more real modules over real Postgres/MQTT (test containers or the server `og-test` DB) | developer venv (docker-less: local Postgres/Mosquitto) or server `og-test` | module pair owners |
| **E** End-to-end | The deployed 7-process stack on the server, against `agent-sim`/`grid-sim` (spec §12.3), driving A1–A11 | server `og-test`, then server live for the G5 sign-off run | WS8 QA |
| **N** Performance | Latency, throughput, memory at 2k/10k hubs | server `og-test`, isolated window | WS8 QA |
| **C** Chaos | Process kill, Postgres/MQTT restart, stale feed, comms loss | server `og-test` | WS8 QA |
| **S** Security | Guardian refusal, unsigned/replayed/expired command rejection | developer venv + server `og-test` | WS5 |

### 1.2 Environments

| Environment | What it is | Used for |
|---|---|---|
| **Developer venv** | Python 3.13 venv, local Postgres 17 + Mosquitto (or test containers), no server access, feeds stubbed/recorded | U, P, most I |
| **Server `og-test`** (192.168.5.35) | A second Postgres database (`og_test`) and a second Mosquitto listener/prefix alongside the eventual `og` production instance, same systemd unit files parameterized by environment file, `agent-sim` at 2,000 hubs (10,000 for N) | I (real feeds via record/replay), E, N, C, S |
| **Server live** (`og`, `https://base.tocy-net.net/og/`) | The production database and units, live ERCOT/EIA/NWS feeds | Only the G5 demo-acceptance script (§5) and any rehearsal explicitly scheduled with the operator; no destructive or chaos test ever runs here |

Never cut: guardian signing, the commitment lock, reserve, one buyer, the trace (delivery plan §6) — these are P1 in
every environment and gate.

### 1.3 Entry / exit criteria per gate

| Gate | When | Entry | Exit |
|---|---|---|---|
| **G1** | Fri 15:00 | This document, `02a`/`02b`/`03` drafted | User approves the spec pack (this document included) |
| **G2** | Fri 19:00 | Repo, migrations, contracts, trace lib, CI, `sim` v1, `feeds` v1 exist | CI green (U+P for merged modules); telemetry visibly flowing hub→MQTT→Postgres on `og-test`; feeds populated with at least one real ERCOT/EIA/NWS pull; `deploy.sh` succeeds against `og-test` |
| **G3** | Sat 06:00 | All 12 modules coded against frozen interfaces | Every module's U and P suites pass (100% of P1 property tests, 0 Hypothesis failures at ≥ 10³ examples); `mutmut`/manual mutation spot-check on `guardian` and `ledger` kills ≥ 80% of seeded mutants (reduced bar vs the full-engine 90%, §8.4-style waiver, recorded) |
| **G4** | Sat 12:00 | End-to-end loop deployed on `og-test` | A1–A10 functional scenarios (§3, §5 steps 1–16) pass on the server; invariant monitor (§2) reports 0 hits over the run; baseline rule-selector shadow comparison produced |
| **G5** | Sat 16:00 | G4 passed | A11 non-functional suite (§4) passes at 2,000 hubs (10,000 measured and recorded even if only "measured, not met"); each of the 7 processes (including `og-safestop` independently) killed and recovers per §4.6; Postgres and MQTT restart tests pass; 1-hour soak completes with 0 invariant hits; nightly `pg_dump` verified restorable |

**Cut-line interaction (delivery plan §6).** If cut line 1 fires (rule selector live, LP in shadow at G3), §3 ES05 tests
run against the rule selector as SUT and the LP as the shadow-comparison target, not vice versa; §5 step 8 notes which
is live. If cut line 2 fires (`DIST_DEFERRAL` open-loop), TS-05 PI-loop tests (TS-05-09/10) are waived for the demo and
recorded as a known limitation, not silently skipped. Cut line 3 (profitability baseline/forgone-upside dropped) waives
TS-08-07/08 for the demo only.

### 1.4 Defect severity policy

| Severity | Definition | Blocks |
|---|---|---|
| **S1 — blocker** | Any invariant (K1–K13) violation of any magnitude, in any environment; a guardian bypass or unsigned command reaching `sim`; data loss on trace or ledger tables; a crash of `guardian`, `ledger`/`selector`, or `safestop` | The current gate and every later gate; the run is invalid and must be re-executed after the fix, not just the failing case |
| **S2 — major** | An acceptance item (A1–A11) fails with no workaround; a functional test tied to a Must story fails | The gate that requires that acceptance item; may be waived past G4/G5 only with a written, time-boxed risk note (delivery-plan style) |
| **S3 — minor** | UI cosmetics, a P2/P3 test, a non-blocking usability issue, a threshold met but with a wider margin than targeted | Tracked; does not block a gate |

**What blocks release.** By explicit instruction: **any invariant violation is a blocker (S1), full stop**, independent
of how small, how rare, or how late in the schedule it is found. There is no S1 waiver for K1–K13. Everything else
follows the S2/S3 rules above and the delivery plan's cut lines.

---

## 2. Invariant property tests (K1–K13)

**Canonical source.** K1–K13's definitions, principles and enforcement points are fixed by
[`00-invariants.md`](00-invariants.md); this section does not redefine them, only maps each canonical K to its
MVP-S property test(s). An earlier draft of this document used its own, differently-numbered K1–K13 catalogue
(drafted in parallel with `02a`/`02b` before `00-invariants.md` existed); that catalogue is retired and the mapping
below uses the canonical IDs throughout — every test ID keeps its original number, only the K column changed where
the old and new numbering meant different things. All thirteen are **zero-tolerance** (§1.4): the invariant monitor
(a process-independent watcher subscribed to trace, ledger and MQTT, modelled on `HC-TL-05`) runs in every I/E/N/C
run from G3 onward and fails the run on any hit.

| K | One-line summary (see `00-invariants.md` for the full definition) | Primary enforcement | Test IDs |
|---|---|---|---|
| K1 | Homeowner reserve never breached | `guardian` G-01, `ledger` | TS-05-01 |
| K2 | One buyer: single-writer ledger, no double reservation | `ledger` | TS-05-02 |
| K3 | Sole signer: no command moves MW without a valid guardian signature | `guardian`, `sim` hub | TS-06-01 |
| K4 | Physical envelope: P/kVA/ramp limits, no synchronized fleet steps, firm-event ramp ceiling | `guardian` G-02…G-06 | TS-06-05 |
| K5 | Grid authority: L2 instructions are hard constraints, never traded for commercial value | `allocator`, `guardian` G-15 | TS-05-14 (new) |
| K6 | Command freshness: sequence/epoch/lease strictly increasing, stale/duplicate commands rejected | `guardian` G-13, `sim` hub | TS-03-01 |
| K7 | Degrade, don't trip: TIMEOUT ≠ VETO ≠ STOP; hold → schedule → local autonomy, never an unplanned trip | `guardian`, `sim` hub lease | TS-03-02, TS-06-04 |
| K8 | Stop authority: a scoped safe stop works with `og-engine` and `og-guardian` both down; stop-only key never releases | `safestop` (independent process) | TS-06-03 |
| K9 | One loop per quantity: exactly one integrating controller (the `DIST_DEFERRAL` PI) regulates a given physical quantity | `allocator` PI, `guardian` G-03 | TS-05-15 (new) |
| K10 | Trace before act: no command is signed unless its decision pre-image is durably traced first | `guardian` G-14, `trace` | TS-09-09 (new) |
| K11 | Verifiable track record: hash-chained trace, configurable retention, verifiable across pruning | `trace` | TS-09-01, TS-09-02 |
| K12 | Time quality: guardian refuses to sign when its own clock offset from NTP exceeds its limit | `guardian` G-20 | TS-06-18 (new) |
| K13 | **Commitment lock.** Granted kW for a committed obligation never drops below $\hat y_{o,t}$ except on an L0/L1/L2 reason code or verified infeasibility, each traced; substitution (same obligation, different hub) is allowed, switching (capacity moved to a different obligation) is not | `selector`/`ledger` (freeze), `guardian` G-19 | TS-04-01…04 |

**Retired-numbering cross-reference** (for anyone comparing against an earlier draft of this document): old K4
("guardian never edits a value") is now evidence for **K3**; old K5 ("safe-stop never releases") is **K8**; old K6
("TIMEOUT≠VETO≠STOP") is **K7**; old K7 ("non-anticipativity") is not a numbered canonical invariant — it is
principle P5, still tested as TS-05-03 but carrying no K tag; old K8 ("ramp/feeder ceilings") is **K4**; old K9
("lease/epoch monotonic") is **K6**; old K10 ("lease expiry → local fallback") is **K7**; old K12 ("no kWh billed
twice") is evidence for **K2** applied at the settlement layer. Old K1, K2, K3, K11 and K13 already used the same
numbers as the canonical set and are unchanged.

### 2.1 Property test specifications

| ID | Title | Level | Preconditions | Hypothesis strategy / steps | Expected result (threshold) | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-03-01 | Lease/epoch never regresses | P | `sim` hub with a lease/epoch counter; `guardian` epoch authority | Generate random interleavings of batches with epochs drawn from a window around the current epoch, including duplicates and out-of-order delivery, ≥ 10³ cases | A command whose epoch ≤ the hub's last-accepted epoch is always refused/ignored; the hub's accepted-epoch sequence is monotonically non-decreasing in every case | A3, A6 | K9 | — (new; nearest: `TC-FUN-108` islanding exclusion) |
| TS-03-02 | Fail-to-local-schedule on lease expiry | P | Hub with a signed fallback schedule loaded | Randomize lease expiry time relative to in-flight commands, ≥ 500 cases | Every case: hub state after expiry equals the signed fallback profile for that clock time, never last-command-hold-forever nor an unbounded/undefined output | A6 | K7 | `TC-FUN-108` (islanding) as a related fixture |
| TS-04-01 | Commitment lock under randomized call arrivals and prices | P | ≥ 2 obligations of the same tier can be admitted; price series randomized | Generate random arrival times and price paths for a second, higher-value same-tier call while the first is `delivering`, ≥ 10³ cases via Hypothesis `stateful` rules (admit, tick, price-move) | In every case, the earlier-committed obligation's granted kW never drops below $\hat y$ unless an injected L0/L1/L2/infeasibility event accompanies the drop; 0 cases of a price-only reduction | A4, A10 | K13 | — (new; review §3.4 replay fixture) |
| TS-04-02 | Commitment lock holds cross-tier | P | Obligations from ≥ 2 tiers | Randomize a lower-tier call's value up to and past the committed higher-tier call's value, ≥ 500 cases | The committed obligation is never reduced to serve a lower- or equal-priority new call regardless of relative price | A4, A10 | K13 | — |
| TS-04-03 | Substitution allowed, switching forbidden | P | One committed obligation, ≥ 2 eligible hubs with spare capacity | Randomly fail/derate the serving hub(s), ≥ 500 cases | The obligation's total granted kW is maintained (or shortfall traced) by swapping hubs; the *obligation identity* receiving the kWh never changes without a reason code | A4, A10 | K13 | — |
| TS-04-04 | Re-nomination point is the only mid-window reselection gate | P | A multi-day/tolling contract fixture with declared re-nomination points | Randomize price/call pressure between points, ≥ 500 cases | 0 reselections strictly between re-nomination points; exactly the contract's declared points allow a new $x_o$ decision for that contract | A4, A9 | K13 | — |
| TS-05-01 | Reserve never breached (property) | P | `FX-FLEET-M`-equivalent MVP-S fleet fixture (§7.2), randomized SoC and command sequences | Generate command batches and load profiles, ≥ 10⁴ cases nightly / 10³ per-commit | $e_{b,t}\ge$ reserve floor for every hub/interval in every case; 0 breaches | A2, A3, A10 | K1 | — |
| TS-05-02 | One kWh, one buyer | P | ≥ 2 obligations competing for the same hub's headroom | Randomize concurrent reservation requests against the ledger, ≥ 10⁴ cases | $\sum_o r_{o,b,t}\le \text{cap}_{b,t}$ always; no interval double-reserved to two obligations | A4, A5, A10 | K2 | — |
| TS-05-03 | Non-anticipativity | P | Selector fixture with a forecast horizon | Feed the selector a scenario set where a future-only signal is injected after the gate; randomize timing, ≥ 500 cases | The gate's decision is bit-identical whether or not the future-only signal is present (it could not have seen it) | A4 | — (P5; not a numbered canonical invariant) | — |
| TS-05-14 | L2 instruction always wins over commercial value (new) | P | An `ERCOT_ENERGY`/`ERCOT_AS` obligation plus a randomized simulated L2/ISO instruction | Randomize instruction magnitude/timing against competing commercial value, ≥ 500 cases | The L2 instruction is honored as a hard constraint in 100% of cases regardless of the commercial opportunity cost; never relaxed for a better price | A4, A5, A10 | K5 | — (new; canonical K5 had no property test) |
| TS-05-15 | Exactly one integrating loop per regulated quantity (new) | P | A bank with an active `DIST_DEFERRAL` PI loop and other services drawing on the same bank | Randomize other services' set-point requests against the PI loop's target, ≥ 500 cases | Only the `DIST_DEFERRAL` PI integrates on bank kVA; every other consumer's view of that quantity is feed-forward only (no second integrator ever accumulates error on the same quantity) | A5 | K9 | — (new; canonical K9 had no property test) |
| TS-06-01 | No command without a valid guardian signature | P | `sim` hub verifies Ed25519 | Generate unsigned, badly-signed, and validly-signed batches, ≥ 10³ cases | Only validly-signed batches execute; every attempt (pass or refusal) has a trace entry | A3, A9, A10 | K3 | `TC-FUN-157` (bad-signature rejection, related pattern) |
| TS-06-02 | Guardian never edits a value | P | Guardian receives a proposed batch | Fuzz proposed batch fields; assert on the signed output | The signed batch's kW/kVA/duration fields are byte-identical to the proposal, or the batch is refused/held — never partially modified | A3, A10 | K3 | — |
| TS-06-03 | Safe-stop is stop-only | P | `og-safestop` scoped to fleet/zone/bank | Randomize stop/attempted-release sequences, ≥ 500 cases | No sequence of inputs to `og-safestop` ever raises a stopped scope's output above 0; only a fresh engine cycle after stop-clear re-grants | A3, A10 | K8 | — |
| TS-06-04 | TIMEOUT, VETO and STOP are distinct | P | Guardian in each verdict state | Drive guardian into TIMEOUT (no verdict in time), VETO (fails a G-check) and STOP (safe-stop engaged), ≥ 100 cases each | TIMEOUT → hold at last good grant; VETO → refuse this batch only, engine may resubmit; STOP → 0 output, no resubmission accepted until cleared. All three verdicts are distinguishable in the trace | A3, A6, A9 | K7 | — |
| TS-06-05 | Ramp/feeder ceilings hold under firm events | P | A firm obligation plus discretionary load on the same feeder | Randomize simultaneous firm-event starts and discretionary ramps, ≥ 500 cases | Fleet and per-feeder ramp stay within G-03/G-04/G-05 limits even when a firm event starts; firm events are pre-staged, not exempted from the *ceiling* (only from being throttled below their contracted ramp) | A2, A5, A10 | K4 | — |
| TS-06-18 | Guardian refuses to sign on a skewed clock (new) | P | Guardian with an injectable NTP-offset test hook | Randomize clock offset across and past the configured limit, ≥ 500 cases | 100% of cycles with offset beyond the limit sign nothing (G-20 hold, not a veto or stop); 100% within limit proceed normally | A3, A10 | K12 | — (new; canonical K12 had no property/negative test) |
| TS-08-01 | No kWh billed twice | P | Meter intervals for ≥ 2 obligations sharing a hub across a day | Randomize overlapping obligation windows and interval boundaries, ≥ 10³ cases | $\sum$ billed kWh per hub-interval across all invoice lines ≤ metered kWh for that interval; recount oracle agrees within ±0.5% | A7, A8, A10 | K2 | — |
| TS-09-01 | Trace chain verifies after pruning | P | Trace with configurable retention per event class; checkpoint anchors | Randomize retention windows and prune points, then run the verifier, ≥ 200 cases | `verify(range)` succeeds across a pruned boundary using the checkpoint anchor; a tampered pre-checkpoint byte is still detected via the anchor hash | A9 | K11 | — |
| TS-09-02 | No command without a trace entry | P | Full stack on `og-test` | Randomize command volume/timing, ≥ 500 cases | Every guardian verdict (PASS/HOLD/VETO/STOP) and every executed command has exactly one trace entry; count of guardian verdicts = count of trace entries of that class | A3, A9, A10 | K11 | — |
| TS-09-09 | Every signed command has a durably-traced pre-image (new) | P | Full stack on `og-test`, guardian signing path instrumented | Randomize command volume/timing, assert ordering of trace-write vs signature, ≥ 500 cases | For every signed command batch, its decision pre-image trace row's `created_at`/sequence precedes the signature timestamp; count of signed batches ≤ count of pre-image trace rows in every case | A9, A10 | K10 | — (new; canonical K10 had no property test) |

---

## 3. Functional test cases by epic (87 cases)

Columns: ID · Title · Level · Preconditions · Steps · Expected result (threshold) · A · K · Existing TC.
Fixtures (`FX-*`) are the MVP-S-scaled equivalents defined in §7.2, reusing `05-testing/01-test-strategy.md` §4.2
naming where the same content applies at 2,000 rather than 500/10,000 hubs.

### 3.1 ES01 — Platform & data (TS-01-01…07)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-01-01 | Migrations apply cleanly on a fresh `og_test` DB | U/I | Empty Postgres 17 instance | Run migration set forward, then one rollback/forward cycle | 0 errors; schema matches the frozen data model in `02a`; `alembic`/migration tool reports clean state | A1–A11 (platform) | — | — |
| TS-01-02 | Pydantic contracts reject malformed inter-module payloads | U | Contracts package importable | Fuzz each shared model with missing/extra/wrong-typed fields | 100% of malformed payloads raise a validation error before reaching business logic | A4–A9 | — | — |
| TS-01-03 | `LISTEN/NOTIFY` delivers internal events under load | I | Postgres with `LISTEN/NOTIFY` wired | Publish 10k events/min for 5 min | 0 dropped notifications; consumer lag < 1 s p99 | A2, A4 | — | — |
| TS-01-04 | Telemetry partition-by-day rolls over correctly at midnight (CT) | I | Telemetry table partitioned | Advance the virtual/system clock across a day boundary while inserting | Rows land in the correct day partition; no insert failures at the boundary | A2 | — | — |
| TS-01-05 | Nightly `pg_dump` produces a restorable backup | I | `og_test` populated | Run `pg_dump`, drop to a scratch DB, restore | Restore succeeds; row counts match; the trace chain still verifies post-restore | A11 | K11 | — |
| TS-01-06 | Deploy script is idempotent | I | Server `og-test` | Run `deploy.sh` twice back to back | Second run is a no-op or clean redeploy; all 7 systemd units remain `active (running)` | A11 | — | — |
| TS-01-07 | No duplicated functions (new; static/import-graph check) | U | Full source tree importable; `og.core` package exists per `02b` §12 | Run an import-graph/lint check (e.g. an AST grep) for SoC-step, capability/envelope-limit, product-rule-rounding, Ed25519 sign/verify, and JCS+SHA-256 trace-hashing formulas defined anywhere outside `og.core`; also assert every consumer (`selector`, `allocator`, `fleet`, `guardian`, `sim`, `settle`, `trace`) imports these from `og.core` rather than reimplementing them | 0 matches for a duplicate formula outside `og.core`; the build fails if a second implementation of any `og.core`-owned function is introduced | A4–A11 (build-blocking) | — | — (new; enforces `02b` §12's ownership rule) |

### 3.2 ES02 — Live feeds & forecast (TS-02-01…07)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-02-01 | ERCOT price/load/wind/solar/AS-price ingestion | I | `feeds` running against record/replay corpus | Run one full day of replayed ERCOT data | All five series stored with correct units and timestamps; freshness badge reflects true recency | A1 | — | `TC-FUN-117`-style (load-zone correctness) |
| TS-02-02 | 30 req/min ERCOT budget respected | I | `feeds` scheduler live | Drive demand for 3× the budget for 5 min | 0 requests over 30/min in any rolling minute; excess requests queued or deferred, not dropped silently | A1 | — | — |
| TS-02-03 | EIA fallback engages on ERCOT outage | I | Fault proxy stubs ERCOT 5xx | Force ERCOT unavailable for 10 min | `feeds` switches to EIA source within one poll cycle; a feed-status event is traced; switch-back on recovery | A1, A6 | — | — |
| TS-02-04 | NWS weather refresh on schedule | I | `feeds` NWS adapter | Run 3 scheduled cycles | Weather updated each cycle; staleness badge accurate between cycles | A1 | — | — |
| TS-02-05 | Staleness threshold triggers "no new commitments" | E | `feeds` + `selector` on `og-test` | Stub the price feed stale beyond threshold (e.g. > 2 gate intervals) | `selector` admits 0 new commitments while stale; existing commitments (K13) are unaffected; alert raised; recorded reason code | A1, A4, A6, A10 | K13 | `TC-FUN-223` (stale ledger holds allocations — related pattern, `FM-ARB-014`) |
| TS-02-06 | Forecast quantiles (P10/P50/P90) computed and monotonic | U | `forecast` module | Feed a synthetic price series | P10 ≤ P50 ≤ P90 at every horizon step; values recompute each 15-min gate | A1, A4 | — | — |
| TS-02-07 | Feed freshness recovers and re-enables commitments | E | Continuation of TS-02-05 | Un-stub the feed | New commitments resume within one gate after freshness restored; no backlog of "phantom" commitments from the stale window | A1, A4, A6 | K13 | — |

### 3.3 ES03 — Fleet twin & test harness (TS-03-03…08)

(TS-03-01/02 are the K6/K7 property tests, §2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-03-03 | 2,000-hub `sim` boot and SoC physics | I | `sim` at 2,000 hubs | Boot, run 1 h simulated | Every hub reports plausible SoC trajectory (η, ramp, standby loss) within the spec §12.3 model tolerance | A2, A11 | K1 | — |
| TS-03-04 | Digital twin marks stale/faulted hubs excluded from capability | I | `sim` + `fleet` twin | Stop telemetry from 5% of hubs | Those hubs excluded from `capability(bank,t)` within one detection cycle (≤ 2 heartbeat intervals); flagged, not silently dropped | A2, A6 | — | `TC-FUN-108` (islanded home excluded, related) |
| TS-03-05 | Bank aggregation matches hub sum | U/I | `fleet` bank aggregation | Randomize hub states, aggregate to bank | Bank kW/kVA = Σ member hub values within floating-point tolerance | A2 | — | — |
| TS-03-06 | Scenario injector: partner call, price spike, feeder overload, comms loss, killed engine | I | `sim`/`grid-sim` scenario panel | Trigger each of the 6 scenario-panel events (delivery plan §4) | Each event is observable end to end (UI, trace) within 1 gate/cycle | A2–A6, A9 | — | — |
| TS-03-07 | Lease renewal under normal operation | I | `sim` hub, `engine` up | Run 30 min normal cycle | Lease renews every cycle; no unwarranted fallback-schedule activation | A2, A3 | K6 | — |
| TS-03-08 | Local autonomy is observable and bounded | E | Engine process killed (see §4.6) | Kill `engine`; observe hubs for 60 s | Hubs hold, then follow the signed fallback schedule (K7); no unbounded drift; recovers cleanly on engine restart | A6, A11 | K6, K7 | — |

### 3.4 ES04 — Contracts, opportunities & commitments (TS-04-05…16)

(TS-04-01…04 are the K13 property tests, §2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-04-05 | Same-tier rising value never displaces a committed obligation | I | Two `ERCOT_ENERGY`-tier calls, one already committed | Offer a second same-tier call at increasing value up to 3× the first | First obligation's $\hat y$ unchanged; second call only takes uncommitted headroom $H$ | A4, A10 | K13 | — |
| TS-04-06 | Cross-tier: a higher-value lower-tier call is refused headroom over a firm commitment | I | `HOME` reserve committed + a market call at higher $/MWh | Offer the market call at a price exceeding the firm tier's implied value | Firm tier retains its reservation; market call gets only $H$ | A4, A5, A10 | K13 | — |
| TS-04-07 | New call during delivery does not interrupt an in-flight obligation | I | Obligation `delivering` | Admit a new call mid-window | New call evaluated only against $H$; in-flight obligation's grants unaffected absent L0–L2/infeasibility | A4, A9, A10 | K13 | — |
| TS-04-08 | L0 exception (device safety) may reduce a commitment | I | Committed obligation; inject a hub fault requiring L0 action | Trigger L0 | $\hat y$ reduced only for the affected hub's share; reason code `R-COMMIT-LOCK-OVERRIDE-L0`; shortfall traced as `AT_RISK`, never as reallocation | A3, A9, A10 | K13 | — |
| TS-04-09 | L1 exception (homeowner reserve) may reduce a commitment | I | Committed obligation; homeowner lowers reserve setpoint mid-window | Trigger L1 | Reduction limited to the reserve-protecting amount; `R-COMMIT-LOCK-OVERRIDE-L1` traced | A3, A9, A10 | K13 | — |
| TS-04-10 | L2 exception (ISO/utility instruction) may reduce a commitment | I | Committed `ERCOT_ENERGY`/`ERCOT_AS` obligation; simulated ERCOT instruction arrives | Trigger L2 | Reduction bounded to the instructed amount; `R-COMMIT-LOCK-OVERRIDE-L2` traced; guardian G-15/G-19 both consulted | A4, A5, A9, A10 | K13 | — |
| TS-04-11 | Infeasibility with a substitute available | I | Serving hub fails; another eligible hub has headroom | Fail the serving hub | Obligation fulfilled via substitution; 0 shortfall; trace shows a substitution event, not a commitment-lock exception | A4, A9, A10 | K13 | — |
| TS-04-12 | Infeasibility with no substitute available | I | Serving hub fails; no eligible substitute (bank saturated) | Fail the serving hub with no headroom elsewhere | Shortfall recorded `AT_RISK` with `R-COMMIT-LOCK-INFEASIBLE`; never silently absorbed or reallocated to a new obligation | A4, A9, A10 | K13 | `TC-FUN-223`-style stale/insufficient-capacity pattern |
| TS-04-13 | Substitution allowed: hub swap keeps the same obligation | I | Committed obligation, ≥ 2 eligible hubs | Force a hub-level anomaly (not a system fault) | Delivering hub changes; obligation ID and $\hat y$ profile unchanged; substitution event traced (§8.8-style) | A4, A9, A10 | K13 | — |
| TS-04-14 | Multi-day contract: re-nomination point allows reselection | I | `FX-CTR-DD`-equivalent multi-day fixture with a declared re-nomination point | Reach the re-nomination point; offer a competing call | Reselection permitted only at that point, for that contract; a competing call between points is refused per K13 | A4, A9, A10 | K13 | — |
| TS-04-15 | Partial take respects `min_qty`/`increment` (ERCOT AS style) | I | `FX-CTR-AS`-equivalent product rule: `min_qty` 0.1 MW, `increment` 0.1 MW | Offer 0.25 MW of eligible headroom | Selector takes 0.2 MW (rounds down to the increment ≥ `min_qty`), never 0.25 MW directly, never below `min_qty` | A4, A5 | — | — |
| TS-04-16 | Partial take respects `block`/all-or-nothing (partner style) | I | `FX-CTR-PC`-equivalent all-or-nothing block product | Offer headroom insufficient for the full block | Selector takes 0 (rejects the block), never a partial fraction of an all-or-nothing product | A4, A5 | — | — |

### 3.5 ES05 — Dispatch (selector, ledger, allocator) (TS-05-04…13)

(TS-05-01/02 are the K1/K2 property tests; TS-05-03 tests non-anticipativity, P5, no K tag; TS-05-14/15 are the
K5/K9 property tests, §2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-05-04 | Selector runs at every 15-min gate plus admission events | I | `selector` scheduled | Run 2 h simulated | A gate solve occurs every 15 min and on each injected admission event; no missed gate | A4 | — (P5) | — |
| TS-05-05 | Selector falls back to the rule selector on LP failure/timeout | I | `selector` with an injected HiGHS failure/timeout | Force infeasible/non-convergent LP | Rule selector (firm → AS → market) takes over within the same gate; event traced; LP resumes next gate if healthy | A4, A6 | — | — |
| TS-05-06 | Rule-selector shadow runs alongside the LP | I | Both selectors wired | Run one gate | Shadow rule-selector plan computed and stored without affecting live dispatch; used for the value-added/forgone-upside comparison (cut line 3 permitting) | A4, A7 | — | — |
| TS-05-07 | RT allocator (2 s) applies committed $\hat y$ as equality/lower-bound before allocating headroom | I | Selector output with ≥ 1 commitment | Run 5 allocator cycles | Committed obligations always satisfied first from capability minus $\hat y$ subtraction; only $H$ optimized for spot | A4, A10 | K2, K13 | — |
| TS-05-08 | Dwell and hysteresis prevent flapping on uncommitted headroom | I | Price oscillating near a threshold | Oscillate price by ±$5/MWh repeatedly over 10 min | No hub switches mode more than once per 5-min dwell window; hysteresis band respected | A4 | — | — |
| TS-05-09 | `DIST_DEFERRAL` PI loop tracks bank kVA setpoint | I | `FX-BANK-HEL`-equivalent bank fixture | Step-change the bank load | PI loop converges to setpoint within the spec's step-response tolerance; no sustained oscillation | A5 | K9 | (waived if cut line 2 fires — recorded, not silently dropped) |
| TS-05-10 | `DIST_DEFERRAL` closed loop respects 95% rating ceiling | I | Same as above | Drive load toward the bank rating | Loop holds ≤ 95% of rating; never exceeds | A5, A10 | K4, K9 | (waived if cut line 2 fires) |
| TS-05-11 | `ERCOT_AS` capacity hold is maintained through the delivery window | I | `FX-CTR-AS`-equivalent AS obligation committed | Run through the hold window with competing headroom pressure | Held capacity never dips below the awarded amount absent an L0–L2/infeasibility event | A5, A10 | K13 | — |
| TS-05-12 | §7.4 AS forward release is default off | I | Fresh `og-test` config, no explicit override | Attempt to trigger a release path without enabling the flag | 0 releases occur; the release code path is unreachable with the default configuration; explicitly enabling it (test-only) is required to exercise the path at all | A4, A10 | K13 | — |
| TS-05-13 | §7.4 AS forward release, when explicitly enabled, is audited and bounded | I | Release flag explicitly enabled for this test only | Trigger a release under a qualifying CVaR condition | Release is future-intervals-only, capped, requires a recorded confirmation, and produces a traced buyback event | A4, A9, A10 | K13 | — |

### 3.6 ES06 — Guardian & safe stop (TS-06-06…23, plus TS-06-18)

(TS-06-01/02 are the K3 property tests; TS-06-03 is the K8 property test; TS-06-04 is the K7 property test;
TS-06-05 is the K4 property test; TS-06-18 is the K12 property test, §2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-06-06 | G-01 reserve floor — negative test | S | Guardian live | Propose a batch breaching reserve+1% | Refused `RESERVE_FLOOR`; traced | A3, A10 | K1 | — |
| TS-06-07a | G-02 hub power bound — negative test | S | Guardian live | Propose a batch exceeding a hub's P limit | Refused `DEVICE_LIMIT`; traced | A3, A10 | K4 | — |
| TS-06-07b | G-21 (supplemental) service-transformer loading — negative test | S | Guardian live | Propose Σ charging > 90% of transformer kVA | Refused `HOSTING_LIMIT_IMPORT`/`_EXPORT` | A3, A10 | — (supplemental, beyond the canonical 12) | — |
| TS-06-08 | G-03 bank/feeder loading — negative test | S | Guardian live | Propose net loading > 95% of rating | Refused; guardian calls the same `og.core.capability.recharge_headroom()` the allocator calls, on its own inputs (no oscillation, no drift between two hand-kept-in-sync formulas) | A3, A10 | K4, K9 (independent check on the PI outer loop) | — |
| TS-06-09 | G-04 hub/firm ramp — negative test | S | Guardian live | Propose a discretionary step > 50 MW/min fleet or > 10%/min bank | Refused `RAMP_LIMIT`; firm/ISO changes on their own pre-staged ramp still pass | A3, A5, A10 | K4 | — |
| TS-06-10 | G-05 synchronized step/stagger — negative test | S | Guardian live | Propose > discretionary cap/30 per 2-s tick | Refused `SYNC_STEP_LIMIT` | A3, A10 | K4 | — |
| TS-06-19 | G-06 feeder/substation ramp ceiling for firm events — negative test (new; closes the design gap in `ES06-S05`) | S | Guardian live, a firm event staged on a feeder with a configured ceiling | Propose a firm-event ramp exceeding the feeder's ceiling (but within the fleet-wide cap) | Refused or reshaped to the ceiling, never signed as proposed; a batch within every ceiling signs normally | A3, A5, A10 | K4 | — (new; review §6 finding #4) |
| TS-06-11 | G-09 reservation-ledger version check | S | Guardian live, ledger version bumped mid-build | Submit a batch built on a stale ledger version | Deferred to next tick, not signed against stale state | A3, A4, A10 | K2 | — |
| TS-06-20 | G-13 sequence/epoch/lease — negative test at the guardian (new; guardian-side complement to the `sim`-hub property test TS-03-01) | S | Guardian live | Submit a batch whose epoch/seq is not strictly greater than the last accepted for that hub, or outside the current lease | Refused `STALE_EPOCH`/`STALE_SEQ`/`EXPIRED`; traced | A3, A10 | K6 | — |
| TS-06-21 | G-14 trace pre-image present — negative test (new) | S | Guardian live, trace-write test hook that can be suppressed | Propose a batch for which the decision pre-image trace write is deliberately withheld | Guardian refuses to sign (hold); once the pre-image write succeeds, an otherwise-identical batch signs normally | A9, A10 | K10 | — (new; canonical K10 had no guardian-level negative test) |
| TS-06-22 | G-15 L2/ISO boundary — negative test | S | Guardian live, simulated ADER telemetered range known | Propose a batch whose telemetered range exceeds ledger-free capacity under an active L2 instruction | Refused; the L2 instruction's bound is enforced, never relaxed for commercial value | A4, A5, A10 | K5 | — |
| TS-06-12 | G-22 (supplemental) asset-state exclusion | S | A quarantined/opted-out/islanded hub in the batch | Propose a non-safe command to that hub | Only safe commands pass for that hub; others refused `QUARANTINED_ASSETS` | A2, A3, A10 | — (supplemental, beyond the canonical 12) | `TC-FUN-108` (islanded home refuses grid commands) |
| TS-06-13 | G-23 (supplemental) command rate/duplicate — negative test | S | Guardian live | Submit 2 commands for one hub in one 2-s tick | Batch rejected, `OUT_OF_ORDER`; a repeated `submission_id` acknowledged, never re-signed | A3, A10 | — (supplemental, beyond the canonical 12) | — |
| TS-06-14 | G-19 commitment lock — negative test (the new check) | S | A committed obligation; propose a batch reducing its granted kW with no reason code | Submit the batch | Guardian refuses to sign; `R-COMMIT-LOCK-*` absent ⇒ VETO; only a batch carrying a valid L0/L1/L2/infeasibility code is signed | A3, A4, A10 | K13 | — (new; this is review finding #1 closed) |
| TS-06-15 | Scoped safe stop — fleet scope | I | Operator UI + `og-guardian` (manual command path) + `og-safestop` (independent process) | Trigger fleet-wide safe stop with two-step confirmation | All hubs commanded to 0 within the scoped ramp; retained MQTT topic set; no release without a fresh operator action | A3, A10 | K8 | — |
| TS-06-16 | Scoped safe stop — zone/bank scope | I | Same, scoped to one zone/bank | Trigger zone-scoped stop | Only the targeted zone/bank stops; other zones continue normal dispatch and commitments (K13 unaffected outside the scope) | A3, A4, A10 | K8, K13 | — |
| TS-06-17 | Guardian TIMEOUT holds last grant | I | Guardian process delayed/unresponsive | Delay guardian response past its deadline | Engine holds the prior grant (no new commands issued); no STOP, no VETO; recovers on guardian response | A3, A6, A10 | K7 | — |
| TS-06-23 | `og-safestop` engages a fleet stop with `og-guardian` AND `og-engine` both killed (new; the K8 topology proof) | C | `og-safestop` up; `og-guardian` and `og-engine` both killed (see §4.6) | Trigger a fleet-scope stop via the operator path while both other processes are down | The stop still engages within the scoped ramp via `og-safestop` alone; no dependency on the killed processes is observed | A3, A10, A11 | K8 | — (new; proves the design-error fix in `02b` §1.3) |

### 3.7 ES07 — Health & degraded modes (TS-07-01…06)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-07-01 | Module heartbeats surface on the health screen | I | `health` + UI | Kill and restart one module | Heartbeat loss detected within 2 heartbeat intervals; recovery detected on restart | A6 | — | — |
| TS-07-02 | Feed staleness drives "no new commitments" degraded mode | E | Continuation of TS-02-05 | Observe health screen and engine behaviour | Degraded mode shown; existing commitments unaffected (K13); alert raised | A6, A10 | K13 | — |
| TS-07-03 | Engine down ⇒ hubs hold lease, then local autonomy | E | Kill `engine` (§4.6) | Observe for 60 s | Health shows engine down; hubs hold then follow fallback schedule (K7); no false "all clear" | A6, A11 | K6, K7 | — |
| TS-07-04 | Hub online/stale/fault classification | I | `sim` fleet with induced staleness and faults | Induce each class on a subset of hubs | Health/UI correctly classifies each hub; counts reconcile with `fleet` twin state | A2, A6 | — | — |
| TS-07-05 | Cycle latency alert on RT cycle overrun | I/N | Engine under load | Inject a slow cycle (> 500 ms) | Alert raised; cycle latency metric recorded; no crash | A6, A11 | — | — |
| TS-07-06 | Alert de-duplication and clearing | I | An active alert condition | Resolve the underlying condition | Alert clears automatically; no duplicate alert storm while the condition persists | A6 | — | — |

### 3.8 ES08 — Settlement (M&V, billing, profitability) (TS-08-02…09)

(TS-08-01 is the K2 property test — no kWh billed twice is evidence of one-buyer holding through settlement,
§2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result (hand-computed) | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-08-02 | Interval metering matches raw telemetry | I | 1-min meter blocks for one obligation | Run one hour; hand-compute expected kWh from the fixture's known power profile | Metered interval kWh within ±0.5% of the hand-computed value | A7, A8 | K2 | — |
| TS-08-03 | Performance % against baseline (worked example) | U | `FX-CTR-DD`-equivalent fixture with a stated baseline and delivered profile | Compute performance % by hand; run `settle` | `settle` output matches the hand-computed performance % exactly to stated precision | A7, A8 | K2 | — |
| TS-08-04 | Insert-only invoice lines | I | Settlement run twice over the same interval (e.g. a correction) | Attempt to UPDATE/DELETE an invoice line directly | Rejected at the DB/app layer; corrections are new insert-only lines referencing the original | A8 | K11, K2 | — |
| TS-08-05 | CSV export matches stored invoice lines | I | Invoice lines present | Export CSV, diff against DB query | 100% row/field match | A8 | — | — |
| TS-08-06 | Profitability: revenue − cost − degradation − penalty = net margin (hand-computed) | U | A fixture with known price, delivered kWh, degradation rate ($0.03/kWh), and one penalty event | Hand-compute net margin; run `settle` | Match to the cent | A7 | — | — |
| TS-08-07 | LP vs rule-baseline value comparison | I | Shadow rule-selector output (TS-05-06) available | Compare LP-dispatched net margin vs rule-baseline net margin for the same window | Value-added figure computed and displayed; sign and rough magnitude consistent with a hand-checked scratch calculation | A7 | — | (waived if cut line 3 fires) |
| TS-08-08 | Forgone-upside reporting under the lock | I | A K13 exception scenario (TS-04-06) with a documented forgone opportunity | Compute forgone upside by hand from the rejected call's value | `settle`/profitability screen reports a forgone-upside figure matching the hand computation within ±2% | A7, A10 | K13 | (waived if cut line 3 fires) |
| TS-08-09 | Settlement excludes stale/estimated intervals from `FINAL` status | I | An interval with a data gap | Run settlement over the gap | That interval marked estimated/flagged, never `FINAL`; the K2 double-billing monitor does not flag it as a violation because it is explicitly labelled | A7, A8 | K2 | — |

### 3.9 ES09 — Audit trace & retention (TS-09-03…08)

(TS-09-01/02 are the K11 property tests and TS-09-09 is the K10 property test, §2.1.)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-09-03 | Every event class appears in the trace | I | A run exercising selection, commitment, re-nomination, exception, shortfall, command, operator action, feed change, alert | Execute one of each | All 9 classes present in the trace with correct schema | A9 | K11 | — |
| TS-09-04 | Chain-verify button reflects true state | I | UI + trace | Click verify on a clean chain, then on a chain with a corrupted byte (test-only) | Clean chain: verified. Corrupted: failure reported with the break location | A9 | K11 | — |
| TS-09-05 | Reason codes include `R-COMMIT-LOCK-*` | I | K13 exception scenarios (TS-04-08…10, 12) | Inspect trace entries for those runs | Each exception carries exactly one of `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2` or `R-COMMIT-LOCK-INFEASIBLE` | A9, A10 | K13 | — |
| TS-09-06 | Retention is configurable per event class | I | `retention.<class>.days` settings | Set two classes to different retention windows | Pruning removes only events older than each class's own window | A9 | K11 | — |
| TS-09-07 | Pruning keeps checkpoint anchors | I | Continuation of TS-09-01 | Prune, then verify | Verification still succeeds using the checkpoint anchor for the pruned range | A9 | K11 | — |
| TS-09-08 | "Why" query returns the deciding factors for a chosen/rejected call | I | A completed selection cycle | Query why obligation X was/was not committed | Response cites the actual constraint/value figures from that cycle's solve, matching the stored plan | A4, A9 | — | — |

### 3.10 ES10 — Operator UI (TS-10-01…07)

| ID | Title | Level | Preconditions | Steps | Expected result | A | K | Existing TC |
|---|---|---|---|---|---|---|---|---|
| TS-10-01 | Control room shows live feeds, fleet MW/MWh, invariant counters | I/UI | UI up against `og-test` | Load control-room screen | Values update via SSE within 2 s of a change; invariant counters at 0 in a clean run | A1, A2, A11 | K1–K13 (display) | — |
| TS-10-02 | Fleet monitoring/control: manual command through the guardian | I/UI | Operator role | Issue a manual command from the UI | Command flows through guardian (signed), acknowledged, visible in trace | A3, A9 | K3 | — |
| TS-10-03 | Scoped safe stop two-step confirmation in the UI | UI | Operator role | Attempt safe stop | First click arms, second confirms; a single click never stops the fleet | A3 | K5 | — |
| TS-10-04 | Dispatch & commitments screen shows the lock | UI | A K13 scenario running | Load the screen during TS-04-05 | Committed obligation shown `delivering` with $\hat y$; competing call shown pending/refused, not silently absorbed | A4, A9, A10 | K13 | — |
| TS-10-05 | Markets & feeds screen shows freshness/source status | UI | `feeds` running | Load the screen; force a stale feed | Freshness badge changes state within 2 s of the underlying status change | A1, A6 | — | — |
| TS-10-06 | Billing & audit screen: trace explorer and chain verification | UI | Trace populated | Open trace explorer, run verification | Entries browsable; verify button matches TS-09-04 result | A9 | K11 | — |
| TS-10-07 | UI refresh under load | UI/N | 2,000 hubs live | Observe UI during a busy cycle | All 7 screens refresh within 2 s (A11 threshold) | A11 | — | — |

**Functional count:** ES01 7, ES02 7, ES03 6, ES04 12, ES05 10, ES06 18, ES07 6, ES08 8, ES09 6, ES10 7 = **87**.

---

## 4. Non-functional tests

| ID | Title | Level | Setup | Steps | Pass criteria | A |
|---|---|---|---|---|---|---|
| TS-N-01 | RT cycle latency at 2,000 hubs | N | `og-test`, 2,000-hub `sim`, 30-min steady run | Measure allocator cycle wall time each 2-s tick | p99 < 500 ms | A11 |
| TS-N-02 | RT cycle latency at 10,000 hubs (measured) | N | `og-test`, 10,000-hub `sim` | Same as above | Recorded as "measured, not met" if it exceeds 500 ms p99 (delivery-plan cut line 4); not a gate blocker at this scale | A11 |
| TS-N-03 | UI refresh under live load | N | UI + 2,000 hubs | Measure SSE-to-render latency on all 7 screens during a busy cycle | ≤ 2 s on each screen, p95 | A11 |
| TS-N-04 | Ingest freshness | N | `feeds` live/replay | Measure time from source publish to `feed_obs` row | Within the source's own refresh cadence + 1 poll interval; freshness badge accurate | A1 |
| TS-N-05 | Memory per process | N | All 7 systemd units at 2,000 hubs, 1 h | Sample RSS per process every minute | Each process stable (no unbounded growth); total fits the server's ~14 GB free with headroom for Postgres/Mosquitto | A11 |
| TS-N-06 | 1-hour soak | N/C | Full stack, 2,000 hubs, mixed scenario load | Run 1 h continuously with the scenario panel cycling through injected events | 0 crashes, 0 invariant hits (§2), memory stable, no unbounded queue growth | A10, A11 |
| TS-N-07 | Postgres restart | C | Full stack up | `systemctl restart postgresql` mid-run | Engine reconnects and rebuilds state from Postgres; no invariant violation during outage or recovery; commitments (K13) intact after recovery | A11 |
| TS-N-08 | MQTT (Mosquitto) restart | C | Full stack up | `systemctl restart mosquitto` mid-run | `sim` and engine reconnect; hubs fail to local autonomy during the outage (K6/K7) and resume on reconnect; no duplicate/lost commands beyond the replay-cache window | A11 |

### 4.6 Process-kill matrix (7 systemd units)

`og-safestop` is a separate row from `og-guardian` — this is the deliberate K8 fix (`02b` §1.3): an earlier draft
colocated them in one process, which meant killing "guardian+safestop" together was the only testable case and hid
exactly the failure K8 exists to prevent. TS-C-03 and TS-C-03b are now independent kills.

| Process killed | Expected behaviour | Test ID | K |
|---|---|---|---|
| `og-feeds` | Feed staleness accrues; degraded mode "no new commitments" (TS-02-05/07) after threshold; existing commitments unaffected; auto-restart via `Restart=always` | TS-C-01 | K13 |
| `og-engine` (selector+ledger+allocator+fleet twin+forecast) | Hubs hold lease then local autonomy (K6/K7); guardian idle (no new batches to sign); `og-safestop` remains fully able to engage a stop (K8, see TS-06-23); recovers state from Postgres on restart | TS-C-02 | K1, K2, K6, K7, K8, K13 |
| `og-guardian` (alone; `og-safestop` stays up) | TIMEOUT semantics (K7): engine holds last grant, no new commands execute; `og-safestop` is unaffected and can still engage a stop with no guardian running (K8); restart resumes signing | TS-C-03 | K3, K4, K5, K7, K8 |
| `og-safestop` (alone; `og-guardian`/`og-engine` stay up) | Normal dispatch is unaffected (K8's independence cuts both ways: the engine/guardian do not depend on `og-safestop` either); a stop attempted while it is down fails closed (visibly refused, not silently accepted) until restart; restart resumes stop-readiness immediately (no state to rebuild — `og-safestop` is stateless beyond `stop_event`/the retained MQTT topic) | TS-C-03b | K8 |
| `og-sim` (fleet-sim+grid-sim) | MQTT telemetry stops; `fleet` twin marks all hubs stale within one detection cycle; engine holds; no false commands sent into a void; restart resumes telemetry | TS-C-04 | K6 |
| `og-settle` (settle + health evaluator + trace pruning) | M&V/billing/profitability stop advancing; health/heartbeat aggregation stops (Health screen goes stale, itself an observable symptom); no data loss, no double-counted interval on restart (K2); resumes from last processed interval | TS-C-05 | K2 |
| `og-api` | Operators lose the console; engine/guardian/safestop/dispatch continue unaffected (no coupling); restart restores UI with correct current state | TS-C-06 | — |

Each row is one `TS-C-0x` chaos test: `systemctl kill -s SIGKILL <unit>`, observe for 2 minutes, `systemctl start <unit>`, observe recovery for 2 minutes. Pass criteria: 0 invariant hits during outage and recovery; the specific expected behaviour above is observed; process returns to `active (running)` and rejoins normal operation without a manual data fix. TS-C-02 and TS-C-03 together (`og-engine` and `og-guardian` both down) feed directly into TS-06-23, the explicit K8 topology proof.

**Non-functional count:** 8 (`TS-N-*`) + 7 (`TS-C-*`, including `TS-C-03b`) = 15.

---

## 5. Demo acceptance script (E-level, Saturday 18:00 sign-off)

Run on the server live (or `og-test` if the operator prefers a final dry run first), against the 2,000-hub `sim`. Each
step is witnessed by the operator and the test log is attached to the evidence for that gate.

1. Open the control room. Confirm ERCOT price/load/wind/solar and AS prices are live with green freshness badges (A1).
2. Confirm 2,000 hubs streaming SoC/P/health on the map, updating within 2 s (A2).
3. Drill into one bank, then one hub; confirm state and last command shown (A2).
4. Issue a manual operator command to one hub; confirm guardian signature, hub ack, and a trace entry appear (A3).
5. Arm and confirm a zone-scoped safe stop (engaged by `og-safestop`, independent of `og-guardian`/`og-engine`); confirm only that zone stops; release only by a fresh operator action after clearing (A3, K8).
6. Inject a new `PARTNER_CAPACITY` call via the scenario panel; confirm it is admitted, evaluated by the selector at the next gate, and shown in the opportunity pipeline (A4, A5).
7. Confirm the call is committed and enters `delivering`; note the granted $\hat y$ (A4).
8. Inject a same-tier, higher-value competing call while step 7's obligation is delivering (A4, A10, K13). Confirm the original obligation's grant is **unchanged**, and the new call is shown pending/refused with a commitment-lock reason.
9. Inject an L1 event (homeowner reserve tightened) affecting a hub serving the committed obligation. Confirm a bounded reduction with `R-COMMIT-LOCK-OVERRIDE-L1` traced, and substitution to another hub where possible (A3, A4, A9, K13).
10. Show the `HOME`, `ERCOT_ENERGY`, `ERCOT_AS`, `DIST_DEFERRAL`, `PARTNER_CAPACITY` obligations all active simultaneously on the dispatch screen, each drawing from its own reservation (A5).
11. Confirm the health screen shows all 7 module heartbeats green (including `og-safestop`), feed freshness, hub health summary, and cycle latency (A6).
12. Kill the `og-engine` process from the terminal (then also kill `og-guardian`, per TS-06-23). Confirm the UI shows engine/guardian down, hubs hold then show local-autonomy behaviour, no invariant alert fires falsely, and a fleet-scope safe stop via `og-safestop` still engages with both processes down (A6, A11, K6/K7/K8).
13. Restart `engine`. Confirm recovery and resumed dispatch within the health screen's next update (A6, A11).
14. Open the profitability screen; confirm revenue/cost/degradation/penalty/net margin per obligation, and the LP-vs-rule-baseline comparison with forgone upside from step 8's lock (A7).
15. Open the billing & audit screen; export a CSV of invoice lines and spot-check one line's numbers against the profitability screen (A8).
16. Open the trace explorer; run chain verification; confirm it passes; show the `R-COMMIT-LOCK-*` entries from steps 8–9 (A9).
17. Show the invariant counters (reserve breaches, double-sold kWh, commitment switches without authority) all reading 0 across the whole run (A10).
18. Show the RT cycle latency panel with p99 < 500 ms at the current 2,000-hub load (A11).
19. (If attempted) Show the 10,000-hub measurement, labelled "measured, not met" if applicable, per cut line 4 (A11).
20. Confirm a nightly `pg_dump` exists and, on request, restore it to a scratch database and re-run chain verification to prove the backup is usable (A11, K11).

Sign-off: all 20 steps observed with the stated result, 0 invariant hits throughout, and any waived item (cut lines 2–4) explicitly called out as a known limitation rather than silently omitted.

---

## 6. Traceability matrix (MVP-S)

Full matrix by test ID; `A` = acceptance item(s), `K` = invariant(s), `Epic` = owning epic. Property tests (§2) and the
demo script (§5) are included; the full per-row functional table is §3 itself, so this section aggregates at epic
level plus lists every K13/guardian-critical row individually, since those carry the highest audit weight.

| Test ID(s) | A | K | Epic |
|---|---|---|---|
| TS-01-01…07 | A1, A2, A4–A9, A11 | — | ES01 |
| TS-02-01…07 | A1, A4, A6, A10 | K13 (TS-02-05/07) | ES02 |
| TS-03-01…08 | A2, A3, A6, A11 | K1, K6, K7 | ES03 |
| TS-04-01…16 | A3–A5, A9, A10 | K13 (all) | ES04 |
| TS-05-01…15 | A2–A6, A10 | K1, K2, K4, K5, K9, K13 | ES05 |
| TS-06-01…23 | A2, A3, A4, A5, A6, A9, A10 | K1, K3, K4, K5, K6, K7, K8, K9, K10, K12, K13 | ES06 |
| TS-07-01…06 | A2, A6, A10, A11 | K6, K7, K13 | ES07 |
| TS-08-01…09 | A7, A8, A10 | K2, K11, K13 | ES08 |
| TS-09-01…09 | A3, A4, A9, A10 | K10, K11, K13 | ES09 |
| TS-10-01…07 | A1–A4, A6, A9–A11 | K1–K13 (display only) | ES10 |
| TS-N-01…08, TS-C-01…06 (incl. TS-C-03b) | A1, A10, A11 | K1, K2, K6, K7, K8, K12, K13 | cross-epic (WS8) |
| Demo script steps 1–20 (§5) | A1–A11 (every item, in order) | K1, K3, K7, K8, K11, K13 (explicitly exercised); all others implied by the invariant-counter step 17 | cross-epic |

Coverage check: every acceptance item A1–A11 has ≥ 3 test IDs and appears in the demo script; every canonical
invariant K1–K13 now has ≥ 1 dedicated property test (§2) plus ≥ 1 functional or guardian negative test (§3, §4) —
this closes the previous gap where K5, K9, K10 and K12 had no property test at all (added as TS-05-14, TS-05-15,
TS-09-09 and TS-06-18 respectively); K13 (commitment lock) has the densest coverage (4 property tests, 12 functional
tests, 1 guardian negative test, 6 demo-script steps), proportionate to it being the review's #1-ranked finding.

---

## 7. Test data

### 7.1 Fixtures (reused/scaled from `05-testing/01-test-strategy.md` §4.2)

| ID | Content | Used by |
|---|---|---|
| `FX-FLEET-MVPS` | 2,000 hubs (MVP-S scale, generator logic reused from `FX-FLEET-M`); 25% EVs; banks/feeders/substations sized for the demo territory | ES03, ES05, N, C, demo script |
| `FX-FLEET-MVPS-10K` | 10,000-hub stretch scale (`FX-FLEET-L` equivalent) | TS-N-02 only |
| `FX-CTR-HOME` | Homeowner reserve 20–100%, storm-hold terms | `HOME` obligation tests |
| `FX-CTR-EE` | `ERCOT_ENERGY` simulated-QSE contract | ES04, ES05 arbitrage tests |
| `FX-CTR-AS-MVPS` | `ERCOT_AS`: min_qty 0.1 MW, increment 0.1 MW, hold window 4 h | TS-04-15, TS-05-11 |
| `FX-CTR-DD` | `DIST_DEFERRAL` on a Helotes-like bank, closed-loop PI target | TS-05-09/10, TS-08-02/03 |
| `FX-CTR-PC-BLOCK` | `PARTNER_CAPACITY` all-or-nothing block, ≥ 30 min notice | TS-04-16, demo script step 6 |
| `FX-PROF-5` | The five MVP-S dispatch profiles (`HOME`, `ERCOT_ENERGY`, `ERCOT_AS`, `DIST_DEFERRAL`, `PARTNER_CAPACITY`), signed test bundle | All |
| `FX-MD-DAY` | One day of ERCOT/EIA/NWS data via record/replay (subset of the full-engine `FX-MD-YEAR` corpus, one representative day with a price spike and a negative-price hour) | ES02, N, demo script |

### 7.2 Fleet profile

`FX-FLEET-MVPS`: 2,000 simulated hubs, 39.2 kWh nameplate / 11 kW inverter / 20% default reserve, 90% round-trip
efficiency, standby loss per `HC-AS-02`; house load and PV shape reused from the full-engine generator at MVP-S scale;
25% EV penetration; organized into banks/feeders/substations sufficient to exercise `DIST_DEFERRAL` (one Helotes-like
bank) and feeder ramp ceilings (K8). Grown to `FX-FLEET-MVPS-10K` only for the stretch performance measurement
(TS-N-02); functional correctness at 10,000 hubs is not required for G5.

### 7.3 Synthetic and live price inputs

- **Live**: real ERCOT Public API (prices, load, wind/solar, AS prices), EIA v2 fallback, NWS weather — pulled through
  `feeds` within the 30 req/min ERCOT budget (TS-02-02).
- **Synthetic/replay**: `FX-MD-DAY`, a recorded day used for deterministic I/E/N/C tests so the suite never depends on
  what ERCOT happens to publish during the 30-hour build window; includes one price-spike interval and one
  negative-price interval to exercise K13 (TS-04-01/02) and the arbitrage/degradation math (TS-08-06).
- Both sources carry the same schema so `selector`/`allocator` code paths are identical; only `feeds`' source
  selection differs.

### 7.4 Product-rule examples

| Product | Rule | Fixture |
|---|---|---|
| ERCOT AS (Non-Spin/ECRS style) | `min_qty` 0.1 MW, `increment` 0.1 MW, continuous above the minimum | `FX-CTR-AS-MVPS`; exercised by TS-04-15 |
| Partner capacity block | `block`/all-or-nothing, no partial take, ≥ 30 min notice, event ≤ 1.5 h | `FX-CTR-PC-BLOCK`; exercised by TS-04-16 |
| `DIST_DEFERRAL` need window | Continuous kW within the contracted window, closed-loop PI target, no min/increment (utility bilateral) | `FX-CTR-DD`; exercised by TS-05-09/10 |
| `HOME` | Not a market product; a standing constraint (reserve range), never a call | `FX-CTR-HOME` |

---

## Summary

- Levels: 21 property (P), 87 functional (mix of U/I/E/S), 8 performance (N), 7 chaos (C) = **123 test cases** total.
- Every canonical K1–K13 invariant has ≥ 1 property test and ≥ 1 functional/guardian negative test (K5, K9, K10, K12
  previously had none — closed in this pass); K13 (commitment lock) carries the heaviest coverage: 4 property, 12
  functional, 1 guardian, 6 demo-script steps.
- Every A1–A11 acceptance item has ≥ 3 linked tests and a step in the 20-step demo script (§5).
- Guardian negative tests cover the full canonical set — G-01…G-06, G-09, G-13, G-14, G-15, G-19 (commitment lock)
  and G-20 (time quality) — plus three supplemental, non-canonical checks (G-21…G-23).
- Any invariant violation is an S1 blocker in any environment, with no waiver; only S2/S3 items follow the delivery
  plan's cut lines.
- Gates G1–G5 map directly onto the delivery plan's schedule with explicit entry/exit criteria and a documented
  mutation-testing waiver at G3.
- Non-functional coverage includes RT cycle p99 < 500 ms at 2k hubs (10k measured), UI ≤ 2 s refresh, ingest
  freshness, per-process memory, an independent kill test for each of the 7 systemd units (including `og-safestop`
  on its own, TS-C-03b) with its expected degraded behaviour, Postgres/MQTT restart, and a 1-hour soak.
- A static "no duplicated functions" check (TS-01-07, §3.1) fails the build if a SoC/physics, limit-check, hashing
  or product-rule-rounding formula is re-implemented outside `og.core` — see `02b` §12 for the ownership rule this
  enforces.
- Existing `05-testing/*` TC IDs are reused where they fit (`TC-FUN-108`, `TC-FUN-117`, `TC-FUN-157`, `TC-FUN-223`);
  most MVP-S cases are new because commitment lock (K13) and this scale of degraded-mode testing are new since the
  first-principles review.
- Test data is scaled from the existing fixture catalogue (`FX-FLEET-M` → `FX-FLEET-MVPS`, `FX-CTR-*`) rather than
  invented fresh, so MVP-S evidence composes into the full-engine suite later with no rewrite.
