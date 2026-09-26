# Team Notices

Newest first. **Every contributor and every AI agent must read this file at the start of each session.** Notices
change requirements or conventions and override older instructions in lane files.

---

## 2026-09-25 (late): progress report and power-quality spec approved

**1. Live status (release `20260925224517`, `main` @ `15aa223`).**

| Check | Result |
|---|---|
| A1 live ERCOT feeds | PASS (519 feed observations) |
| A2 hubs online | FAIL at check time (0/2000; telemetry was reconnecting after the restart); re-check pending |
| A4 grants persisted | PASS (65,040) |
| A4/A5 commitments | FAIL (0): `ledger.reserve()` returns `R-COMMIT-LOCK-INFEASIBLE`; under investigation (real K2 limit or a residual bug) |
| A6 guardian PASS | FAIL: 100% G-14 vetoes before the last fixes; re-check pending |
| A8 P&L / invoices | FAIL (0): blocked on commitments |
| A9 trace verify | PASS |
| A10 UI | PASS |
| A11 injection + alert | PASS (SCADA overload alert raised and cleared) |

Fixed today:

- the engine crash loop (`HubState.health` now includes `offline`);
- the G-14 pre-image write and key;
- the intake zone code (`LZ_*`, not `HB_*`) and `NSPIN`;
- degradation cost now applies only to MARKET;
- two selector crashes (the reservation interval key, and `obligation_id` passed to `ledger.reserve()`);
- the MQTT schema validator is compiled once;
- the simulators are bound to loopback;
- CSRF protection, the safestop key-generation print, and the test ACL.

**2. The service-profile and power-quality spec is APPROVED; this supersedes "do not implement it yet" below.**
`docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md`. Owner decisions:

- DATA_CENTER is a new profile.
- Schema changes are additive only.
- K14 and G-21..G-24 are canonical; G-25 covers calibration safety.
- PQ-aware selection works on uncommitted headroom only, **and** PQ is monitored continuously on delivered power, with
  correction on deviation.
- Waveforms come from each hub inverter over SCADA.
- Arbitrage uses the grid-code minimum.

Terminology:

- **Substitution** means moving delivery to different hubs.
- **Inverter swap** means a physical hardware replacement.
- Remote phase **recalibration** is tried before any swap.

Build status:

- Wave 1 (A: schemas and migrations `0010`/`0011`; B: `core/pq`; E: the simulator inverter model and calibration) is
  built on branch `wip/pq-wave1-20260925`. B is complete. A and E still need their lint, type and dedicated tests.
- Wave 2 (C guardian PQ checks, D allocator PQ eligibility and monitoring, G waveform ingest, H simulator waveforms,
  I asset health) and wave 3 (J API/UI, F DATA_CENTER) are owned by the lead's agents.

**Lane impact:**

- **rpagaria2000:** add K14 to P1 (PQ envelope never violated by a signed batch). Q2: add a scenario where an
  inverter drifts, is excluded from PQ-sensitive work, and is recalibrated (the `ogsim` catalogue has
  `inverter_calibration_drift` and `harmonic_injection`).
- **fancyviper007:** DOC1 needs a PQ alerts section (drafted after wave 3). DEMO: add "customer PQ deviation →
  correction". U1 covers PQ screens once wave 3 lands.

Please don't depend on the `wip/` branches; they are checkpoints, not review targets.

---

## 2026-09-25 (evening): requirement changes

**1. Energy is checked continuously, not just capacity (all lanes).**
A home can have kW headroom but not enough charge. Every cycle, the remaining energy above reserve is compared with
each committed delivery; a shortfall risk sets AT_RISK and raises `ALR-ENERGY-SHORTFALL-RISK`. A missing or stale SoC
means **zero discharge**. The guardian projects SoC over the command lease (`G-01-ENERGY`). See `BUILD.md` §2a and
`docs/orchestrator/07-delivery/00-invariants.md` (K1/K13 additions).

**2. Base hardware is confirmed (all lanes; update any fixtures you write).**
Per battery unit: **39.2 kWh usable, 11 kW**. **20% of homes have 2 units: 78.4 kWh, 20 kW** (every 5th hub id,
`hub-00004`, `hub-00009`, …). Reserve 20% (7.84 kWh per unit). Banks are **~600 kVA feeder segments** (40 × 50 homes).
Do not use 13.5 kWh / 5 kW / 75 kVA anywhere.

**3. Service tailoring and power quality are coming next (MVP-S+; spec draft pending owner approval).**
Each customer gets a **ServiceProfile** (pipeline AC mitigation, data center and arbitrage are different services
with their own control, measurement and settlement) and a **PowerQualityEnvelope** (phase, voltage, current,
frequency, power factor, THD). Inverter imperfections (frequency/amplitude offsets, harmonics, phase error) are
modelled and aggregated.
Spec: `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md` (draft).
**Do not implement it yet.** But do not hard-code per-service behaviour in new tests or UI: parametrise by service
type so profiles can plug in.

Lane impact:

- **rpagaria2000:** P1 property tests must include the energy-sufficiency invariants (no command implies discharge
  below reserve within its lease; Σ promised energy ≤ available energy). Q1 scenarios: add "energy exhausted
  mid-delivery → AT_RISK + substitution, never silent over-delivery".
- **fancyviper007:** U1 must cover the Dispatch screen's new energy margin / time-to-depletion columns. DEMO: add a
  step "energy runs low on a committed delivery → alert and substitution". Profiles and PQ screens come later.

**4. Process safety on the server (lead only, FYI).**
Test processes must never be stopped by pattern (`pkill -f`). See `BUILD.md` §6.

---

## 2026-09-25: team set-up

Lanes, work packages and the workflow are in `WORKBOARD.md`, `CONTRIBUTING.md` and `docs/team/lane-*.md`. The spec
set is under `docs/orchestrator/`.
