# Resolution A8 — UI/UX specification (`04-ui/01-ui-ux-specification.md`)

Status: v1.0 · 2026-09-25 · Owner: UX lead, operator console · Scope: every review finding that cites the UI or the
console, and every `00-decision-register.md` v0.2 item assigned to `04-ui` · Result: `04-ui/01-ui-ux-specification.md`
v0.3 → **v0.4** (edited in place; see its "Changes in this version").

**Method.** Each finding was treated as a claim and checked against the v0.3 text (a backup of v0.3 was kept outside
the repository for the comparison) before anything changed. The register wins where it and a review differ; register
items are cited as R*nn* / V-*nn* / Q*nn*. Nothing was dropped (D0a): screens outside the judged MVP keep their design and
carry the `R2` build tag. No code, installs or server access were involved.

**Dispositions.** *Fixed* — everything the finding or register item asks of the UI specification is done. *Partly
fixed* — the UI's part is done; the rest belongs to another document, named in the Note and in §6. *Rejected* — not
done, with the reason. *Deferred R2* — the specification itself is postponed (not used: every design is specified now;
`R2` build tags sequence building only). *User decision Qnn* — the design implements the register's proposed default
and waits for the user's answer.

## 1. Findings that cite the UI or the console

| ID | Verified? (evidence in v0.3) | Disposition | Where (v0.4) | Note |
|---|---|---|---|---|
| JDG-001 | Yes — 16 screens, 143 UI requirements, all Must/Should with no build tag or sequencing (§2.3, all requirement tables) | Partly fixed | Build column on all 21 requirement tables (209 rows: 83 `MVP-J`, 44 `MVP-B`, 82 `R2`); §2.3 legend; UI-GLB-01 | The MVP cut line, capacity plan and release map belong to `01-product/03`; the UI tags follow `06-reviews/04` §5.3/§5.5 and yield to the release map where it differs |
| JDG-003 | Partly — the 14-step storyline lives in `01-product/01` §5.4, E23 and TC-E2E; the UI had no demo mapping at all | Partly fixed | §13 (7-minute script mapped beat by beat to screens, requirements, build tags and fallbacks); UI-SIM-08 | Rewriting §5.4, E23 and TC-E2E-020…041 belongs to `01-product` and `05-testing` |
| JDG-004 | Yes — "firmness", "capture ratio" and "overlap" appear nowhere in v0.3; ownership only as UI-PLN-02's stacked bar and scenario deltas (UI-PLN-04), plus the SVC funnel | Partly fixed | §3.17 Insights: UI-INS-01 (ownership heatmap, planned vs realized, kW and $), -02 (price of firmness vs payment), -03 (breach radar + calibration), -04 (displacement ledger + kW rescued), -05 (M&V overlap), -06 (capture ratio) | FR-DE-050 "Should" → "Must" belongs to `03`/`01-product` |
| JDG-005 | Yes — no metric of what the orchestrator adds; no tile | Partly fixed | UI-OPS-09 (tile 1 + thesis caption), UI-INS-07 (four-policy replay report) | The replay itself (FR-DE-122 extended to today's rule allocator) is `03`/`01-product`; moving the IRR table out of `01-vision` §1 is `01-product` |
| JDG-010 | Yes — line 516 of v0.3: `3 AS │ ERCOT_AS Non-Spin │ 900 kW │ ⛔ Displaced │ $412 foregone`; UI-MKT-04's acceptance reconciled with "displaced-`ERCOT_AS` rows in DSP" | Partly fixed | DSP mock (§3.4) rebuilt: AS hold "Ring-fenced — held", deployments from the ring-fence; UI-DSP-01, -02, -15; UI-MKT-04 (buyback only for a §7.4 forward release or a capability loss); §3.0(l) | Refined with R17: the reviewer's "displaced examples from T3/T4" becomes T4 and price-responsive energy outside an on-line ADER, because an on-line ADER's instruction is an L2 hard constraint (UI-DSP-16). E05-S06, FR-PLAN-008 and FR-BILL-004 rewrites belong to `01-product` |
| JDG-011 | Yes — UI-OBL-04 and UI-PLN-01 bound "the day-ahead declaration" to 10:00 CT; the OBL mock labelled 14:00 "DA-close"; the PLN mock showed "DECLARED at 09:42"; UI-PLN-05 and §4.7 used 14:00 — the document was internally inconsistent | Fixed | Status bar (§2.2) two fixed markers; UI-OBL-04; UI-PLN-01; PLN day-before timeline; §4.7; §6.1 | DST-safe on 2025-11-02 and 2026-03-08 in the acceptance criteria; states `SUBMITTED`, `DECLARED`, `PROVISIONAL`, `LATE` |
| JDG-012 | Yes — UI-OBL-02 accepted "≥ one intraday re-plan cycle (≤ 15 min)" | Partly fixed | UI-OBL-02 (V-41: median ≥ 60 min, P10 ≥ 15 min), UI-INS-03 (calibration plot), UI-OPS-10 (scorecard KPI) | KPI-13 (`01-product/01`) and NFR-008 (`02-architecture/01`) still carry other targets |
| JDG-013 | Partly — the 21-row scorecard is in `01-product/01` §3 and E23-S05; the UI had no scorecard | Partly fixed | UI-OPS-10 (8 headline KPIs with measured value, target, provenance, drill-down) | The 8-KPI set and "value of orchestration" KPI must be added to `01-product/01` §3 and E23-S05 |
| JDG-019 | Yes — §2.3: 16 screens × 12 roles, 143 requirements | Fixed | §2.3 operator mode (7 screens + SCADA log panel + simple map), UI-GLB-01; every other screen keeps its design with `R2`; new Safety screen (§3.18) so `OP` reaches the kill switch; UI-MAP-09 simple map; UI-SCD-13 SCADA log panel | Operator mode filters navigation only; permissions unchanged |
| JDG-020 | Yes — performance only in §7 budgets; nothing on screen | Partly fixed | UI-OPS-11 (tick p99, telemetry→twin p99, commands/s, link to the benchmark report); §7 rows | The `bench/` report, the 1k→10k curve and the before/after optimization belong to `06`/`05-testing` |
| JDG-022 | Yes — v0.3 §10 set "SUS ≥ 80" as the overall target (the same infeasible summative gate as `05-testing` O7) | Partly fixed | §10: formative test (5 participants × 3 demo tasks, SUS reported, failures published) for the judged MVP; summative targets tagged `R2` | `05-testing` A-T3/O7 and G3-J item 7 are `05`'s |
| JDG-025 | Yes — §3.0(b) enum `Live`, `Pilot`, `Research`, `Planned`, `Feature-flagged` | Fixed | §3.0(b) enum `Live`, `Pilot contract`, `Planned`, `Design-only` with definitions; UI-SVC-06 acceptance | — |
| JDG-026 | Partly — the met/partly/unknown board is FR-UI-008/FR-RPT-005/E23-S05 (`01-product`); the UI's "Validation & Conditions" panel carried evidence and open reviewer conditions, i.e., business-case content in the console | Partly fixed | §3.0(b) reworked to "service status and measured facts" with a Projects Deck link; UI-OBL-05, UI-CUS-02, UI-SVC-06; UI-MNV-08 (measured P10 next to the 9.5 kW claim) | FR-UI-008, FR-RPT-005, E18-S04, E22-S04 and E23-S05 must move the board to the Projects Deck (§6) |
| JDG-027 | Partly — §3.0(h) declined personal-data questions with a message; no pre-send check log shown; no privacy framing | Fixed | §3.0(h); UI-GLB-09 (privacy card with the pre-send check log and trace ID); UI-SEC-17; §13 Q&A backups | Register Q17 default (decline on the node) applied |
| JDG-029 | Partly — cites `04-ui` among others; the UI had runbook links (UI-ALR-05) but no quick reference or first-shift path | Partly fixed | UI-GLB-07 (quick reference, top-5 task cards, runbook index); UI-ALR-05 ("stub" label); status-bar Help | `PROFILE=demo` seeding, `make demo` on k3d, README quick start and the integrator guide belong to `06`/`01-product` |
| GRD-046 | Yes — v0.3 has no EEA board, VDI or hotline log, shift log, handover, contact directory, or NPC/MPC/LPC/COP view; the quoted ISA-101 line is §1.1 principle 2 | Fixed | §3.19 Grid & ISO desk: UI-GOP-01 (as-operated topology and switching per bank, per phase), -02 (EEA board and posture), -03 (instruction and hotline log with acknowledgement timer), -04 ("what ERCOT sees" with history), -05 (shift log and handover), -06 (contact directory), -07 (autonomous response and settings conformance); UI-DSP-17 ("what ERCOT sees" beside the internal state) | Built `R2` except the DSP panel (`MVP-B`); test cases to be added by `05-testing` |
| GRD-010 | Yes — UI-SEC-05, §3.0(g), §4.4 and §9.1 made zone and fleet engage wait for a second approver; the exception covered only guardian or Critical-alarm stops; no verbal-instruction trigger | Partly fixed; User decision Q1 | §3.0(g) stop-engage rule; UI-SEC-05, -06, -15; UI-SAF-02 (trigger capture incl. ERCOT VDI and utility instruction); UI-SCD-10; §4.4, §4.10; §9.1; §1 principle 9 | Register R3 was amended accordingly (v0.2). FR-SAFE-007/008, TC-FUN-503/505, `03-security` §5.2 A-16/A-17 and §6.5 still show the old rule (§6). Q1 decides who co-signs |
| GRD-041 | Yes — §3.0(g) Tier 1 listed "any change to a customer's declared capacity"; §9.1 "Activate a high-value contract or change declared capacity — Tier 1" | Partly fixed | §3.0(g) "Automatic (pre-authorized)" row; §9.1; UI-PLN-05, -09; UI-OBL-08; UI-SCD-14; UI-DSP-17 | `03-security` §5.8 and `02` §2.3's transition table still list declared-capacity changes as Tier 1 |
| ARC-062 | Yes — `05` §2.8 L1 "console push 1 s → 5 s" against NFR-206 (≤ 2 s); v0.3 had no channel classes | Partly fixed | UI-GLB-04; §6.6; §7 (control-room latency ≤ 2 s at every shedding level) | `05` §2.8 should read "analytic channels only" |
| ARC-041 | Yes — v0.3 used its own degraded wording ("dispatcher running in degraded mode", "fell back to day-ahead schedule"), neither `05`'s fleet modes nor `03`'s DM codes | Partly fixed | §3.0(m) (DM causes mapped onto `05` §2.1 modes); UI-GLB-02; UI-OPS-05; UI-DSP-08 | R42's single entry/exit table is owned by `05`; the UI's display mapping follows it where they differ |
| ARC-020 | Partly — the finding is about NATS fan-out; v0.3 §6/§7.1 implied per-entity diffs to browsers | Fixed | §6.1: the console consumes 1-Hz server-side aggregates; per-hub detail on demand | — |
| ARC-001 | Partly — cites "16 console screens" as build-scope evidence | Partly fixed | Build tags and operator mode (see JDG-001) | Build plan and dates are `06`/`01-product` |
| RT-011 | Partly — §3.0(h) and UI-CUS-04 required human confirmation of AI-drafted intake, but no persistent `AI-drafted` flag, no source-span preview and no guardian dry run in the confirmation | Partly fixed | §3.0(h) "AI-drafted calls"; UI-CUS-04; UI-SEC-16; §9.1 last row | Intake itself is `R2`; CTL-121/CTL-146 extensions are `03-security`'s |
| RT-018 | Yes — v0.3's only stop path ran through the main console and the guardian; no independent path or alerting state was shown | Partly fixed | UI-SAF-08 (SSA, out-of-band path, independent alert channel, watchdog); UI-OOB-01…04; §4.11 | Dead-man alerting (CTL-056, ALR-290) and CTL-037's re-pointing are `03-security`/`06` |
| Red team §4 (SSA out-of-band trigger) | Yes — nothing in v0.3 | Fixed | §3.20 out-of-band stop console (stop only, no release; `og-safestop`, V-24); UI-SAF-08; §4.11 | Co-sign on the out-of-band console needs `03-security` confirmation (§6) |

## 2. Register items assigned to the UI

| ID | Verified? (evidence in v0.3) | Disposition | Where (v0.4) | Note |
|---|---|---|---|---|
| R3 (amended) | Yes — v0.3 implemented the v0.1 R3 (bank Tier 1, zone/fleet Tier 2, guardian/S1 exception; declared-capacity change Tier 1) | Fixed; User decision Q1 | §3.0(g); §9.1; UI-SEC-05, -06, -15; UI-SAF-02, -05, -06; UI-SCD-10; UI-PLN-05; UI-ADM-08; §4.3, §4.4, §4.10 | R3 stays *Proposed* until Q1; Q1's own default conflicts with SoD-03 (§6, item 3) |
| R4 (amended) | Partly — 30/60/120 s ramps present; no protective/non-protective classes, sequencing, frequency gating or retained scope topic | Fixed | UI-SEC-14; UI-SAF-03, -04; §3.18; §4.4 | Ramp values labelled [unsigned — Q13] |
| R10 (amended with R47) | Yes — UI-SVC-09 required the full-year replay for every profile change | Fixed | UI-SVC-09 (tiered gate: golden week + envelope check for tighten-only; full-year replay for loosening); §3 SVC interactions; V-27 note | — |
| R16 (closed) | Yes — no Safe-Stop Authority, no out-of-band surface | Fixed | §3.18 (UI-SAF-08, guardian-down state); §3.20 UI-OOB-01…04; §4.4 note; §4.11 | Tagged `MVP-J` here; the release map must confirm (§6) |
| R17 | Yes — no ADER status, UDSP tracking, hard-constraint rows or "what ERCOT sees"; §4.1 suggested curtailing `ERCOT_ENERGY` for an AT_RISK firm call | Fixed | UI-DSP-16 (L2 hard-constraint row), -17 (what ERCOT sees + invariant), UI-MKT-08 (UDSP/SPD, NCLR), UI-GOP-03/-04; §3.0(l); §4.1 | `03` §8.5 Example A variant still prices a squeezed base point (§6) |
| R19 | Yes — no emergency-posture display | Fixed | UI-GOP-02; UI-GLB-06; UI-SAF-04 (awarded AS → hotline note); V-16 frequency/EEA hold in UI-SAF-03 | Needs ERCOT notice ingestion (`04`, §4) |
| R20 | Partly — no close control existed, but readiness, lessee close, qualifying outage and `MOBILE_DER` were absent; a `FLT` task read "deploy/redeploy a unit" | Fixed | UI-OPS-07; UI-HUB-11; UI-CUS-05; UI-SVC-11; §1 `FLT` task; §5.2 (`MOBILE_DER` badge, D3 kept) | Q20 default (licensed field engineer sign-off) applied |
| R21 | Yes — no build tags | Fixed | Build column everywhere; §2.3 legend; operator mode | See JDG-001 |
| R22 | Partly — UI-SEC-02 integrity status only; no anchor window or journal mode | Fixed | UI-SAF-09; §3.0(m) (journal/anchor cause → CONSERVATIVE); UI-OOB-04 | — |
| R23 | Yes — no `SHADOW` state | Fixed | UI-GLB-03; §3.0(d) `RECORDED`; UI-INS-08; UI-ADM-08; §2.2 | `02` must add `RECORDED` to the command vocabulary (§6) |
| R24 | Yes — no Insights view, value-of-orchestration tile, scorecard, performance strip or 7-minute mapping | Fixed | §3.17 UI-INS-01…10; UI-OPS-09, -10, -11; UI-OBL-02; §13 | — |
| R25 | Yes — no QSE-desk role, no VDI or hotline log | Partly fixed | §1 `QSE` (proposed); UI-GOP-03; UI-SAF-02; §4.10 | `03-security` §5.1 must add the role code (§6) |
| R26 | Yes — no autonomous-response indicators | Fixed | UI-DSP-18 (FROZEN integrators, autonomous ΔP); UI-GOP-07; UI-HUB-13 (IEEE 1547 settings drift); UI-MNV-08; UI-GLB-06 | — |
| R27 / R27a | Yes — no partition market-role model or NOIE-consent display | Fixed | UI-SVC-10; UI-CUS-09; DSP breadcrumb; UI-MAP-09 | Q25 default applied |
| R31 | Partly — §4.6 said "dispatcher fails CLOSED"; no TIMEOUT distinct from VETO | Fixed | UI-DSP-19; §4.6; §3.0(m); §9 example | — |
| R33 | Yes — chip labels `CONFIRMED`/`TIMEOUT` vs `02`'s `COMPLETED`/`EXPIRED`; no `PARTIAL`/`SUPERSEDED` | Fixed | §3.0(d) mapping table; UI-DSP-05; HUB layout; §4.3 | `CONFIRMED`/`TIMEOUT` retired so "confirmation" means only the human Tier 1 step |
| R40 / V-29 | Yes — UI-DSP-07 mixed "online/substituting/degraded/quarantined/offline" | Fixed | §3.0(k); UI-DSP-07; UI-MAP-03; UI-HUB-12; UI-OPS-01; §4.2 | — |
| R42 | Yes (see ARC-041) | Fixed | §3.0(m); UI-GLB-02; UI-OPS-05; UI-DSP-08; §2.2 | — |
| R48 | Partly — no rejection-under-saturation wording, but no `Clipped`/`Deferred` outcomes either | Fixed | §3.0(l); UI-DSP-20; §6.6; §9 example | — |
| R49 | Partly — UI-DSP-12 had accept/reject but no durable constraint set | Fixed | UI-DSP-12; §3.0(h) | — |
| V-03, V-32 | Partly — §6.1 "10 s (faster during active events)"; no on-line-ADER cadence | Fixed | §6.1; DSP data sources | — |
| V-04, V-05 | Partly — §6.4 "timeout ceiling configurable (assumption: 30 s)"; §9 example "FAILED at 30 s" | Fixed | §3.0(d); §6.4; §9 example | — |
| V-12…V-15 | Partly — 15-min co-sign present; no Tier 1/Tier 2 expiries, single-use approvals or cumulative windows | Fixed | §3.0(g); UI-GLB-05; UI-SAF-06; UI-SEC-05, -15; §4.3 | — |
| V-16, V-17 | Partly — ramps present; "ramping up in stages" without ≥ 15 min, sequence or hotline step | Fixed | UI-SEC-14; UI-SAF-03, -05; §4.4 | — |
| V-19 | Partly — UI-CUS-07 had a due date without a value | Fixed | UI-CUS-07 (30 days, 45-day ceiling; Q16 default) | — |
| V-37 | Yes — role codes "provisional"; no `APR`; `SRE` as kill-switch approver (security: no fleet operations); `SYSADM` approving profiles (SoD-03); `STL` unmasking personal data (A-03) | Partly fixed | §1 role table and V-37 paragraph; §2.3; HUB, SVC, SEC, ADM, SCD permissions; §3.0(j); UI-HUB-07, -10; UI-ADM-08; UI-SIM-02 | `QSE` code, a SIM Lab row and the `UTL` portal scope need `03-security` (§6) |
| V-41 | Yes (see JDG-012) | Fixed | UI-OBL-02; UI-INS-03; UI-OPS-10 | — |
| Q1 | — | User decision Q1 | §3.0(g), §3.18, §11 items 8 and 16 | Default applied; routing limited to `03-security` §6.5 roles until answered |
| Q10 | — | User decision Q10 | UI-SAF-05; §11 item 21 | Default applied (utility zone vs load zone; only the engaging utility releases) |
| Q17 | — | User decision Q17 | §3.0(h); UI-GLB-09 | Default applied (decline on the node) |
| Q20 | — | User decision Q20 | UI-HUB-11; UI-OPS-07 | Default applied |
| Q22 | — | User decision Q22 | §13; §11 item 23 | Default applied (2026-10-21 on the node) |

## 3. Related findings addressed in the UI (they do not cite the UI)

| ID | What changed in the UI |
|---|---|
| JDG-009 | `SHADOW` mode surfaces (via R23): UI-GLB-03, UI-INS-08 |
| JDG-016 | Real-data provenance tile with live and replayed labels: UI-OPS-12 |
| JDG-017 | Partition market roles and NOIE consent (via R27a): UI-SVC-10, UI-CUS-09 |
| JDG-018 | Audited AI on/off switch and AI-off parity: UI-GLB-08, UI-DSP-13 |
| JDG-028 | Faults only through simulator APIs, ground-truth tags: UI-SIM-09 |
| JDG §3.1 CR-8 | Two pre-authenticated browsers, expiries unchanged: UI-SAF-07, §13 setup |
| ARC-015, GRD-055 | One hub-state vocabulary (via V-29): §3.0(k) |
| ARC-035 | Stub runbooks labelled from alarms: UI-ALR-05 |
| ARC-049 | Time-boxed AI constraint sets (via R49): UI-DSP-12 |
| ARC-061 | Clipped/deferred, never rejected for saturation (via R48): UI-DSP-20 |
| GRD-001, GRD-002, GRD-017, GRD-022, GRD-023 | ADER instructions as hard constraints, "what ERCOT sees", UDSP/NCLR tracking, COP: UI-DSP-16, -17, UI-MKT-08, UI-GOP-04 |
| GRD-003, GRD-006, GRD-027 | Regulated quantity in kVA or per-phase A; as-operated topology: UI-DSP-09, UI-GOP-01 |
| GRD-004 | EEA posture board: UI-GOP-02 |
| GRD-005, GRD-018, GRD-019 | Statute-shaped `MOBILE_TEEEF`, no Base close: UI-HUB-11, UI-SVC-11, UI-CUS-05, UI-OPS-07 |
| GRD-009, GRD-020 | Frozen integrators, settings conformance: UI-DSP-18, UI-HUB-13, UI-GOP-07 |
| GRD-014, GRD-021 | Instruction and hotline log, ICCP-loss steps: UI-GOP-03, §4.10 |
| GRD-016, GRD-024 | Market-role model and partner mode: UI-CUS-09, UI-SVC-10 |
| GRD-025 | Stop sequencing and frequency gating (via V-16): UI-SAF-03, UI-SEC-14 |
| GRD-037 | M&V overlap surfaced: UI-INS-05 |
| GRD-044 | Tail figure of buyback shown in the forward-release confirmation: §3.0(g), §9.1 |
| GRD-057 | Per-ADER qualified-MW caps as headroom: UI-MKT-05 |
| RT-001, RT-002 | Stop independent of the guardian (via R16): UI-SAF-08, UI-OOB-01…04 |
| RT-007 | Anchoring window and journal mode visible (via R22): UI-SAF-09 |
| RT-012 | Cross-principal cumulative windows shown (via V-14): UI-SAF-06 |

## 4. Disposition counts

Sections 1 and 2: **57 rows** (25 findings, 32 register items).

| Disposition | Findings (§1) | Register items (§2) | Total |
|---|---|---|---|
| Fixed | 7 | 25 (R3 also waits on Q1) | **32** |
| Partly fixed (UI part done; the rest is another document's — §6) | 18 (GRD-010 also waits on Q1) | 2 (R25, V-37) | **20** |
| User decision Qn (register default applied) | — | 5 (Q1, Q10, Q17, Q20, Q22) | **5** |
| Rejected | 0 | 0 | **0** (one refinement: JDG-010's "displaced examples from T3/T4" narrowed by R17) |
| Deferred R2 | 0 | 0 | **0** (every design is specified now; `R2` build tags only sequence building) |

## 5. New UI IDs (66)

| Family | New IDs | Build |
|---|---|---|
| GLB (global chrome, new) | UI-GLB-01…09 | 01, 02, 04, 05 `MVP-J`; 03, 07, 08, 09 `MVP-B`; 06 `R2` |
| OPS | UI-OPS-09, -10, -11, -12 | 10, 12 `MVP-J`; 09, 11 `MVP-B` |
| MAP | UI-MAP-09 | `MVP-B` |
| OBL | UI-OBL-08 | `MVP-B` |
| DSP | UI-DSP-15…20 | 15, 16, 18, 19, 20 `MVP-J`; 17 `MVP-B` |
| HUB | UI-HUB-11, -12, -13 | `R2` |
| PLN | UI-PLN-09 | `MVP-B` |
| MKT | UI-MKT-08 | `R2` |
| CUS | UI-CUS-09 | `R2` |
| MNV | UI-MNV-08 | `MVP-J` |
| SCD | UI-SCD-13, -14 | 13 `MVP-J`; 14 `MVP-B` |
| SVC | UI-SVC-10, -11 | `MVP-B` |
| SEC | UI-SEC-16, -17 | `R2` |
| SIM | UI-SIM-08, -09 | `MVP-J` |
| INS (Insights, new) | UI-INS-01…10 | 01–09 `MVP-B`; 10 `R2` |
| SAF (Safety, new) | UI-SAF-01…09 | `MVP-J` |
| GOP (Grid & ISO desk, new) | UI-GOP-01…07 | `R2` |
| OOB (out-of-band stop console, new) | UI-OOB-01…04 | `MVP-J` |

Amended in place (IDs unchanged): UI-OPS-01, -04, -05, -07; UI-MAP-03; UI-OBL-02, -04, -05; UI-DSP-01, -02, -05, -07,
-08, -09, -12; UI-HUB-07, -10; UI-PLN-01, -05, -08; UI-MKT-04, -05; UI-CUS-02, -04, -05, -07; UI-SCD-10; UI-SVC-06,
-09; UI-ALR-05; UI-SEC-05, -06, -13, -14, -15; UI-ADM-08; UI-SIM-02. The kill-switch requirements UI-SEC-04…06, -10…-12,
-14, -15 keep their IDs and now render on the Safety screen (§3.18).

## 6. Unresolved cross-document issues

| # | Owner | Issue | Needed change |
|---|---|---|---|
| 1 | `03-security/02` | §5.2 A-16/A-17 (zone/fleet engage = second approver), §5.8 "Exception" row and §6.5 "Approval to engage" still follow the v0.1 R3 | Single-person engage at every scope with a 15-min co-sign; ERCOT verbal and utility instructions as qualifying triggers (R3 amended) |
| 2 | `03-security/02` | §5.1 has no QSE-desk code (R25); §5.2 has no SIM Lab row; `UTL` portal scope unstated | Add `QSE` (or name another code); add a SIM Lab row (`SRE`; `OP` in `SIMULATION` deployments only); state the `UTL` portal |
| 3 | Register (user) | Q1's default ("system admin or executive on call" for fleet) conflicts with SoD-03 and §6.5's eligible roles | User decision Q1; the console routes only to §6.5 roles meanwhile |
| 4 | `03-security/02` | Out-of-band console: token population, co-sign by a second token holder (UI-OOB-03), SSA status endpoint; CTL-037 text still names a guardian endpoint | Confirm or amend; re-point CTL-037 at the SSA (R16) |
| 5 | `03-security/02`, `05` | Stop class (protective vs non-protective, V-16) is derived from the recorded reason category | Publish the category list and the audit flag for reclassification |
| 6 | `02-architecture/02` | §2.3's transition table still lists the v0.1 tiers; no `RECORDED` state for `SHADOW` (R23, R33); no entities for shift log, handover or contacts; `IsoInstruction` lacks caller and acknowledgement-time fields | Align §2.3 with R3 amended; add `RECORDED`; add the entities or fields UI-GOP-03/-05/-06 need |
| 7 | `02-architecture/05` | §2.1 SAFE_STOP row ("triggered by guardian and confirmed by one operator") predates the amended R3; §2.8 L1 slows the whole console; R42's single entry/exit table not yet published; no VDI acknowledgement target | Reword §2.1 and §2.8 (analytic channels only, ARC-062); publish the R42 table; set the acknowledgement target (UI shows an assumption) |
| 8 | `02-architecture/03` | §8.5 Example A variant prices a squeezed ERCOT base point (contradicts R17); FR-DE-050 still "Should"; FR-DE-122 lacks today's rule allocator as a policy; §8.13 DM codes not pointed at `05`'s modes | Re-run Example A under R17; FR-DE-050 → Must; extend FR-DE-122 (value of orchestration); map DM codes to `05` modes (R42) |
| 9 | `01-product/01…03` | FR-UI-008, FR-RPT-005, E18-S04, E22-S04, E23-S05 keep a condition board in the console (JDG-026); E05-S06, FR-PLAN-008, FR-BILL-004 (JDG-010); KPI-13 target (JDG-012); no value-of-orchestration KPI or 8-KPI headline set (R24); no FRs for Insights, Safety, Grid & ISO desk or the out-of-band console | Rewrite per the findings; add FRs and stories; confirm the UI build tags in the release map, especially the SSA/out-of-band console (`MVP-J` here) and the "what ERCOT sees" panel (`MVP-B` here) |
| 10 | `01-product/01`, `06-reviews/04` | The 7-minute script's beat 5 shows an AI proposal vetoed, while AI proposals (E07-S06, E20-S02) are `R2` in the same review's §5.5 | Decide: build proposals for J, or keep the scripted operator-dispatch veto (the §13 fallback) |
| 11 | `05-testing/02`, `04` | TC-UI-006, -009, -030, -032, -061, -073, -097, -118, -129 and TC-UX-003, -004, -008, -016 encode v0.3 behaviour (old tiers and strings, the 10:00 "declaration", the ≤ 15-min lead time, displaced-AS reconciliation, a `SEC`+`SRE` release pair); 66 new UI IDs have no tests; the formative test replaces the summative gate for J | Update the cases; add TC-UI/TC-UX cases for UI-GLB, INS, SAF, GOP, OOB and the new rows; regenerate the traceability matrix |
| 12 | `02-architecture/07` | Console-initiated SCADA controls still tiered by scope (zone = Tier 2); the grid-sim association's Secure Authentication exception label (Q11) | Align with R3 amended; label the demo exception as in UI-SCD-13 |
| 13 | `02-architecture/06` | The benchmark report location and recording rules behind UI-OPS-11; IdP session lifetime for the two demo browsers (UI-SAF-07); the dead-man channel status UI-SAF-08 reads | Specify them in the platform document |
| 14 | `02-architecture/04` | ERCOT operating notices (OCN, Advisory, Watch, EEA) are not ingested (GRD-004) but UI-GLB-06 and UI-GOP-02 need them | Add the notices to the ingestion catalogue |
| 15 | Register | R3 and R4 remain *Proposed* pending Q1; Q10, Q17, Q20, Q22 answered by defaults only | User decisions |
